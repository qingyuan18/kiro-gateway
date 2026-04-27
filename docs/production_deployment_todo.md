# Kiro Gateway 生产化部署改造 TODO

> 目标：支持 EKS 多 Pod 水平扩展部署，消除 SQLite 单点和本地文件依赖。

---

## 背景

当前架构的限制：

| 问题 | 原因 |
|------|------|
| 不能多 Pod 部署 | SQLite 是单写者，多 Pod 共享文件会锁冲突 |
| Pod 重启丢数据 | 数据存在容器本地文件系统 |
| Credential 文件路径不共享 | `creds_file` / `sqlite_db` 指向本地路径，Pod 之间不通 |
| Token 刷新冲突 | 多 Pod 同时刷新同一个 refresh token，后刷新的会失败 |

---

## 改造方案：SQLite → DynamoDB

### 表设计

**1. 租户表 `kiro-gw-tenants`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `api_key` (PK) | String | 租户 API Key，如 `sk-kiro-xxx` |
| `name` | String | 租户名称 |
| `owner` | String | 负责人 |
| `enabled` | Number | 1=启用 0=禁用 |
| `budget_usd` | Number | 预算上限 |
| `rate_limit_rpm` | Number | 每分钟请求数限制 |
| `rate_limit_tpm` | Number | 每分钟 token 数限制 |
| `total_cost_usd` | Number | 累计花费（原子更新） |
| `created_at` | String | 创建时间 |

**2. 用量日志表 `kiro-gw-usage`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `api_key` (PK) | String | 租户 API Key |
| `timestamp` (SK) | String | 请求时间 ISO 格式 |
| `model` | String | 使用的模型 |
| `input_tokens` | Number | 输入 token 数 |
| `output_tokens` | Number | 输出 token 数 |
| `cost_usd` | Number | 本次请求费用 |
| `ttl` | Number | DynamoDB TTL，自动过期清理（建议 90 天） |

**3. 凭据池表 `kiro-gw-credentials`**

| 字段 | 类型 | 说明 |
|------|------|------|
| `cred_id` (PK) | String | 凭据 ID |
| `name` | String | 账号名称 |
| `refresh_token` | String | Kiro refresh token |
| `access_token` | String | 当前有效的 access token |
| `token_expires_at` | String | access token 过期时间 |
| `region` | String | AWS 区域 |
| `enabled` | Number | 1=启用 0=禁用 |
| `request_count` | Number | 累计请求数（原子更新） |
| `last_used_at` | String | 最后使用时间 |

---

### Token 刷新冲突处理（乐观重试）

多 Pod 同时刷新同一个 credential 时，只有第一个成功，其他失败后重新读取即可：

```
Pod 需要 access_token：
1. 读 DDB → access_token 没过期 → 直接用
2. 过期了 → 用 refresh_token 调 OIDC 刷新
   2a. 成功 → 条件写回 DDB（ConditionExpression: refresh_token = :old_token）
       - 写成功 → 用新 token
       - 写失败（别人先写了）→ 重新读 DDB → 用别人刷新的 token
   2b. 失败（token 已被别人消费）→ 重新读 DDB → 用别人刷新的 token
```

不需要分布式锁，代价只是偶尔多一次失败的 OIDC 调用，可以接受。

---

## 改造步骤

### Phase 1：存储抽象层

- [ ] 定义存储接口 `StorageBackend`（抽象类），方法对齐现有 `TenantDatabase` 和 `CredentialPool`
- [ ] 将现有 SQLite 实现包装为 `SqliteBackend`（保证本地开发和单 Pod 部署仍然可用）
- [ ] 实现 `DynamoDBBackend`（用 aiobotocore 异步访问）
- [ ] 通过环境变量切换：`STORAGE_BACKEND=sqlite` 或 `STORAGE_BACKEND=dynamodb`

### Phase 2：Credential 存储迁移

- [ ] 凭据池的 refresh_token / access_token 存入 DynamoDB
- [ ] 不再依赖本地文件路径（`creds_file` / `sqlite_db` 模式在 EKS 上弃用，仅保留 `refresh_token` 模式）
- [ ] `KiroAuthManager` 的 token 刷新逻辑增加乐观重试：刷新失败后重新读 DDB

### Phase 3：租户和用量迁移

- [ ] 租户 CRUD 对接 DynamoDB
- [ ] 用量记录写入 DynamoDB（设置 TTL 自动清理历史数据）
- [ ] 费用累计使用 `UpdateItem` 原子递增（`ADD total_cost_usd :cost`）

### Phase 4：速率限制

- [ ] 当前内存中的 token bucket 在多 Pod 下不共享
- [ ] 方案 A（简单）：每个 Pod 独立限速，限额 = 总限额 / Pod 数（通过环境变量配置）
- [ ] 方案 B（精确）：用 DynamoDB 做滑动窗口计数器（写入频繁，成本较高）
- [ ] 建议先用方案 A，后续有需要再升级

### Phase 5：EKS 部署配置

- [ ] Dockerfile 优化（多阶段构建，非 root 用户）
- [ ] Kubernetes manifests（Deployment、Service、HPA）
- [ ] Pod 通过 IRSA（IAM Roles for Service Accounts）访问 DynamoDB，不需要 AK/SK
- [ ] 健康检查对接 `/health` 端点
- [ ] 环境变量通过 ConfigMap / Secrets 管理

---

## 环境变量（新增）

```env
# 存储后端：sqlite（默认，本地开发）或 dynamodb（生产）
STORAGE_BACKEND="sqlite"

# DynamoDB 配置（STORAGE_BACKEND=dynamodb 时必填）
DYNAMODB_REGION="us-east-1"
DYNAMODB_TABLE_PREFIX="kiro-gw"    # 表名前缀，最终表名如 kiro-gw-tenants

# Pod 数量（用于速率限制分摊，方案 A）
POD_COUNT=2
```

---

## 不需要改动的部分

- 路由层（routes_openai.py / routes_anthropic.py）— 不感知存储后端
- Failover 逻辑（pool_selector.py）— 只依赖 CredentialPool 接口
- 转换器、流式处理 — 与存储无关
- Admin API 路由 — 只需切换底层调用

---

## 风险和注意事项

| 风险 | 应对 |
|------|------|
| DynamoDB 成本 | 用量日志设 TTL 自动清理；使用 On-Demand 模式，按请求计费 |
| 本地开发体验退化 | 保留 SQLite 后端，本地默认用 SQLite，零配置启动 |
| 迁移期间数据丢失 | 写迁移脚本 SQLite → DynamoDB，上线前跑一次 |
| OIDC 刷新偶尔多一次失败调用 | 可接受，重试读 DDB 即可恢复，不影响用户请求 |
