# Kiro Gateway 多租户测试报告

**测试时间**: 2026-04-23 08:45 ~ 09:00  
**测试人员**: AI Agent (Kiro CLI)  
**项目版本**: 2.4-dev.7  

---

## 环境信息

| 项目 | 值 |
|------|-----|
| OS | macOS |
| Python | 3.13.12 |
| 认证方式 | kiro-cli SQLite (`~/Library/Application Support/kiro-cli/data.sqlite3`) |
| 服务端口 | 9000（8000 已被占用） |
| 模型加载 | 18 个从 Kiro API 获取 + 1 个 hidden = 19 个 |
| 多租户数据库 | `data/test_tenants.db` |

## 部署步骤

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建 .env（使用本机 kiro-cli 凭证，启用多租户）
cat > .env << 'EOF'
PROXY_API_KEY="test-master-key-123"
KIRO_CLI_DB_FILE="~/Library/Application Support/kiro-cli/data.sqlite3"
SERVER_HOST="127.0.0.1"
SERVER_PORT="9000"
MULTI_TENANT_ENABLED=true
ADMIN_API_TOKEN="test-admin-token-456"
TENANT_DB_PATH="data/test_tenants.db"
LOG_LEVEL="INFO"
DEBUG_MODE=off
EOF

# 3. 后台启动
mkdir -p data
nohup python3 main.py --port 9000 > /tmp/kiro-gateway.log 2>&1 &
```

启动日志确认：
- ✅ Auth manager 初始化成功（auto-detected region: us-east-1）
- ✅ 模型缓存就绪（19 models）
- ✅ 多租户数据库初始化（`data/test_tenants.db`）
- ⚠️ 如使用假 token 会报 401，但不影响多租户管理功能（回退到 fallback 模型列表）

---

## 测试结果

### 基础功能

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 1 | 健康检查 `/health` | ✅ | 返回 `{"status": "healthy", "version": "2.4-dev.7"}` |
| 2 | 模型列表 `/v1/models` | ✅ | 租户 key 认证后返回 19 个模型 |

### 租户 Key 管理（Admin API）

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 3 | 创建 Key A（Team Alpha, $10, 60RPM） | ✅ | 生成 `sk-kiro-WShb...` |
| 4 | 创建 Key B（Team Beta, $0.001, 2RPM） | ✅ | 生成 `sk-kiro-FKQU...` |
| 5 | 列出所有 Key | ✅ | 返回 2 个 key 及完整元数据 |
| 6 | 查看 Key 详情 | ✅ | 包含 budget/used/enabled/last_used_at |
| 7 | 更新 Key 预算 | ✅ | Key B 预算从 $0.001 更新到 $100 |
| 8 | 禁用 Key | ✅ | `enabled=0` 后访问返回 401 |
| 9 | 重新启用 Key | ✅ | `enabled=1` 后恢复正常 |
| 10 | 删除 Key | ✅ | 删除后立即不可用（401） |
| 11 | 重置月度用量 | ✅ | `used_usd` 归零，`reset_count=2` |
| 12 | Admin API 认证保护 | ✅ | 无效 admin token 返回 403 |

### 认证与安全

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 13 | 有效租户 Key 访问 | ✅ | 正常返回数据 |
| 14 | 无效 Key 访问 | ✅ | 返回 401 |
| 15 | 无认证访问 | ✅ | 返回 401 |

### 实际模型调用

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 16 | OpenAI API 非流式调用 | ✅ | claude-haiku-4.5 正常回复，input=3020, output=151 |
| 17 | Anthropic API 调用（x-api-key） | ✅ | 返回含 thinking 块的完整响应 |

### 预算控制

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 18 | 用量记录 | ✅ | 精确记录 input/output tokens 和费用（$0.00302） |
| 19 | 超预算自动停止 | ✅ | Key B 预算 $0.001，一次请求后 used=$0.0028，后续返回 401 |

### 限流（Rate Limiting）

| # | 测试场景 | 结果 | 说明 |
|---|---------|:----:|------|
| 20 | 2 RPM 限流 | ✅ | 第 1 个请求通过，第 2、3 个返回 429 `"Retry after 3.5s"` |

---

## 发现的问题

### 🐛 Master Key 在多租户模式下不可用

**严重程度**: 中  
**位置**: `kiro/multi_tenant/tenant_auth.py:resolve_tenant()`

**现象**: README 声称 PROXY_API_KEY 在多租户模式下仍可作为 master key 使用，但实际使用 master key 访问 `/v1/models` 返回 401。

**原因**: `resolve_tenant()` 在租户数据库中找不到 key 时直接抛出 `HTTPException(401)`，没有返回 `None` 给调用方回退到 PROXY_API_KEY 检查。

```python
# tenant_auth.py 当前逻辑（有问题）
key_info = await tenant_db.validate_key(api_key_value)
if key_info is None:
    raise HTTPException(status_code=401, ...)  # ← 直接抛异常，master key 无法回退

# 期望逻辑
key_info = await tenant_db.validate_key(api_key_value)
if key_info is None:
    return None  # ← 返回 None，让 verify_api_key 回退到 PROXY_API_KEY 检查
```

**影响**: 多租户模式下只能用 `sk-kiro-*` 租户 key 访问 API，PROXY_API_KEY 作为 master key 失效。Admin API 不受影响（走独立认证）。

---

## 总结

多租户核心功能全部验证通过：Key 的 CRUD 管理、预算控制、限流、用量追踪、禁用/启用、双 API（OpenAI + Anthropic）支持均工作正常。发现 1 个 master key 回退的 bug，不影响租户 key 的正常使用。
