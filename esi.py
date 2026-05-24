import os
import json
import logging
import asyncio
import base64
import httpx
import sqlite3
import re
import http.server
import threading
import urllib.parse
import secrets
import time

from cryptography.fernet import Fernet

from fastmcp import FastMCP

from fastmcp.server import Context

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from config import *

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Create a FileHandler
file_handler = logging.FileHandler(ESI_MCP_LOG_PATH)
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 方便调试时追踪本地路径和日志文件位置
logger.debug(f"HOME_DIR: {HOME_DIR}")
logger.debug(f"ESI_MCP_DIR: {ESI_MCP_DIR}")
logger.debug(f"ESI_MCP_DIR exists: {ESI_MCP_DIR.exists()}")

# 用于加密保存 token 的密钥文件，以及 OAuth state 的有效期
FERNET_KEY_PATH = ESI_MCP_DIR / "master.key"
STATE_TTL_SECONDS = 300
PENDING_STATES = {}
PENDING_STATE_LOCK = threading.Lock()


# 加载已有的 Fernet 密钥；如果没有则生成一个仅当前用户可读写的密钥文件
def _load_or_create_fernet() -> Fernet:
    env_key = os.environ.get("ESI_MCP_FERNET_KEY")
    if env_key:
        return Fernet(env_key.encode())

    if FERNET_KEY_PATH.exists():
        key = FERNET_KEY_PATH.read_bytes().strip()
        return Fernet(key)

    key = Fernet.generate_key()
    FERNET_KEY_PATH.write_bytes(key)
    try:
        os.chmod(FERNET_KEY_PATH, 0o600)
    except OSError:
        pass
    return Fernet(key)


FERNET = _load_or_create_fernet()


# 判断某个值是否已经是加密后的字符串
def _is_encrypted(value):
    if value is None:
        return False
    try:
        FERNET.decrypt(value.encode())
        return True
    except Exception:
        return False


# 只有在未加密时才加密，避免重复加密导致读取失败
def _encrypt_if_needed(value):
    if value is None:
        return None
    if _is_encrypted(value):
        return value
    return FERNET.encrypt(value.encode()).decode()


# 将存储在数据库中的密文解析回明文 token
def _decrypt_value(value):
    if value is None:
        return None
    if value == "":
        return value
    if not _is_encrypted(value):
        return value
    return FERNET.decrypt(value.encode()).decode()


# 保存本次授权请求的 state，用来防止 OAuth 回调被替换或重放
def _store_state(state: str):
    with PENDING_STATE_LOCK:
        PENDING_STATES[state] = time.time()
        _purge_expired_states_locked()


# 回调到达时消费并删除对应的 state，确保一次性使用
def _consume_state(state: str) -> bool:
    with PENDING_STATE_LOCK:
        _purge_expired_states_locked()
        if state not in PENDING_STATES:
            return False
        del PENDING_STATES[state]
        return True


# 清掉过期的 state，避免内存里长期积累
def _purge_expired_states_locked():
    now = time.time()
    expired = [state for state, created_at in PENDING_STATES.items() if now - created_at > STATE_TTL_SECONDS]
    for state in expired:
        del PENDING_STATES[state]


# Initialize SQL database
# characters 表保存角色信息和 token，settings 表保存默认角色等配置
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                character_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                scopes TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()


init_db()

# SQL helper functions
# 这里统一在写入前加密、读取时解密，避免数据库里直接落明文 token
def save_character(character_id, name, scopes, access_token=None, refresh_token=None, token_expiry=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO characters 
            (character_id, name, scopes, access_token, refresh_token, token_expiry)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (character_id, name, scopes, _encrypt_if_needed(access_token), _encrypt_if_needed(refresh_token), token_expiry))
        conn.commit()


# 读取所有角色，返回时把 token 解密成可用明文
def get_characters():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT * FROM characters")
        columns = [desc[0] for desc in cursor.description]
        records = []
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            record["access_token"] = _decrypt_value(record.get("access_token"))
            record["refresh_token"] = _decrypt_value(record.get("refresh_token"))
            records.append(record)
        return records


# 读取单个角色的 token 信息，供请求钩子和刷新逻辑使用
def get_character_tokens(character_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("""
            SELECT access_token, refresh_token, token_expiry
            FROM characters WHERE character_id = ?
        """, (character_id,))
        row = cursor.fetchone()
    if not row:
        return None
    access_token, refresh_token, expiry_str = row
    return _decrypt_value(access_token), _decrypt_value(refresh_token), expiry_str


# 刷新 token 后，把新 token 重新加密存回数据库
def update_character_tokens(character_id, access_token, refresh_token, token_expiry):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE characters
            SET access_token = ?, refresh_token = ?, token_expiry = ?
            WHERE character_id = ?
        """, (_encrypt_if_needed(access_token), _encrypt_if_needed(refresh_token), token_expiry, character_id))
        conn.commit()


# 删除角色；如果删除的是默认角色，也同步清掉默认设置
def delete_character(character_id):
    with sqlite3.connect(DB_PATH) as conn:
        # Check if this is the default character
        default_char_id = get_default_character_id()
        is_default = character_id == default_char_id
        
        # Delete the character
        conn.execute("DELETE FROM characters WHERE character_id = ?", (character_id,))
        
        # If it was the default character, clear the default
        if is_default:
            conn.execute("DELETE FROM settings WHERE key = 'default_character_id'")
        
        conn.commit()
def get_default_character_id():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT value FROM settings WHERE key = 'default_character_id'")
        row = cursor.fetchone()
        return int(row[0]) if row else None

def set_default_character_id(character_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO settings (key, value) VALUES ('default_character_id', ?)
        """, (str(character_id),))
        conn.commit()


# ── Background OAuth callback server ──────────────────────────
# 这个本地 HTTP 服务负责接收 EVE SSO 的回调，并自动完成账号绑定。
# 之所以用同步函数，是因为回调线程里不能直接 await 异步逻辑。

def _exchange_code_sync(code: str) -> str:
    """同步交换 code，供回调线程调用。"""
    try:
        auth_str = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        with httpx.Client() as sync_client:
            resp = sync_client.post(
                "https://login.eveonline.com/v2/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CALLBACK_URL,
                },
                headers={"Authorization": f"Basic {auth_str}"},
            )
            if resp.status_code != 200:
                return f"Token exchange failed: {resp.text}"
            td = resp.json()
            at = td["access_token"]
            rt = td.get("refresh_token", "")
            ei = td.get("expires_in", 1199)

            # 从 JWT 中拿到角色 ID 和名字，便于保存本地角色记录
            parts = at.split(".")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
            sub = payload.get("sub", "")
            cid = int(sub.split(":")[-1]) if ":" in sub else 0
            cname = payload.get("name", "Unknown")

            # 再额外请求一次 ESI，拿到更完整的角色名称
            cr = sync_client.get(f"https://esi.evetech.net/latest/characters/{cid}/",
                headers={"Authorization": f"Bearer {at}"})
            if cr.status_code == 200:
                cname = cr.json().get("name", cname)

            # 保存 token，过期时间按当前时间 + expires_in 计算
            token_expiry = (datetime.utcnow() + timedelta(seconds=ei)).isoformat()
            save_character(cid, cname, " ".join(SCOPES), at, rt, token_expiry)
            if not get_default_character_id():
                set_default_character_id(cid)
            return f"Success: {cname} (ID: {cid})"
    except Exception as e:
        return f"Error: {str(e)}"


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles EVE SSO redirect at /callback."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/callback":
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            state = params.get("state", [None])[0]

            if not state or not _consume_state(state):
                logger.warning("Rejected callback: missing or invalid state")
                self._respond_html("<h2>❌ 回调校验失败</h2><p>state 参数无效或已过期。</p>")
                return

            if error:
                msg = f"Authorization Error: {error}"
                logger.warning(msg)
                self._respond_html(f"<h2>❌ {msg}</h2>")
            elif code:
                logger.info(f"Callback received, exchanging code...")
                result = _exchange_code_sync(code)
                logger.info(f"Auto-binding result: {result}")
                if result.startswith("Success:"):
                    cname = result.split(": ", 1)[1]
                    self._respond_html(
                        f"<h2>✅ 绑定成功！</h2>"
                        f"<p>角色: {cname}</p>"
                        f"<p>现在可以关闭页面回去聊天了。</p>"
                    )
                else:
                    self._respond_html(
                        f"<h2>❌ 绑定失败</h2><p>{result}</p>"
                        f"<p>请把浏览器地址栏中的 code 参数复制发送给我，手动调用 auth_with_code。</p>"
                    )
            else:
                self._respond_html("<h2>No code in callback</h2>")
        elif parsed.path == "/":
            self._respond_html(
                "<h2>EVE SSO Callback Server</h2>"
                "<p>Running. Waiting for EVE authorization redirect...</p>"
                "<p><a href='/authorize'>Start authorization</a></p>"
            )
        elif parsed.path == "/authorize":
            # 进入授权页前先生成新的 state，并保存到内存中，供 /callback 校验
            state = secrets.token_urlsafe(32)
            _store_state(state)
            auth_params = {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": CALLBACK_URL,
                "scope": " ".join(SCOPES),
                "state": state,
            }
            auth_url = f"https://login.eveonline.com/v2/oauth/authorize?{urllib.parse.urlencode(auth_params)}"
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _respond_html(self, body: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # quiet


def _start_callback_server():
    """Start the callback HTTP server in a daemon thread."""
    try:
        server = http.server.HTTPServer(("0.0.0.0", 8080), CallbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="esi-callback")
        thread.start()
        logger.info("✅ Callback server started on port 8080 (auto-binding enabled)")
    except OSError as e:
        logger.warning(f"⚠️  Could not start callback server on port 8080: {e}")


# Auto-start callback server at module load
_start_callback_server()


# 下面是 ESI 请求客户端的配置。
# 这里不写死 Authorization，而是根据请求路径动态补上当前角色的 token。
headers = {
    "Accept-Language": "en",
    "X-Compatibility-Date": "2025-08-26",
    "X-Tenant": "tranquility",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "ESI MCP Client",
}

# Create an HTTP client for ESI API
client = httpx.AsyncClient(
    base_url="https://esi.evetech.net",
    headers=headers,
)

# Define request hook to add authorization dynamically
async def add_auth_header(request):
    path = request.url.path
    # 只给角色相关接口和公司相关接口补 Authorization
    match = re.match(r'/characters/(\d+)/', path)
    if not match:
        match = re.match(r'/corporations/(\d+)/', path)
    if match:
        path_id = int(match.group(1))
        if '/characters/' in path:
            # 角色接口直接使用路径里的角色 ID
            character_id = path_id
        else:
            # 公司接口没有显式 ID，改用默认角色
            character_id = get_default_character_id()
            if not character_id:
                logger.error("No default character for corp endpoint")
                return

        # 读取当前角色的 token，并在过期时自动刷新
        token_data = get_character_tokens(character_id)
        if not token_data:
            logger.error(f"No tokens for character {character_id}")
            return
        access_token, refresh_token, expiry_str = token_data
        if not access_token:
            logger.error(f"No access token for character {character_id}")
            return

        try:
            if expiry_str is None:
                raise ValueError("No expiration time")
            expiry = datetime.fromisoformat(expiry_str)
            if expiry < datetime.utcnow():
                # 过期时走 refresh token 流程，拿新 access token
                async with httpx.AsyncClient() as temp_client:
                    token_params = {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": CLIENT_ID,
                    }
                    response = await temp_client.post("https://login.eveonline.com/v2/oauth/token", data=token_params)
                    if response.status_code != 200:
                        logger.error(f"Failed to refresh token for character {character_id}: {response.text}")
                        return
                    token_data = response.json()
                    access_token = token_data["access_token"]
                    refresh_token = token_data.get("refresh_token", refresh_token)  # May rotate
                    expires_in = token_data["expires_in"]
                    expiry = datetime.utcnow() + timedelta(seconds=expires_in)
                    expiry_str = expiry.isoformat()
                    update_character_tokens(character_id, access_token, refresh_token, expiry_str)
                logger.info(f"Refreshed token for character {character_id}")
            logger.debug(f"Adding auth for character {character_id}: {access_token[:10]}...")
            request.headers['Authorization'] = f'Bearer {access_token}'
        except Exception as e:
            logger.error(f"Error handling token for character {character_id}: {str(e)}")
            return

client.event_hooks['request'] = [add_auth_header]

# Load OpenAPI spec
openapi_spec_url = "https://esi.evetech.net/meta/openapi.json?compatibility_date=2025-08-26"
openapi_spec = httpx.get(openapi_spec_url).json()

# Create the MCP server
mcp = FastMCP.from_openapi(
    openapi_spec=openapi_spec,
    client=client,
    name="ESI MCP Server"
)

@mcp.tool
async def add_character(ctx: Context) -> str:
    """Add a new character by authenticating with EVE Online SSO."""
    logger.debug("Starting authentication")

    try:
        # Fetch OAuth server metadata
        async with httpx.AsyncClient() as temp_client:
            response = await temp_client.get(METADATA_URL)
            response.raise_for_status()
            oauth_metadata = response.json()
            auth_endpoint = oauth_metadata["authorization_endpoint"]

        # Build authorization URL (state is required by EVE SSO, PKCE not needed)
        state = secrets.token_urlsafe(32)
        _store_state(state)
        auth_params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": CALLBACK_URL,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        auth_url = f"{auth_endpoint}?{urlencode(auth_params)}"

        return (
            f"请在浏览器中打开以下链接授权：\n"
            f"{auth_url}\n\n"
            f"授权后浏览器会自动跳转到回调地址，内置的 8080 回调服务器将自动完成绑定，"
            f"你看到「绑定成功」页面即可关闭。\n"
            f"如果自动绑定失败，请把 URL 中的 code 参数值发给我手动调用 auth_with_code。"
        )
    except Exception as e:
        logger.error(f"Authentication failed: {e}", exc_info=True)
        return f"Authentication failed: {str(e)}"

@mcp.tool
async def auth_with_code(code: str) -> str:
    """Exchange an OAuth authorization code for tokens. Use this when you already have the code from the browser redirect."""
    logger.debug(f"auth_with_code: starting exchange")
    try:
        async with httpx.AsyncClient() as client:
            auth_str = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            resp = await client.post(
                "https://login.eveonline.com/v2/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CALLBACK_URL,
                },
                headers={"Authorization": f"Basic {auth_str}"},
            )
            if resp.status_code != 200:
                logger.error(f"Token exchange failed: {resp.text}")
                return f"Token exchange failed: {resp.text}"
            td = resp.json()
            at = td["access_token"]
            rt = td.get("refresh_token", "")
            ei = td.get("expires_in", 1199)

            # 从 JWT 中解析角色 ID，再额外请求一次 ESI 获取完整角色名
            parts = at.split(".")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
            sub = payload.get("sub", "")
            cid = int(sub.split(":")[-1]) if ":" in sub else 0
            cname = payload.get("name", "Unknown")
            cr = await client.get(f"https://esi.evetech.net/latest/characters/{cid}/",
                headers={"Authorization": f"Bearer {at}"})
            if cr.status_code == 200:
                cname = cr.json().get("name", cname)

            # 保存角色与 token，如果还没有默认角色则设为默认
            token_expiry = (datetime.utcnow() + timedelta(seconds=ei)).isoformat()
            save_character(cid, cname, " ".join(SCOPES), at, rt, token_expiry)
            if not get_default_character_id():
                set_default_character_id(cid)
            return f"Authenticated: {cname} (ID: {cid})"
    except Exception as e:
        logger.error(f"auth_with_code failed: {e}", exc_info=True)
        return f"Authentication failed: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")