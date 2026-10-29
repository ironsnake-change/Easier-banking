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

应用会使用 Python 标准库自动读取项目根目录的 `.env`（若存在），进程环境变量优先。可参考 `.env.example`；其默认 `DEMO_MODE=1`，评委克隆后无需申请或配置任何大模型 Key。启动日志会明确打印当前 `demo_mode`、`gateway_mode` 与会话有效期。

## Windows 桌面版

构建完成后的单文件应用位于 `dist/Easier-banking.exe`，双击即可启动，无需安装 Python、Node.js 或 Docker。应用会自动启动本机 FastAPI 服务，并使用 Edge/Chrome 的独立应用窗口打开银行工作台。

- 持久化数据库：`%LOCALAPPDATA%\Easier-banking\bank_core.db`
- 启动与错误日志：`%LOCALAPPDATA%\Easier-banking\desktop.log`
- 桌面应用不会直接打开 `static/index.html`；所有登录、余额、提案和云端模型请求都必须经过内置后端。

需要重新构建时，在安装了 Python 3.11+ 的 Windows 环境运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_desktop.ps1
```

首次构建会把 PyInstaller 和运行依赖安装到本地 `.desktop_venv`，不会进入正式运行依赖或提交到 Git。

演示账户：

- 林砚：`linyan` / `Demo@2026`
- 张建国：`zhangjg` / `Demo@2026`

## 环境变量

### CloudBase 网关（可选）

云端网关源码位于 `gateway/`。它在 CloudBase 中把 OpenAI 兼容请求转发到智谱，并强制锁定模型。部署时必须在目标函数的“环境变量”中配置：

```text
ZHIPU_API_KEY=<智谱控制台生成的新 Key>
APP_SECRET=<与 Agent 的 EASIER_BANKING_GATEWAY_APP_SECRET 一致>
```

将函数执行超时设置为至少 20 秒，并把 HTTP 访问服务路径映射为 `/chat/completions`。不要把这些值写入 `.env.example`、页面或 Git。详细步骤见 `gateway/README.md`。

- `DEMO_MODE=1`：评委演示默认模式。只有该值启用时，提案响应中的 `demo_hint` 才会返回本次一次性验证码，界面显示“演示模式：验证码见提示”。生产环境必须设为 `0` 并接入真实短信、语音或验证器通道。
- `OTP_PEPPER`：一次性验证码哈希的服务端密钥；生产环境必须显式设置并妥善保管。
- `ACTION_PROPOSAL_TTL_SECONDS`：授权提案有效期，默认 120 秒。
- `LIMIT_OVERRIDE_TTL_SECONDS`：临时额度有效期，默认 900 秒。
- `EASIER_BANKING_GATEWAY_URL`：腾讯云 AI 网关地址。
- `EASIER_BANKING_GATEWAY_APP_SECRET`：可选覆盖评委体验网关密钥；留空或设为 `YOUR_` 开头的占位值时自动进入离线规则模式。
- `EASIER_BANKING_CLOUD_RATE_LIMIT`：每用户每分钟云端调用上限，默认 20。
- `EASIER_BANKING_CLOUD_TIMEOUT_SECONDS`：云端单次超时，默认且最高 8 秒。
- `EASIER_BANKING_CLOUD_FAILURE_THRESHOLD` / `EASIER_BANKING_CLOUD_CIRCUIT_SECONDS`：连续失败阈值与熔断时间，默认 3 次 / 60 秒。
- `EASIER_BANKING_SESSION_TTL_SECONDS`：登录会话有效期，默认 28800 秒（8 小时）。
- `EASIER_BANKING_LOGIN_MAX_FAILURES`、`..._FAILURE_WINDOW_SECONDS`、`..._LOCK_SECONDS`：登录连续失败锁定策略，默认 5 次、5 分钟窗口、锁定 5 分钟。
- `DEEPSEEK_API_KEY`、`RELAY_BASE_URL`、`RELAY_API_KEY`：可选的二级模型通道。

## 网关与隐私

评委体验使用的是部署在腾讯云上海节点的受限凭证，服务端已实施每用户 20 次/分钟限流、8 秒超时与连续失败熔断，仅用于聊天推理，赛后将作废。浏览器不会获得或保存该凭证。

发送给网关的上下文只包含账户状态、交易摘要、联系人和银行卡后四位等办理业务所需信息；手机号、证件号与完整卡号会在服务端脱敏。未配置网关时系统自动使用本地规则引擎，不影响登录、只读查询和受控提案流程。

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
- 后端服务不可达时，浏览器只展示上次同步的只读缓存；不得在浏览器本地生成授权、扣款、转账、锁卡或订阅变更。界面会明确显示“服务暂不可用，操作未执行”。

### 审计与兼容

- 提案创建、确认、取消、过期、执行以及临时额度创建均写入 SHA-256 审计链。
- 旧的 `POST /api/assistant/action` 与 `POST /api/assistant/execute` 已返回 HTTP 410。
- 新流程为 `POST /api/assistant/propose` 与 `POST /api/assistant/confirm`。
- `/api/system/reset-demo` 与 `/api/security/reset-breaker` 仅在 `DEMO_MODE=1` 可用；重置演示业务数据不会删除审计链，并要求重新登录。
- 登录会话默认 8 小时过期；同一用户名或 IP 连续 5 次失败后锁定 5 分钟。

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

当前基线：`117 passed`。覆盖两阶段授权、并发防重、OTP、临时额度、会话与登录限流、网关熔断、沙箱工具、合规边界、六场景工具及桌面启动器。
