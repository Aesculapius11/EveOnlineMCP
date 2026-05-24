# Eve Online ESI MCP 服务器

## 概览

本项目实现了一个本地 MCP（Model Context Protocol）服务器，用于访问 EVE Online 的 ESI（EVE Swagger Interface）API。它使用 FastMCP 库根据 ESI 的 OpenAPI 规范创建代理服务器，并内置通过 EVE Online SSO 的 OAuth 身份验证支持。服务器支持多角色管理，将认证令牌安全地存储在 SQLite 数据库中，并自动刷新过期令牌。它面向需要以安全的、多角色方式访问 ESI 端点的开发者和 EVE Online 爱好者。

主要技术：
- Python 3.8+
- FastMCP 用于 MCP 服务器创建
- httpx 用于异步 HTTP 请求
- SQLite 用于本地令牌存储
- OAuth2 PKCE 流程用于安全认证
![](https://img.unclemusclez.com/u/VHQHRy.gif)
## 功能

- **多角色支持**：为多个 EVE Online 角色进行身份验证和管理，令牌按角色存储。
- **自动刷新令牌**：过期时按需刷新令牌，使用已存储的刷新令牌。
- **动态授权**：根据 API 路径中的角色 ID 自动向 ESI 请求添加 Bearer 令牌。
- **SSO 集成**：通过浏览器 OAuth 流程轻松添加角色。
- **OpenAPI 驱动**：从官方 ESI OpenAPI 规范生成，确保与未来 API 更改兼容。
- **日志记录**：详细的调试日志写入文件，便于排查问题。
- **本地存储**：令牌和角色数据存储在 `~/.esi-mcp/characters.db`。

## 安装

1. **前提条件**：
   - Python 3.8 或更高版本。
   - 安装所需依赖：
     ```
     pip install fastmcp httpx sqlite3 asyncio secrets base64 hashlib re logging pathlib urllib3
     ```
     注意：像 `fastmcp` 这样的依赖可能需要特定的安装说明；请查阅 [FastMCP 文档](https://fastmcp.cloud/docs) 获取详细信息。

2. **克隆仓库**：
   ```
   git clone https://github.com/unclemusclez/EveOnlineMCP.git
   cd EveOnlineMCP
   ```

3. **运行脚本**：
   ```
   python esi.py
   ```
   这会以 stdio 传输模式启动 MCP 服务器。若要使用其他传输方式（例如 HTTP），请相应修改 `mcp.run(transport="stdio")`。

## 使用说明

### 添加角色
服务器包含 `add_character` 工具用于认证新角色：
- 通过 MCP 接口调用该工具（例如从客户端）。
- 它会打开浏览器进行 EVE Online SSO 登录。
- 认证完成后，角色信息和令牌会保存到数据库中。

客户端调用示例（伪代码）：
```python
result = await mcp_client.add_character()
print(result)  # "Authenticated character: CharacterName (ID: 123456789)"
```

### 删除角色
服务器包含 `delete_character` 工具用于从数据库中删除角色：
- 通过 MCP 接口调用该工具（例如从客户端）。
- 它会从数据库中删除角色信息和令牌。
- 如果该角色被设置为默认角色，则会清除该设置。

客户端调用示例（伪代码）：
```python
result = await mcp_client.remove_character(character_id=123456789)
print(result)  # "Removed character: CharacterName (ID: 123456789)"
```

### 发起 ESI 请求
- 服务器代理 ESI 端点，例如 `/characters/{character_id}/wallet/`。
- 认证会根据路径中的 `character_id` 自动处理。
- 使用 FastMCP 客户端与服务器交互。

客户端使用示例：
```python
from fastmcp import Client

async with Client("stdio") as client:  # 或 HTTP 传输
    balance = await client.GetCharactersCharacterIdWallet(character_id=123456789)
    print(balance)
```

### 数据库管理
- 角色和令牌存储在 `~/.esi-mcp/characters.db`。
- 数据表：
  - `characters`：存储 character_id、name、scopes、access_token、refresh_token、token_expiry。
  - `settings`：存储默认设置，例如 `default_character_id`。

可以使用 SQLite 工具手动查询数据库以进行检查。

### 日志记录
- 日志写入 `~/.esi-mcp/esi-mcp.log`。
- 如有需要，可在代码中设置日志级别（默认：DEBUG）。

## 配置

- **OAuth 常量**：
  - `CLIENT_ID` 和 `CLIENT_SECRET`：替换为您的 EVE Online 开发者应用凭证。
  - `SCOPES`：ESI 权限范围列表；根据需要自定义。
  - `CALLBACK_URL`：SSO 的本地回调地址（默认：`http://localhost:8080/auth/callback`）。

- **兼容性日期**：设置为 `2025-08-26` 以保持向前兼容；根据 ESI 更新进行调整。

- **数据库路径**：可通过 `DB_PATH` 自定义。

## 故障排除

- **令牌错误**：检查日志中的刷新失败信息；确保 `CLIENT_ID` 有效。
- **SSO 问题**：确认浏览器可以打开并且回调端口（8080）未被占用。
- **未授权 (401)**：确保已添加角色并具有所需权限范围（例如 `esi-wallet.read_character_wallet.v1`）。
- **未找到令牌**：请先运行 `add_character` 工具。

## 贡献

欢迎贡献！请 Fork 仓库并提交修复或功能的 Pull Request。确保代码符合 PEP8 并在可能的情况下包含测试。

## 许可证

本项目采用 MIT 许可证。详情请参见 [LICENSE](LICENSE.md)。

---

有关 EVE Online ESI 的更多信息，请访问 [官方文档](https://developers.eveonline.com/api-explorer)。有关 FastMCP 的信息，请参阅 [FastMCP 文档](https://fastmcp.cloud/docs)。

## 更新说明

- 更新 `config.py.example`：
  - 将 `CALLBACK_URL` 修改为“公开回调地址”，通过反向代理转发到本地回调服务器。
  - 明确 `CLIENT_SECRET` 在 `auth_with_code` 中仍会用于 Basic 认证。
- 更新 `esi.py`：
  - 新增内置的本地 OAuth 回调 HTTP 服务器，自动在模块加载时启动并监听 `8080` 端口。
  - 添加 `/authorize` 路径直接跳转 EVE SSO 授权页面，及 `/callback` 路径处理授权码回调。
  - 优化授权码处理流程：回调时自动交换令牌、获取角色信息、保存到数据库并设置默认角色。
  - 新增 `auth_with_code` 工具函数，支持手动提供浏览器回调中的 `code` 参数完成登录绑定。
  - 简化依赖与逻辑，去除不再使用的 PKCE 生成和旧 OAuth 代理代码。
  - 增加了 OAuth 回调的 `state` 校验，防止 CSRF 和重放。
  - 在写入 SQLite 前对 access token 和 refresh token 加密。