# Credential Pool 设置指南 — 多 Kiro 账号接入

## 原理

Kiro Gateway 支持将多个 Kiro 账号放入一个"池子"，请求自动轮流分配到不同账号，实现负载均衡和故障转移。

每个 Kiro 账号的核心凭据就是一个 **refresh token**，拿到它就能接入池子。

---

## 第一步：拿到 Refresh Token

有三种方式，选一种即可。

### 方式 A：从 kiro-cli 的 SQLite 数据库提取

kiro-cli 登录后会把 token 存在本地数据库里：

```
macOS:  ~/Library/Application Support/kiro-cli/data.sqlite3
Linux:  ~/.local/share/kiro-cli/data.sqlite3
```

数据库中 `auth_kv` 表的 `kirocli:odic:token` 存了一个 JSON，里面包含 `refresh_token`。

提取命令：

```bash
# macOS
sqlite3 ~/Library/Application\ Support/kiro-cli/data.sqlite3 \
  "SELECT value FROM auth_kv WHERE key = 'kirocli:odic:token';" \
  | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['refresh_token'])"

# Linux
sqlite3 ~/.local/share/kiro-cli/data.sqlite3 \
  "SELECT value FROM auth_kv WHERE key = 'kirocli:odic:token';" \
  | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['refresh_token'])"
```

会输出一串很长的字符串，这就是 refresh token。复制保存好。

> **数据库结构参考**：表 `auth_kv`，key 为 `kirocli:odic:token`，value 是 JSON，包含 `access_token`、`refresh_token`、`expires_at`、`region` 等字段。

### 方式 B：从 Kiro IDE 的缓存文件提取

Kiro IDE 登录后会生成 JSON 凭据文件：

```
~/.aws/sso/cache/kiro-auth-token.json
```

打开文件，找到 `refreshToken` 字段的值。

### 方式 C：抓包获取

用浏览器开发者工具或 Charles/mitmproxy 抓取 Kiro IDE 登录流程中的网络请求，在 token 响应中找到 `refreshToken` 字段。

---

## 第二步：多账号怎么办？

kiro-cli 同一时间只能登录一个账号，所以需要**登录一个、提取一个、再换下一个**：

```bash
# === 账号 A ===
kiro-cli login          # 用账号 A 的邮箱登录
# 提取 token（用上面方式 A 的命令），保存到某个地方
TOKEN_A="eyJhbGci...账号A的token..."

# === 账号 B ===
kiro-cli logout
kiro-cli login          # 用账号 B 的邮箱登录
TOKEN_B="eyJhbGci...账号B的token..."

# === 账号 C ===
kiro-cli logout
kiro-cli login          # 用账号 C 的邮箱登录
TOKEN_C="eyJhbGci...账号C的token..."

# 最后可以登回你常用的账号
kiro-cli logout
kiro-cli login          # 用你日常使用的账号登录
```

> **提示**：不同的 Kiro 账号需要不同的邮箱/AWS 账号。同一个邮箱登录多次拿到的是同一个账号。

---

## 第三步：开启 Credential Pool

在 `.env` 中启用：

```env
CREDENTIAL_POOL_ENABLED=true
CREDENTIAL_POOL_STRATEGY="round_robin"

# 多租户也要开（池子依赖 Admin API）
MULTI_TENANT_ENABLED=true
ADMIN_API_TOKEN="你自己编一个管理密码"
```

重启网关。

---

## 第四步：把 Token 注入池子

通过 Admin API 逐个添加：

```bash
ADMIN="你的ADMIN_API_TOKEN"
GW="http://localhost:8000"

# 添加账号 A
curl -X POST $GW/admin/pool/credentials \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号A-小明",
    "cred_type": "refresh_token",
    "refresh_token": "eyJhbGci...账号A的完整token..."
  }'

# 添加账号 B
curl -X POST $GW/admin/pool/credentials \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号B-小红",
    "cred_type": "refresh_token",
    "refresh_token": "eyJhbGci...账号B的完整token..."
  }'

# 也可以直接指向 kiro-cli 数据库文件（不用手动提取 token）
curl -X POST $GW/admin/pool/credentials \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号C-本机",
    "cred_type": "sqlite_db",
    "sqlite_db": "~/Library/Application Support/kiro-cli/data.sqlite3"
  }'
```

---

## 第五步：确认生效

```bash
# 查看池子里的所有账号
curl $GW/admin/pool/credentials \
  -H "Authorization: Bearer $ADMIN" | python3 -m json.tool
```

返回示例：

```json
{
  "credentials": [
    {"id": 1, "name": "账号A-小明", "refresh_token": "eyJhbGci...", "request_count": 0, "enabled": 1},
    {"id": 2, "name": "账号B-小红", "refresh_token": "eyJhbGci...", "request_count": 0, "enabled": 1}
  ],
  "active": 2,
  "strategy": "round_robin"
}
```

之后发请求，网关会自动在这些账号之间轮流分配。如果某个账号失效（401/403），会自动切换到下一个。

---

## 常见问题

| 问题 | 回答 |
|------|------|
| 一个邮箱能注册几个 Kiro 账号？ | 一个邮箱 = 一个账号，要多个账号就需要多个邮箱 |
| Token 会过期吗？ | 会，但网关会自动用 refresh token 去换新的 access token，无需手动操作 |
| 某个账号被封了怎么办？ | 网关会自动 failover 到池子里其他账号，对使用者无感知 |
| 池子空了会怎样？ | 自动回退到 `.env` 中配置的默认凭据（REFRESH_TOKEN / KIRO_CREDS_FILE） |
| `cred_type` 有哪些选项？ | `refresh_token`（直接填 token）、`creds_file`（JSON 文件路径）、`sqlite_db`（kiro-cli 数据库路径） |
