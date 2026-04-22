# Kiro Gateway 多租户 & 凭据池 测试指南

## 前置准备

### 1. 配置环境变量

在 `.env` 文件中添加以下配置：

```env
# 多租户模式
MULTI_TENANT_ENABLED=true

# 管理员密码 —— 你自己编一个强密码，不是从外部获取的
# 跟 PROXY_API_KEY 一样，是你自定义的
ADMIN_API_TOKEN="test-admin-token-123"

# 租户数据库路径（默认即可）
TENANT_DB_PATH="data/tenants.db"

# （可选）启用凭据池 —— 多 Kiro 账号负载均衡
CREDENTIAL_POOL_ENABLED=true
CREDENTIAL_POOL_STRATEGY="round_robin"
```

### 2. 安装依赖并启动

```bash
pip install -r requirements.txt
python main.py
```

启动后日志中应包含：
- `Multi-tenant enabled (db: data/tenants.db)`
- `Credential pool enabled: 0 credentials, strategy=round_robin`（首次无凭据时为 0）

---

## Part 1: 多租户测试

以下假设网关运行在 `http://localhost:8000`，管理员 token 为 `test-admin-token-123`。

### Step 1: 创建租户 API Key

```bash
curl -s -X POST http://localhost:8000/admin/keys \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "测试团队A",
    "owner": "team-a@example.com",
    "budget_usd": 50,
    "rate_limit_rpm": 10,
    "rate_limit_tpm": 50000
  }' | python3 -m json.tool
```

**预期**: 返回 201，响应中包含 `"key": "sk-kiro-..."` 字段。请保存此 key 用于后续步骤。

### Step 2: 查看所有租户 Key

```bash
curl -s http://localhost:8000/admin/keys \
  -H "Authorization: Bearer test-admin-token-123" | python3 -m json.tool
```

**预期**: 返回 `keys` 数组，包含 Step 1 创建的 key。

### Step 3: 使用租户 Key 发起请求（OpenAI 格式）

将 `<TENANT_KEY>` 替换为 Step 1 返回的 key：

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <TENANT_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "messages": [{"role": "user", "content": "你好，请用一句话介绍自己"}],
    "stream": false
  }' | python3 -m json.tool
```

**预期**: 正常返回模型响应（与使用 PROXY_API_KEY 效果一致）。

### Step 4: 使用租户 Key 发起请求（Anthropic 格式）

```bash
curl -s http://localhost:8000/v1/messages \
  -H "x-api-key: <TENANT_KEY>" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Hello!"}]
  }' | python3 -m json.tool
```

**预期**: 正常返回 Anthropic 格式的响应。

### Step 5: 查询用量

```bash
curl -s "http://localhost:8000/admin/keys/<TENANT_KEY>/usage" \
  -H "Authorization: Bearer test-admin-token-123" | python3 -m json.tool
```

**预期**: 返回 `requests`、`total_input_tokens`、`total_output_tokens`、`total_cost_usd` 等字段，数值应大于 0。

### Step 6: 测试速率限制

快速连续发送超过 RPM 限制（10 次/分钟）的请求：

```bash
for i in $(seq 1 12); do
  echo "Request $i:"
  curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer <TENANT_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}], "stream": false}'
done
```

**预期**: 前 10 个请求返回 200，超出后返回 429（Rate limit exceeded）。

### Step 7: 更新租户配置

```bash
curl -s -X PUT http://localhost:8000/admin/keys/<TENANT_KEY> \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{"budget_usd": 100, "rate_limit_rpm": 30}' | python3 -m json.tool
```

**预期**: 返回更新后的 key 信息，`budget_usd` 为 100，`rate_limit_rpm` 为 30。

### Step 8: 禁用与删除

禁用 key：

```bash
curl -s -X PUT http://localhost:8000/admin/keys/<TENANT_KEY> \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{"enabled": 0}' | python3 -m json.tool
```

验证禁用后请求被拒绝（应返回 401）：

```bash
curl -s -w "\nHTTP %{http_code}\n" http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <TENANT_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]}'
```

删除 key：

```bash
curl -s -X DELETE http://localhost:8000/admin/keys/<TENANT_KEY> \
  -H "Authorization: Bearer test-admin-token-123" | python3 -m json.tool
```

**预期**: 返回 `{"deleted": true}`。

### Step 9: 验证 PROXY_API_KEY 仍可用

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer my-super-secret-password-123" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "Master key test"}], "stream": false}' \
  | python3 -m json.tool
```

**预期**: 正常返回，PROXY_API_KEY 作为超级密钥始终可用。

---

## Part 2: 凭据池测试

凭据池允许将多个 Kiro 账号注入网关，请求自动轮询分配。

### Step 10: 添加凭据到池

方式一 —— 使用 refresh token：

```bash
curl -s -X POST http://localhost:8000/admin/pool/credentials \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号A",
    "cred_type": "refresh_token",
    "refresh_token": "eyJhbGci...(你的refresh_token)...",
    "region": "us-east-1"
  }' | python3 -m json.tool
```

方式二 —— 使用 JSON 凭据文件：

```bash
curl -s -X POST http://localhost:8000/admin/pool/credentials \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号B",
    "cred_type": "creds_file",
    "creds_file": "~/.aws/sso/cache/kiro-auth-token.json"
  }' | python3 -m json.tool
```

方式三 —— 使用 kiro-cli SQLite 数据库：

```bash
curl -s -X POST http://localhost:8000/admin/pool/credentials \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "账号C",
    "cred_type": "sqlite_db",
    "sqlite_db": "~/Library/Application Support/kiro-cli/data.sqlite3"
  }' | python3 -m json.tool
```

> macOS 路径: `~/Library/Application Support/kiro-cli/data.sqlite3`
> Linux 路径: `~/.local/share/kiro-cli/data.sqlite3`

**预期**: 返回 201，`refresh_token` 字段被脱敏显示（只显示前 8 位）。

### Step 11: 查看池中所有凭据

```bash
curl -s http://localhost:8000/admin/pool/credentials \
  -H "Authorization: Bearer test-admin-token-123" | python3 -m json.tool
```

**预期**: 返回 `credentials` 数组、`active` 数量、`strategy` 字段。

### Step 12: 验证轮询生效

发送多个请求，观察网关日志中 `Pool: loaded credential #` 的切换：

```bash
for i in $(seq 1 5); do
  curl -s -o /dev/null -w "Request $i: HTTP %{http_code}\n" \
    http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer my-super-secret-password-123" \
    -H "Content-Type: application/json" \
    -d '{"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "pool test"}], "stream": false}'
done
```

查询凭据列表，`request_count` 应均匀递增（round_robin 模式）。

### Step 13: 禁用/删除池中凭据

```bash
# 禁用（停止接收新请求，不删除）
curl -s -X PUT http://localhost:8000/admin/pool/credentials/1 \
  -H "Authorization: Bearer test-admin-token-123" \
  -H "Content-Type: application/json" \
  -d '{"enabled": 0}' | python3 -m json.tool

# 删除
curl -s -X DELETE http://localhost:8000/admin/pool/credentials/1 \
  -H "Authorization: Bearer test-admin-token-123" | python3 -m json.tool
```

**预期**: 禁用后该凭据不再被选中；池空时自动回退到 `.env` 中的默认凭据。

---

## 常见问题

| 问题 | 解决方案 |
|------|---------|
| 启动报错 `ADMIN_API_TOKEN is not set` | 在 `.env` 中设置 `ADMIN_API_TOKEN`（自己编一个密码） |
| Admin API 返回 403 | 检查 `Authorization: Bearer` 头中的 token 是否与 `ADMIN_API_TOKEN` 一致 |
| 租户 Key 返回 401 | 确认 key 状态为 `enabled=1` 且未超预算 |
| 速率限制不生效 | 确认创建 key 时设置了 `rate_limit_rpm` 或 `rate_limit_tpm` |
| 凭据池报 `No upstream credentials` | 至少通过 Admin API 添加一条凭据，或关闭 `CREDENTIAL_POOL_ENABLED` 使用默认凭据 |
| macOS 找不到 kiro-cli 数据库 | 路径为 `~/Library/Application Support/kiro-cli/data.sqlite3`，不是 `~/.local/share/` |
| `ADMIN_API_TOKEN` 从哪获取？ | 不从任何地方获取，是你**自己编**的管理密码，跟 `PROXY_API_KEY` 一样 |
