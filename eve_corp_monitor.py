#!/usr/bin/env python3
"""
EVE Corporation Monitor — 定时 cron 更新军团数据到 Redis
- 军团机库物资 (Corp Assets)
- 常用物品市场价格 (Jita Market Prices)
- 军团工业流水线 (Industry Jobs)
- 军团市场订单 (Corp Orders)
- 星系制造成本指数 (Cost Index)

用法:
  # 设置环境变量或创建 config.py (参考 config_corp_monitor.py.example)
  python3 eve_corp_monitor.py

频率控制: ESI 限速 ~100 req/s, 保守起见每批请求间隔 0.6-1.2s。
"""

import sys, json, time, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional
from pathlib import Path

import httpx

# ═══════════════════ CONFIG ═══════════════════════
# 优先从 config.py 读取，否则从环境变量读

try:
    from config_corp_monitor import (
        REDIS_HOST, REDIS_PORT, REDIS_PASS, REDIS_DB, REDIS_PREFIX,
        CLIENT_ID, CALLBACK_URL, DB_PATH, FERNET_KEY_PATH, COMPAT_DATE,
        CORP_ID, CHAR_MAIN, CHAR_MARKET,
        THE_FORGE, JITA_SYSTEM, JITA_STATION,
        HAAJINEN_SYSTEM, HAAJINEN_STATION,
        DIVISIONS, CORPSAG_CONTAINER, OFFICE_FOLDER,
        TRACKED_ITEMS, BUY_ITEMS, SELL_ITEMS,
    )
except ImportError:
    # ── Redis ──
    REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_PASS = os.environ.get("REDIS_PASS", "")
    REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
    REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "eve:corp:monitor")

    # ── EVE SSO ──
    CLIENT_ID = os.environ.get("EVE_CLIENT_ID", "")
    CALLBACK_URL = os.environ.get("EVE_CALLBACK_URL", "http://localhost:8080/callback")
    DB_PATH = os.path.expanduser(os.environ.get("EVE_DB_PATH", "~/.esi-mcp/characters.db"))
    FERNET_KEY_PATH = os.path.expanduser(os.environ.get("EVE_FERNET_KEY", "~/.esi-mcp/master.key"))
    COMPAT_DATE = os.environ.get("ESI_COMPAT_DATE", "2025-08-26")

    # ── Corp / Characters ──
    CORP_ID = int(os.environ.get("EVE_CORP_ID", "0"))
    CHAR_MAIN = int(os.environ.get("EVE_CHAR_MAIN", "0"))
    CHAR_MARKET = int(os.environ.get("EVE_CHAR_MARKET", "0"))

    # ── Locations ──
    THE_FORGE = 10000002
    JITA_SYSTEM = 30000142
    JITA_STATION = 60003760
    HAAJINEN_STATION = 60002317
    HAAJINEN_SYSTEM = 30001424

    # ── Divisions ──
    DIVISIONS = os.environ.get("EVE_DIVISIONS", "CorpSAG1,CorpSAG2,CorpSAG3,CorpSAG4,CorpSAG6,CorpSAG7,CorpDeliveries").split(",")
    CORPSAG_CONTAINER = int(os.environ.get("EVE_CORPSAG_CONTAINER", "0"))
    OFFICE_FOLDER = int(os.environ.get("EVE_OFFICE_FOLDER", "0"))

    # ── Items (empty = skip) ──
    TRACKED_ITEMS = {}
    BUY_ITEMS = set()
    SELL_ITEMS = set()


# Token decryption (from esi.py Fernet encryption)
try:
    from cryptography.fernet import Fernet
    def _decrypt_token(encrypted: str) -> str:
        if encrypted is None:
            return None
        try:
            key = Path(FERNET_KEY_PATH).read_bytes().strip()
            fn = Fernet(key)
            return fn.decrypt(encrypted.encode()).decode()
        except Exception:
            return encrypted  # plaintext fallback
except ImportError:
    def _decrypt_token(encrypted: str) -> str:
        return encrypted

BASE_ESI = "https://esi.evetech.net"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"


# ═══════════════════ ESI CLIENT ═══════════════════

class EsiClient:
    """ESI HTTP client with token management and rate-limit handling."""

    def __init__(self):
        self._token: Optional[str] = None
        self._client = httpx.Client(timeout=30, follow_redirects=True)

    def _load_token(self) -> str:
        if not os.path.exists(DB_PATH):
            raise SystemExit(f"DB not found: {DB_PATH}. Run auth first.")
        conn = __import__("sqlite3").connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key='default_character_id'").fetchone()
        if not row:
            raise SystemExit("No default character in DB. Run auth first.")
        cid = int(row[0])
        row = conn.execute(
            "SELECT access_token, refresh_token, token_expiry FROM characters WHERE character_id=?",
            (cid,)
        ).fetchone()
        conn.close()
        if not row:
            raise SystemExit(f"No token for character {cid}.")

        at_enc, rt_enc, expiry_str = row
        at = _decrypt_token(at_enc)
        rt = _decrypt_token(rt_enc)
        if expiry_str:
            expiry = datetime.fromisoformat(expiry_str).replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc):
                at = self._refresh_token(rt, cid)
        return at

    def _refresh_token(self, rt: str, cid: int) -> str:
        print("  [auth] Token expired, refreshing...")
        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": CLIENT_ID,
        })
        if resp.status_code != 200:
            raise SystemExit(f"Token refresh failed: {resp.text}")
        d = resp.json()
        at = d["access_token"]
        rt_new = d.get("refresh_token", rt)
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=d["expires_in"])).isoformat()
        conn = __import__("sqlite3").connect(DB_PATH)
        conn.execute("UPDATE characters SET access_token=?, refresh_token=?, token_expiry=? WHERE character_id=?",
                     (at, rt_new, expiry, cid))
        conn.commit()
        conn.close()
        print("  [auth] Token refreshed ✓")
        return at

    def get_token(self) -> str:
        if not self._token:
            self._token = self._load_token()
        return self._token

    def get(self, path: str, auth: bool = True, params: dict = None) -> Optional[dict | list]:
        headers = {"X-Compatibility-Date": COMPAT_DATE}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_token()}"

        for attempt in range(3):
            resp = self._client.get(f"{BASE_ESI}{path}", headers=headers, params=params or {})
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 304:
                return None
            elif resp.status_code == 420:
                wait = int(resp.headers.get("X-Esi-Error-Limit-Reset", "60"))
                print(f"  ⚠️  Rate limited! Waiting {wait}s...")
                time.sleep(wait)
            elif resp.status_code == 401:
                self._token = self._load_token()
                headers["Authorization"] = f"Bearer {self._token}"
            else:
                if resp.status_code == 404:
                    return None
                print(f"  ⚠️  ESI {resp.status_code} on {path}: {resp.text[:120]}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    return None
        return None

    def post(self, path: str, body: list, auth: bool = False) -> Optional[list]:
        headers = {"X-Compatibility-Date": COMPAT_DATE, "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_token()}"
        resp = self._client.post(f"{BASE_ESI}{path}", json=body, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        print(f"  ⚠️  POST {resp.status_code} on {path}: {resp.text[:120]}")
        return None

    def close(self):
        self._client.close()


# ═══════════════════ REDIS ════════════════════════

try:
    import redis as redis_module
except ImportError:
    redis_module = None


class RedisStore:
    def __init__(self):
        if redis_module is None:
            raise SystemExit("redis-py not installed. pip install redis")
        self.r = redis_module.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            password=REDIS_PASS, db=REDIS_DB,
            decode_responses=True
        )
        self.r.ping()

    def set_json(self, key: str, value, expire: int = 900):
        self.r.set(f"{REDIS_PREFIX}:{key}", json.dumps(value, ensure_ascii=False, default=str), ex=expire)

    def get_json(self, key: str):
        raw = self.r.get(f"{REDIS_PREFIX}:{key}")
        return json.loads(raw) if raw else None

    def hset_json(self, name: str, key: str, value, expire: int = 900):
        hkey = f"{REDIS_PREFIX}:{name}"
        self.r.hset(hkey, key, json.dumps(value, ensure_ascii=False, default=str))
        self.r.expire(hkey, expire)

    def hget_json(self, name: str, key: str):
        raw = self.r.hget(f"{REDIS_PREFIX}:{name}", key)
        return json.loads(raw) if raw else None

    def set_timestamp(self, key: str):
        self.r.set(f"{REDIS_PREFIX}:ts:{key}", datetime.now(timezone.utc).isoformat())


# ═══════════════════ CORP ASSETS ══════════════════

def fetch_corp_assets(esi: EsiClient, store: RedisStore):
    """Fetch all corp assets, group by division, write to Redis."""
    print(f"\n{'='*60}")
    print("  1. 军团机库物资 (Corp Assets)")
    print(f"{'='*60}")

    all_assets = []
    page = 1
    while True:
        data = esi.get(f"/latest/corporations/{CORP_ID}/assets/", params={"page": page})
        if data is None or not isinstance(data, list) or len(data) == 0:
            break
        all_assets.extend(data)
        print(f"  Page {page}: {len(data)} items")
        page += 1
        time.sleep(1.2)

    if not all_assets:
        print("  ❌ No assets returned")
        return

    print(f"  Total assets: {len(all_assets)}")

    type_ids = set(a["type_id"] for a in all_assets if "type_id" in a)
    names = resolve_names_batch(esi, list(type_ids))

    divisions = defaultdict(list)
    for a in all_assets:
        flag = a.get("location_flag", "Unknown")
        divisions[flag].append(a)

    for div_name in DIVISIONS:
        items_in_div = divisions.get(div_name, [])
        if not items_in_div:
            print(f"  [SKIP] {div_name} — empty")
            continue

        agg = defaultdict(lambda: {"qty": 0, "type_id": 0, "bp_copies": 0})
        for a in items_in_div:
            tid = a["type_id"]
            q = a.get("quantity", 1)
            if q == -1:
                agg[tid]["qty"] += 1
                agg[tid]["bp_copies"] += 1
            else:
                agg[tid]["qty"] += q
            agg[tid]["type_id"] = tid

        item_list = []
        for tid, info in sorted(agg.items(), key=lambda x: x[1]["qty"], reverse=True):
            entry = {"type_id": tid, "name": names.get(tid, f"UNKNOWN_{tid}"), "quantity": info["qty"]}
            if info["bp_copies"] > 0:
                entry["blueprint_copies"] = info["bp_copies"]
            item_list.append(entry)

        store.set_json(f"assets:{div_name}", item_list, expire=900)
        store.set_timestamp(f"assets:{div_name}")
        print(f"  [OK] {div_name}: {len(item_list)} unique items ({sum(i['quantity'] for i in item_list)} units)")

    for flag, items in divisions.items():
        if flag in DIVISIONS:
            continue
        agg = defaultdict(lambda: {"qty": 0, "type_id": 0, "bp_copies": 0})
        for a in items:
            tid = a["type_id"]
            q = a.get("quantity", 1)
            if q == -1:
                agg[tid]["qty"] += 1
                agg[tid]["bp_copies"] += 1
            else:
                agg[tid]["qty"] += q
            agg[tid]["type_id"] = tid
        item_list = [{"type_id": tid, "name": names.get(tid, f"UNKNOWN_{tid}"), "quantity": info["qty"]}
                     for tid, info in sorted(agg.items(), key=lambda x: x[1]["qty"], reverse=True)]
        store.set_json(f"assets:{flag}", item_list, expire=900)
        store.set_timestamp(f"assets:{flag}")
        print(f"  [OK] {flag}: {len(item_list)} items (uncategorized)")

    summary = {}
    for div_name in DIVISIONS:
        if div_name in divisions:
            summary[div_name] = {
                "unique_items": len(set(a["type_id"] for a in divisions[div_name])),
                "total_units": sum(a.get("quantity", 1) for a in divisions[div_name]),
            }
    store.r.delete(f"{REDIS_PREFIX}:assets:summary")
    for div_name, div_summary in summary.items():
        store.hset_json("assets:summary", div_name, div_summary, expire=900)
    print(f"  ✅ 机库数据已写入 Redis ({len(summary)} divisions)")


# ═══════════════════ MARKET PRICES ═════════════════

def fetch_market_prices(esi: EsiClient, store: RedisStore):
    """Fetch Jita market prices for all tracked items."""
    print(f"\n{'='*60}")
    print("  2. 常用物品市场价格 (Jita Market)")
    print(f"{'='*60}")

    if not TRACKED_ITEMS:
        print("  ⚠️ TRACKED_ITEMS 为空，跳过")
        return

    total = len(TRACKED_ITEMS)
    prices_data = {}

    for idx, (type_id, name) in enumerate(TRACKED_ITEMS.items(), 1):
        print(f"  [{idx}/{total}] {name} (type {type_id})...", end=" ")
        sys.stdout.flush()

        result = {"type_id": type_id, "name": name}

        sell_data = esi.get(
            f"/latest/markets/{THE_FORGE}/orders/", auth=False,
            params={"type_id": type_id, "order_type": "sell"}
        )
        time.sleep(0.6)

        if sell_data:
            jita_sells = [o for o in sell_data if o.get("system_id") == JITA_SYSTEM]
            if jita_sells:
                jita_sells.sort(key=lambda x: x["price"])
                result["sell_min"] = jita_sells[0]["price"]
                result["sell_top5"] = [{"price": o["price"], "volume": o["volume_remain"]}
                                        for o in jita_sells[:5]]
                result["sell_vol_20"] = sum(o["volume_remain"] for o in jita_sells[:20])
            else:
                result["sell_min"] = None
                result["sell_vol_20"] = 0

        if type_id in BUY_ITEMS:
            buy_data = esi.get(
                f"/latest/markets/{THE_FORGE}/orders/", auth=False,
                params={"type_id": type_id, "order_type": "buy"}
            )
            time.sleep(0.6)

            if buy_data:
                jita_buys = [o for o in buy_data if o.get("system_id") == JITA_SYSTEM]
                if jita_buys:
                    jita_buys.sort(key=lambda x: x["price"], reverse=True)
                    result["buy_max"] = jita_buys[0]["price"]
                    result["buy_top5"] = [{"price": o["price"], "volume": o["volume_remain"]}
                                           for o in jita_buys[:5]]
                    result["buy_vol_5"] = sum(o["volume_remain"] for o in jita_buys[:5])
                else:
                    result["buy_max"] = None

        prices_data[type_id] = result
        print(f"✓ sell={result.get('sell_min', 'N/A')} buy={result.get('buy_max', '-')}")
        sys.stdout.flush()
        time.sleep(0.3)

    store.set_json("market:prices", prices_data, expire=900)
    store.set_timestamp("market:prices")
    for type_id, data in prices_data.items():
        store.hset_json("market:items", str(type_id), data, expire=900)
    print(f"  ✅ {len(prices_data)} items' prices written to Redis")


# ═══════════════════ INDUSTRY JOBS ════════════════

def fetch_industry_jobs(esi: EsiClient, store: RedisStore):
    """Fetch corporation industry jobs (active + ready)."""
    print(f"\n{'='*60}")
    print("  3. 军团工业流水线 (Industry Jobs)")
    print(f"{'='*60}")

    jobs = esi.get(f"/latest/corporations/{CORP_ID}/industry/jobs/")
    if not jobs:
        print("  ❌ No jobs returned")
        return

    active = [j for j in jobs if j.get("status") in ("active", "ready")]
    if not active:
        print("  📭 No active/ready jobs")
        store.set_json("industry:jobs", [], expire=900)
        return

    type_ids = list(set(j["product_type_id"] for j in active))
    names = resolve_names_batch(esi, type_ids)
    blueprint_ids = [j.get("blueprint_type_id") for j in active if j.get("blueprint_type_id")]
    bpo_names = resolve_names_batch(esi, list(set(blueprint_ids)))

    activities = {1: "manufacturing", 3: "te_research", 4: "me_research", 5: "copying", 8: "invention"}
    activity_cn = {1: "制造", 3: "TE研究", 4: "ME研究", 5: "拷贝", 8: "发明"}

    now = datetime.now(timezone.utc)
    result_jobs = []
    for j in active:
        end = datetime.fromisoformat(j["end_date"].replace("Z", "+00:00"))
        if end < now:
            status, status_cn = "completed", "✅ 已完成"
            remaining_sec = 0
        else:
            remaining_sec = (end - now).total_seconds()
            h = int(remaining_sec / 3600)
            status = "running"
            status_cn = f"🕐 ~{h}h" if h < 24 else f"🕐 ~{h//24}天{h%24}h"

        a_id = j.get("activity_id", 1)
        result_jobs.append({
            "job_id": j.get("job_id"),
            "activity_id": a_id,
            "activity": activities.get(a_id, "unknown"),
            "activity_cn": activity_cn.get(a_id, "未知"),
            "product_type_id": j["product_type_id"],
            "product_name": names.get(j["product_type_id"], f"type_{j['product_type_id']}"),
            "blueprint_type_id": j.get("blueprint_type_id"),
            "blueprint_name": bpo_names.get(j.get("blueprint_type_id"), ""),
            "runs": j.get("runs", 1),
            "status": status,
            "status_cn": status_cn,
            "end_date": j.get("end_date"),
            "remaining_seconds": remaining_sec,
            "probability": j.get("probability"),
        })

    store.set_json("industry:jobs", result_jobs, expire=900)
    store.set_timestamp("industry:jobs")

    for j in result_jobs:
        print(f"  [{j['activity_cn']}] {j['product_name']} ×{j['runs']} — {j['status_cn']}")

    count_done = sum(1 for j in result_jobs if j["status"] == "completed")
    print(f"  ✅ 共 {len(result_jobs)} 任务 ({count_done} 已完成) → Redis")


# ═══════════════════ CORP ORDERS ══════════════════

def fetch_corp_orders(esi: EsiClient, store: RedisStore):
    """Fetch corp market orders (sell + buy)."""
    print(f"\n{'='*60}")
    print("  4. 军团市场订单 (Corp Orders)")
    print(f"{'='*60}")

    orders = esi.get(f"/latest/corporations/{CORP_ID}/orders/")
    if not orders:
        print("  ❌ No orders returned")
        return

    sells = [o for o in orders if not o.get("is_buy_order")]
    buys = [o for o in orders if o.get("is_buy_order")]
    type_ids = list(set(o["type_id"] for o in orders))
    names = resolve_names_batch(esi, type_ids)

    sell_items = [{
        "type_id": o["type_id"], "name": names.get(o["type_id"], f"type_{o['type_id']}"),
        "price": o["price"], "volume_remain": o["volume_remain"], "volume_total": o["volume_total"],
        "total_value": o["price"] * o["volume_remain"],
    } for o in sells]

    buy_items = [{
        "type_id": o["type_id"], "name": names.get(o["type_id"], f"type_{o['type_id']}"),
        "price": o["price"], "volume_remain": o["volume_remain"], "volume_total": o["volume_total"],
        "escrow": o.get("escrow", 0),
    } for o in buys]

    sell_total_value = sum(s["total_value"] for s in sell_items)
    buy_escrow_total = sum(b["escrow"] for b in buy_items)

    result = {
        "sell_orders": sell_items, "buy_orders": buy_items,
        "summary": {
            "sell_count": len(sell_items), "sell_total_value": sell_total_value,
            "buy_count": len(buy_items), "buy_escrow_total": buy_escrow_total,
        }
    }
    store.set_json("orders", result, expire=900)
    store.set_timestamp("orders")
    print(f"  📤 卖单: {len(sell_items)} 项, 总价值 {sell_total_value:,.0f} ISK")
    print(f"  📥 收单: {len(buy_items)} 项, 押金 {buy_escrow_total:,.0f} ISK")
    print(f"  ✅ 订单数据已写入 Redis")


# ═══════════════════ COST INDEX ═══════════════════

def fetch_cost_index(esi: EsiClient, store: RedisStore):
    """Fetch system manufacturing cost index."""
    print(f"\n{'='*60}")
    print("  5. 星系制造成本指数")
    print(f"{'='*60}")

    data = esi.get("/latest/industry/systems/")
    if not data:
        print("  ❌ 无法获取成本指数")
        return

    for system in data:
        if system.get("solar_system_id") == HAAJINEN_SYSTEM:
            cost_indices = system.get("cost_indices", [])
            result = {ci["activity"]: {"cost_index": ci["cost_index"]} for ci in cost_indices}
            store.set_json("cost_index:haajinen", result, expire=900)
            store.set_timestamp("cost_index:haajinen")
            mfg_idx = result.get("manufacturing", {}).get("cost_index", "?")
            print(f"  ✅ Haajinen manufacturing cost index: {mfg_idx}")
            return

    print(f"  ⚠️ 未找到目标星系 (system {HAAJINEN_SYSTEM})")


# ═══════════════════ HELPERS ══════════════════════

def resolve_names_batch(esi: EsiClient, type_ids: list) -> dict:
    if not type_ids:
        return {}
    unique = list(set(type_ids))
    chunks = [unique[i:i+1000] for i in range(0, len(unique), 1000)]
    result = {}
    for chunk in chunks:
        data = esi.post("/latest/universe/names/", body=chunk)
        if data:
            for item in data:
                result[item["id"]] = item["name"]
        time.sleep(0.6)
    return result


# ═══════════════════ MAIN ════════════════════════

def main():
    start = time.time()
    print(f"🚀 EVE Corp Monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"   Corp ID: {CORP_ID}")
    print(f"   Redis: {REDIS_HOST}:{REDIS_PORT}")
    print()

    try:
        store = RedisStore()
        print("✅ Redis 连接正常")
    except Exception as e:
        print(f"❌ Redis 连接失败: {e}")
        sys.exit(1)

    esi = EsiClient()
    try:
        token = esi.get_token()
        print(f"✅ ESI Token 有效 (len={len(token)})")

        fetch_corp_assets(esi, store)
        fetch_market_prices(esi, store)
        fetch_industry_jobs(esi, store)
        fetch_corp_orders(esi, store)
        fetch_cost_index(esi, store)

    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        esi.close()

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"  ✅ 全部完成! 耗时 {elapsed:.0f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
