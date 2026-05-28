# Eve Online ESI MCP Server

## Overview

This project implements a local MCP (Model Context Protocol) server for accessing the EVE Online ESI (EVE Swagger Interface) API. It uses the FastMCP library to create a proxy server based on the ESI OpenAPI specification, with built-in support for OAuth authentication via EVE Online's SSO. The server handles multiple characters, stores authentication tokens securely in a SQLite database, and automatically refreshes expired tokens. It's designed for developers and EVE Online enthusiasts who need programmatic access to ESI endpoints in a secure, multi-character setup.

Key technologies:
- Python 3.8+
- FastMCP for MCP server creation
- httpx for asynchronous HTTP requests
- SQLite for local token storage
- OAuth2 PKCE flow for secure authentication
![](https://img.unclemusclez.com/u/VHQHRy.gif)
## Features

- **Multi-Character Support**: Authenticate and manage multiple EVE Online characters, with tokens stored per character.
- **Automatic Token Refresh**: Tokens are refreshed on-demand when expired, using stored refresh tokens.
- **Dynamic Authorization**: Automatically adds Bearer tokens to ESI requests based on the character ID in the API path.
- **SSO Integration**: Easy character addition via browser-based OAuth flow.
- **OpenAPI-Driven**: Generated from the official ESI OpenAPI spec, ensuring compatibility with future API changes.
- **Logging**: Detailed debug logging to a file for troubleshooting.
- **Local Storage**: Tokens and character data stored in `~/.esi-mcp/characters.db`.

## Installation

1. **Prerequisites**:
   - Python 3.8 or higher.
   - Install required dependencies:
     ```
     pip install fastmcp httpx sqlite3 asyncio secrets base64 hashlib re logging pathlib urllib3
     ```
     Note: Some dependencies like `fastmcp` may require specific installation instructions; check the [FastMCP documentation](https://fastmcp.cloud/docs) for details.

2. **Clone the Repository**:
   ```
   git clone https://github.com/unclemusclez/EveOnlineMCP.git
   cd EveOnlineMCP
   ```

3. **Run the Script**:
   ```
   python esi.py
   ```
   This starts the MCP server in stdio transport mode. For other transports (e.g., HTTP), modify `mcp.run(transport="stdio")` accordingly.

## Usage

### Adding a Character
The server includes a tool `add_character` to authenticate new characters:
- Call the tool via the MCP interface (e.g., from a client).
- It opens a browser for EVE Online SSO login.
- After authentication, the character's details and tokens are saved to the database.

Example (pseudocode for client-side call):
```python
result = await mcp_client.add_character()
print(result)  # "Authenticated character: CharacterName (ID: 123456789)"
```
### Removing a Character
The server includes a tool `delete_character` to remove a character from the database:
- Call the tool via the MCP interface (e.g., from a client).
- It removes the character's details and tokens from the database.
- If the character is set as default, this setting is cleared.

Example (pseudocode for client-side call):
```python
result = await mcp_client.remove_character(character_id=123456789)
print(result)  # "Removed character: CharacterName (ID: 123456789)"
```

### Making ESI Requests
- The server proxies ESI endpoints, e.g., `/characters/{character_id}/wallet/`.
- Authentication is handled automatically based on the `character_id` in the path.
- Use a FastMCP client to interact with the server.

Example client usage:
```python
from fastmcp import Client

async with Client("stdio") as client:  # Or HTTP transport
    balance = await client.GetCharactersCharacterIdWallet(character_id=123456789)
    print(balance)
```

### Database Management
- Characters and tokens are stored in `~/.esi-mcp/characters.db`.
- Tables:
  - `characters`: Stores character_id, name, scopes, access_token, refresh_token, token_expiry.
  - `settings`: Stores defaults like `default_character_id`.

You can query the DB manually with SQLite tools for inspection.

### Logging
- Logs are written to `~/.esi-mcp/esi-mcp.log`.
- Set logging level in code if needed (default: DEBUG).

## Configuration

- **OAuth Constants**:
  - `CLIENT_ID` and `CLIENT_SECRET`: Replace with your EVE Online developer app credentials.
  - `SCOPES`: List of ESI scopes; customize as needed.
  - `CALLBACK_URL`: Local callback for SSO (default: http://localhost:8080/auth/callback).

- **Compatibility Date**: Set to "2025-08-26" for future-proofing; update as per ESI changes.

- **Database Path**: Customizable via `DB_PATH`.

## Troubleshooting

- **Token Errors**: Check logs for refresh failures; ensure CLIENT_ID is valid.
- **SSO Issues**: Verify browser opens and callback port (8080) is free.
- **Unauthorized (401)**: Ensure character is added and has required scopes (e.g., `esi-wallet.read_character_wallet.v1`).
- **No Tokens Found**: Run `add_character` tool first.

## Contributing

Contributions are welcome! Please fork the repo and submit pull requests for bug fixes or features. Ensure code follows PEP8 and includes tests where possible.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE.md) for details.

---

For more details on EVE Online ESI, visit the [official documentation](https://developers.eveonline.com/api-explorer). For FastMCP, refer to [FastMCP Docs](https://fastmcp.cloud/docs).

## Update Notes

- Updated `config.py.example`:
  - Changed `CALLBACK_URL` to a public callback endpoint that can be reverse-proxied to the local callback server.
  - Clarified that `CLIENT_SECRET` is still used in `auth_with_code` for Basic authentication.
- Updated `esi.py`:
  - Added a built-in local OAuth callback HTTP server that starts automatically in a daemon thread and listens on port `8080`.
  - Added support for `/authorize` to redirect to EVE SSO authorization and `/callback` to handle the authorization code callback.
  - Improved auth code handling: automatically exchange the code for tokens, fetch character info, save it to the database, and set the default character.
  - Added an `auth_with_code` tool function to allow manual login binding using the `code` parameter from the browser redirect.
  - Simplified dependency and auth logic by removing no longer used PKCE generation and old OAuth proxy code.
  - Added state validation for the OAuth callback flow.
  - Encrypted access and refresh tokens before saving to SQLite.
---

## T2 制造利润计算器

`t2_profit.py` — 自动计算 T2 物品制造利润的命令行工具。

### 功能

- 自动获取蓝图材料 (fuzzwork API)
- 自动获取 Jita 4-4 实时买卖价 (ESI)
- 自动获取星系制造成本指数 (ESI)
- 计算发明成功率与摊销
- 支持批量对比多个物品

### 用法

```bash
# 单个物品
python3 t2_profit.py "Wasp II"

# 多个物品对比
python3 t2_profit.py "425mm Railgun II" "Nova Rage" "Warden II"

# 自定义参数
python3 t2_profit.py --me 4 --runs 20 --system jita "Hornet II"

# 用买单收材料（更低估成本）
python3 t2_profit.py --buy-mode buy "425mm Railgun II"

# 不计算发明摊销
python3 t2_profit.py --no-invention "425mm Railgun II"

# JSON 输出
python3 t2_profit.py --json "Wasp II"
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--me` | 2 | 材料效率 |
| `--runs` | 10 | 制造次数 |
| `--system` | haajinen | 制造星系 (haajinen/jita/dodixie/rens/amarr) |
| `--buy-mode` | sell | `sell`=按卖价买材料, `buy`=按买单收材料 |
| `--no-invention` | - | 不计算发明摊销 |
| `--json` | - | JSON 格式输出 |

### 依赖

- Python 3.8+
- 标准库 (无额外依赖)

## EVE Corp Monitor

`eve_corp_monitor.py` — 定时抓取军团数据写入 Redis，供其他工具/机器人使用。

### 功能

- 军团机库物资按分库聚合
- Jita 4-4 常用物品实时买卖价
- 军团工业任务列表 (active + ready)
- 军团市场订单 (卖单 + 买单)
- 星系制造成本指数

### 配置

```bash
# 复制配置模板
cp config_corp_monitor.py.example config_corp_monitor.py

# 编辑填入你的实际值
# - Redis 连接信息
# - EVE SSO Client ID
# - 军团 ID / 角色 ID
# - 需要追踪的物品清单
```

### 依赖

```bash
pip install httpx redis cryptography
```

### 用法

```bash
# 直接运行
python3 eve_corp_monitor.py

# 配合 cron 每 15 分钟运行
# */15 * * * * cd /path/to/EveOnlineMCP && python3 eve_corp_monitor.py >> /var/log/eve-monitor.log 2>&1
```

### Redis Keys

```
eve:corp:monitor:assets:{Division}     — 机库物资按分库
eve:corp:monitor:market:prices         — 全量物品价格
eve:corp:monitor:market:items:{type_id}— 单物品快速查找
eve:corp:monitor:industry:jobs         — 工业任务列表
eve:corp:monitor:orders                — 市场订单
eve:corp:monitor:assets:summary        — 机库概览
eve:corp:monitor:cost_index:haajinen   — 星系成本指数
```
