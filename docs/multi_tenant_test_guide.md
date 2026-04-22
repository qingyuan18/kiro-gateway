# Kiro Gateway 多租户功能测试指南

## 前置准备

### 1. 配置环境变量

在 `.env` 文件中添加以下配置：

```env
MULTI_TENANT_ENABLED=true
ADMIN_API_TOKEN="test-admin-token-123"
TENANT_DB_PATH="data/tenants.db"
```

### 2. 安装依赖并启动服务

```bash
pip install -r requirements.txt
python main.py
```

启动后日志中应包含 `Multi-tenant enabled (db: data/tenants.db)` 字样。

---

## 测试步骤

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

## 常见问题

| 问题 | 解决方案 |
|------|---------|
| 启动报错 `ADMIN_API_TOKEN is not set` | 在 `.env` 中设置 `ADMIN_API_TOKEN` |
| Admin API 返回 403 | 检查 `Authorization: Bearer` 头中的 token 是否与 `ADMIN_API_TOKEN` 一致 |
| 租户 Key 返回 401 | 确认 key 状态为 `enabled=1` 且未超预算 |
| 速率限制不生效 | 确认创建 key 时设置了 `rate_limit_rpm` 或 `rate_limit_tpm` |
