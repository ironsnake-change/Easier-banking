# Easier-banking

面向个人与家庭场景的银行 AI 智能体工作台。项目采用单仓库结构，默认使用 Python 内置 SQLite，无需额外数据库或容器。

## 本地运行

1. 安装 Python 3.11 或更高版本。
2. 创建虚拟环境并安装依赖：

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. 启动服务：

   ```bash
   python -m uvicorn main:app --host 127.0.0.1 --port 8000
   ```

4. 浏览器访问 `http://127.0.0.1:8000`。数据库会在首次启动时自动创建并写入演示数据。

演示账户：

- 林砚：`linyan` / `Demo@2026`
- 张建国：`zhangjg` / `Demo@2026`

## 环境变量

- `DEMO_MODE=1`：仅供评测演示。只有该值启用时，提案响应中的 `demo_hint` 才会返回本次一次性验证码。生产环境必须关闭并接入真实短信、语音或验证器通道。
- `OTP_PEPPER`：一次性验证码哈希的服务端密钥；生产环境必须显式设置并妥善保管。
- `ACTION_PROPOSAL_TTL_SECONDS`：授权提案有效期，默认 120 秒。
- `LIMIT_OVERRIDE_TTL_SECONDS`：临时额度有效期，默认 900 秒。
- `EASIER_BANKING_GATEWAY_URL`：腾讯云 AI 网关地址。
- `EASIER_BANKING_GATEWAY_APP_SECRET`：腾讯云 AI 网关应用密钥。
- `DEEPSEEK_API_KEY`、`RELAY_BASE_URL`、`RELAY_API_KEY`：可选的二级模型通道。

## 安全设计

### 单一风险策略

`core/policy.py` 的 `classify(user_id, action)` 是风险分级的唯一来源：

- **L1**：冻结卡片、开启境外/夜间/免密代扣/ATM 锁等保护性操作。需要确认卡，无需验证码。
- **L2**：额度内且收款人已登记的转账，以及订阅、退订、资金口袋、理财申赎。需要确认卡。
- **L3**：超额转账、临时提额、首次向未登记收款人转账、解冻、关闭安全锁、提高卡片限额。需要确认卡和一次性验证码。

聊天路径、银行卡控制台及旧工具注册中心均调用同一策略。用户或模型自行提交的 `confirmed=true` 不再具有执行意义。

### 提案状态机

所有 AI 写操作先生成后端提案，默认 120 秒后过期：

```text
PENDING -> CONFIRMED -> EXECUTED
        -> CANCELLED
        -> EXPIRED
```

确认令牌仅以哈希形式存储并在提案响应中返回一次。参数动作会生成规范化 JSON 的 SHA-256 摘要。确认时同时校验当前用户、令牌、参数摘要、PENDING 状态与过期时间，并通过单条条件 UPDATE 抢占提案；只有更新行数为 1 的请求可以执行账务操作，因此重复或并发确认不会重复扣款。

### 一次性验证码与临时额度

- OTP 使用密码学安全随机数生成，服务端保存带 pepper 的哈希、过期时间和失败次数。
- OTP 缺失或错误都会计数；连续失败 3 次后提案作废。
- `DEMO_MODE=1` 时，响应中的 `demo_hint` 用于评委演示；其他情况不得返回明文验证码。
- L3 转账确认成功后写入 `limit_overrides`。有效额度等于用户基础额度加未过期临时额度，基础 `transfer_threshold` 不会被修改；临时额度到期后自动失效。
- 基础单日限额也不能通过 `/api/preferences` 直接调高；调高限额会生成 `limit_change` 提案，并按 L3 要求验证。
- 浏览器完全离线时的本地账本只用于演示与容灾，无法替代服务端信任边界；涉及真实资金或生产数据时必须连接已鉴权的后端服务。

### 审计与兼容

- 提案创建、确认、取消、过期、执行以及临时额度创建均写入 SHA-256 审计链。
- 旧的 `POST /api/assistant/action` 与 `POST /api/assistant/execute` 已返回 HTTP 410。
- 新流程为 `POST /api/assistant/propose` 与 `POST /api/assistant/confirm`。

请求示例：

```json
POST /api/assistant/propose
{
  "action": {
    "action": "transfer",
    "params": {"recipient": "房东陈姐", "amount": 300, "remark": "房租"}
  }
}
```

```json
POST /api/assistant/confirm
{
  "proposal_id": "PROP-...",
  "confirm_token": "server-issued-token",
  "otp": null,
  "cancel": false
}
```

## 测试

```bash
python -m pytest -q
```
