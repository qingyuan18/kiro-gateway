# Kiro Gateway 多租户功能集成方案
## 基于 One-Router(https://github.com/XtraVisionsAI/one-router.git) 的借鉴与改造

**文档日期**: 2026-04-22  
**作者**: JiaDe Wang  
**项目**: Kiro Gateway 商业化升级

---

## 📊 调研结果：Kiro Gateway vs One-Router 功能对比

### ❌ Kiro Gateway 目前的限制

| 功能 | Kiro Gateway | One-Router |
|------|-------------|-----------|
| 多用户管理 | ❌ 单一 PROXY_API_KEY | ✅ 多 API Key 管理 |
| 虚拟账号分账 | ❌ 无 | ✅ 完整支持 |
| 用量追踪 | ❌ 仅调试日志 | ✅ 数据库存储 + 查询API |
| 预算控制 | ❌ 无 | ✅ 每 Key 独立预算 + 自动停用 |
| 速率限制 | ❌ 无 | ✅ 每 Key 独立 RPM/TPM |
| 成本计算 | ❌ 无 | ✅ 自动计费 + 缓存差价 |
| Admin UI | ❌ 无 | ✅ 完整的 Web 管理界面 |
| 数据库 | ❌ 无状态 | ✅ SQLite/PostgreSQL/DynamoDB |

---

## ✅ 建议：将 One-Router 的多租户功能集成到 Kiro Gateway

### 核心价值

如果你要将 Kiro Gateway 商业化或多人使用，必须添加以下功能：

#### 1. 虚拟账号分账（Multi-tenancy）
- 为每个用户/团队创建独立的 API Key
- 独立计费、预算控制
- 防止互相干扰

#### 2. 用量追踪与计费
- 记录每个请求的 token 用量
- 区分 `cached_tokens` 和 `cache_write_tokens` 的价格
- 生成账单报告

#### 3. 预算与速率控制
- 设置每月预算上限（超额自动停用）
- 设置 RPM（请求/分钟）和 TPM（token/分钟）限制
- 防滥用

---

## 🎯 集成方案：从 One-Router 借鉴到 Kiro Gateway

### Phase 1: 数据库 + API Key 管理（核心）

#### 1.1 数据库设计（参考 one-router）

**API Keys 表**
```sql
CREATE TABLE api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,           -- 虚拟 API Key (sk-xxx)
  name TEXT,                          -- Key 名称/描述
  owner TEXT,                         -- 所属用户/团队
  budget_usd REAL,                    -- 月预算上限（美元）
  used_usd REAL DEFAULT 0,            -- 当月已用额度
  rate_limit_rpm INTEGER,             -- 每分钟请求限制
  rate_limit_tpm INTEGER,             -- 每分钟 token 限制
  enabled BOOLEAN DEFAULT 1,          -- 是否启用
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_used_at DATETIME
);
```

**Usage Logs 表**
```sql
CREATE TABLE usage_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  api_key TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  cost_usd REAL,                      -- 计算后的成本
  request_id TEXT,
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (api_key) REFERENCES api_keys(key)
);

CREATE INDEX idx_usage_api_key ON usage_logs(api_key);
CREATE INDEX idx_usage_timestamp ON usage_logs(timestamp);
```

#### 1.2 核心中间件

**认证中间件**（替代当前的单一 PROXY_API_KEY）
```javascript
async function authenticate(req, res, next) {
  const apiKey = req.headers['authorization']?.replace('Bearer ', '');
  
  // 查询数据库验证 Key
  const keyInfo = await db.get(
    'SELECT * FROM api_keys WHERE key = ? AND enabled = 1',
    [apiKey]
  );
  
  if (!keyInfo) {
    return res.status(401).json({ error: 'Invalid API key' });
  }
  
  // 检查预算
  if (keyInfo.budget_usd && keyInfo.used_usd >= keyInfo.budget_usd) {
    return res.status(429).json({ error: 'Budget exceeded' });
  }
  
  req.apiKeyInfo = keyInfo;
  next();
}
```

**计费中间件**（响应后记录用量）
```javascript
async function trackUsage(req, res, usage) {
  const { model, input_tokens, output_tokens, cached_tokens, cache_write_tokens } = usage;
  
  // 计算成本（参考 one-router 的定价逻辑）
  const cost = calculateCost(model, {
    input_tokens,
    output_tokens,
    cached_tokens,
    cache_write_tokens
  });
  
  // 记录用量
  await db.run(`
    INSERT INTO usage_logs 
    (api_key, model, input_tokens, output_tokens, cached_tokens, cache_write_tokens, cost_usd, request_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `, [
    req.apiKeyInfo.key,
    model,
    input_tokens,
    output_tokens,
    cached_tokens,
    cache_write_tokens,
    cost,
    req.id
  ]);
  
  // 更新已用额度
  await db.run(`
    UPDATE api_keys 
    SET used_usd = used_usd + ?, last_used_at = CURRENT_TIMESTAMP
    WHERE key = ?
  `, [cost, req.apiKeyInfo.key]);
}
```

---

### Phase 2: 速率限制

借鉴 One-Router 的 Token Bucket 算法：

```javascript
const rateLimiters = new Map(); // key -> { tokens, lastRefill }

async function rateLimitMiddleware(req, res, next) {
  const { key, rate_limit_rpm, rate_limit_tpm } = req.apiKeyInfo;
  
  if (!rate_limit_rpm && !rate_limit_tpm) {
    return next(); // 无限制
  }
  
  // 实现 Token Bucket 逻辑
  const limiter = rateLimiters.get(key) || initBucket(rate_limit_rpm, rate_limit_tpm);
  
  if (!limiter.allowRequest()) {
    return res.status(429).json({ 
      error: 'Rate limit exceeded',
      retry_after: limiter.retryAfter()
    });
  }
  
  next();
}
```

---

### Phase 3: 管理 API

**创建 API Key**
```http
POST /admin/keys
Authorization: Bearer <ADMIN_TOKEN>

{
  "name": "Team Alpha",
  "owner": "alpha@acme.com",
  "budget_usd": 100,
  "rate_limit_rpm": 60,
  "rate_limit_tpm": 100000
}
```

**查询用量**
```http
GET /admin/keys/:key/usage?start=2026-04-01&end=2026-04-30
Authorization: Bearer <ADMIN_TOKEN>
```

**响应示例**
```json
{
  "key": "sk-kiro-abc123",
  "period": "2026-04",
  "budget_usd": 100,
  "used_usd": 23.45,
  "requests": 1250,
  "tokens": {
    "input": 500000,
    "output": 150000,
    "cached": 300000
  }
}
```

---

## 🔧 技术实现细节

### 数据库选择

| 场景 | 推荐 |
|------|------|
| 单机部署 | SQLite (零配置) |
| 多实例/高并发 | PostgreSQL |
| 云原生 | DynamoDB (AWS) |

### 成本计算公式

参考 One-Router 的定价逻辑：

```javascript
function calculateCost(model, usage) {
  const pricing = {
    'claude-3.5-sonnet': {
      input: 3.00 / 1_000_000,    // $3/MTok
      output: 15.00 / 1_000_000,  // $15/MTok
      cached: 0.30 / 1_000_000,   // $0.3/MTok (10% of input)
      cache_write: 3.75 / 1_000_000 // $3.75/MTok (1.25x of input)
    }
  };
  
  const price = pricing[model] || pricing['claude-3.5-sonnet'];
  
  return (
    usage.input_tokens * price.input +
    usage.output_tokens * price.output +
    (usage.cached_tokens || 0) * price.cached +
    (usage.cache_write_tokens || 0) * price.cache_write
  );
}
```

---

## 📋 实施计划

### Week 1-2: 基础设施
- [ ] 集成数据库（SQLite 先行）
- [ ] 实现 API Key CRUD
- [ ] 认证中间件替换现有的 PROXY_API_KEY

### Week 3: 计费与追踪
- [ ] 用量记录中间件
- [ ] 成本计算模块
- [ ] 预算检查逻辑

### Week 4: 速率限制
- [ ] Token Bucket 实现
- [ ] RPM/TPM 限制
- [ ] 429 错误处理

### Week 5-6: 管理界面
- [ ] Admin API 端点
- [ ] 简单的 Web UI（可选）
- [ ] 用量报表导出

---

## 🎯 成功指标

1. **隔离性**: 每个 API Key 独立计费，互不影响
2. **可观测性**: 实时查看用量和成本
3. **可控性**: 预算超限自动停止，速率限制生效
4. **可扩展性**: 支持 100+ 虚拟账号同时使用

---

## 📚 参考资源

- **One-Router 源码**: [GitHub](https://github.com/openchatai/one-router)
- **Kiro Gateway 项目**: 当前代码库
- **定价参考**: Anthropic/OpenAI 官方定价页面

---

## ⚠️ 注意事项

1. **缓存成本差异**: 
   - Cached tokens (读取) = 10% 输入价格
   - Cache write tokens (写入) = 1.25x 输入价格

2. **月度重置**: 
   - 每月 1 号重置 `used_usd` 为 0
   - 或使用滚动 30 天窗口

3. **安全性**:
   - Admin API 必须有独立认证
   - API Key 生成使用加密随机数
   - 防止 Key 枚举攻击

---

**下一步**: 开始 Phase 1 的数据库设计与 API Key 管理实现。
