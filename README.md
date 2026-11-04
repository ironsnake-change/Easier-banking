# Easier-banking 银行 AI 智能体

Easier-banking 是面向个人与家庭金融场景的银行 AI 智能体。用户可以用自然语言查询账户、分析账单、转账、管理银行卡、处理订阅代扣，或把资金锁定到专项口袋。系统把“模型理解”和“资金执行”分开，所有写操作都必须经过服务端风险分级、确认卡、一次性验证码和原子提案状态机。

## 核心能力

- 智能转账：联系人解析、重名消歧、中文金额、额度校验、反诈拦截与 Saga 补偿。
- 账单分析：收支趋势、分类统计、异常交易识别、月度与年度分析。
- 理财操作：产品比较、R1-R4 风险适配、申购、赎回、收益与持仓展示。
- 卡片管理：冻结、解冻、境外锁、夜间锁、免密代扣总闸、ATM 保护与限额。
- 订阅代扣：自动扫描、续费提醒、退订、恢复订阅、未知订阅拒绝伪造。
- 跨场景联动：专项资金口袋、生活服务订单、AA 分账、定时任务和 DAG 编排。
- 全龄适配：标准网银与长辈关怀模式；大字号、温和话术和语音播报。
- 交易追溯：电子回单、中文大写金额、SHA-256 哈希签名和四阶段执行时间轴。

## 权限分级

- L1 保护性开启：冻结卡片、开启境外/夜间/免密代扣/ATM 锁，只需一键确认。
- L2 常规资金动作：额度内已登记收款人转账、退订、口袋划拨、小额理财赎回，需要确认卡。
- L3 风险扩张：超额转账、临时提额、首次陌生收款人、解冻、关闭安全锁、提高限额、理财申购和大额赎回，需要确认卡和一次性 OTP。

AI 聊天不会直接执行写操作，而是生成 `action_proposals` 提案。确认令牌和参数分别哈希保存；确认接口使用单条条件 `UPDATE` 抢占，只有 `rowcount=1` 的请求能执行，因此并发点击不会重复扣款。

跨场景复合指令会进入真实 DAG：先执行只读账户核验，再逐节点处理专项资金锁定和外部生活服务订单。每个节点都调用统一风险策略，前端按顺序展示授权卡并刷新 DAG/SAGA 状态；取消后续节点时，已提交节点会逆序补偿回滚。

## 快速启动

环境要求：Python 3.11 或更高版本。项目使用 SQLite，无需 Docker 或外部数据库。

```powershell
cd Easier-banking
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 。首次启动会自动创建数据库并载入演示数据。

不要直接双击项目内的 `static/index.html`。该方式没有后端服务，系统会按安全策略进入只读离线状态，无法调用云端模型或执行资金操作。

## 演示账户

| 账户 | 登录账号 | 密码 | 特征 |
|---|---|---|---|
| 林砚 | `linyan` | `Demo@2026` | 标准模式、私人银行主力账户、尾号 8848 |
| 张建国 | `zhangjg` | `Demo@2026` | 长辈关怀模式、颐养账户、尾号 0921 |

## Windows 桌面版

主目录中的 `Easier-banking_桌面版.exe` 是单文件 Windows 应用，双击即启动：

- 自动启动本地 FastAPI 服务，并打开 Edge/Chrome 独立应用窗口。
- 运行时不需要安装 Python、Node.js、Docker 或数据库。
- 数据保存在 `%LOCALAPPDATA%\Easier-banking\bank_core.db`。
- 日志保存在 `%LOCALAPPDATA%\Easier-banking\desktop.log`。
- 浏览器窗口关闭后，本地服务同步退出。

桌面版未做商业代码签名，Windows 首次运行可能显示 SmartScreen。请核对随附的 SHA-256 文件后选择“更多信息 -> 仍要运行”。

## 模型与网关

默认路由顺序为：

```text
腾讯云 CloudBase GLM 主通道
  -> DeepSeek / OpenAI 兼容中转备用
  -> 本地确定性规则引擎
```

网关默认配置内置在 `core/config.py`，可通过 `.env` 或环境变量覆盖：

- `EASIER_BANKING_GATEWAY_URL`
- `EASIER_BANKING_GATEWAY_APP_SECRET`
- `EASIER_BANKING_CLOUD_RATE_LIMIT=20`
- `EASIER_BANKING_CLOUD_TIMEOUT_SECONDS=8`
- `EASIER_BANKING_CLOUD_FAILURE_THRESHOLD=3`
- `EASIER_BANKING_CLOUD_CIRCUIT_SECONDS=60`

网关密钥为空或使用 `YOUR_` 占位值时，应用自动切换本地规则模式，不发起网络请求。每个用户每分钟最多 20 次云端调用；8 秒超时；连续 3 次失败后熔断 60 秒。发送给模型的卡号只保留后四位，手机号、证件号和完整卡号会脱敏。

如果桌面版顶栏显示“模式：离线”，先进入“系统与偏好设置”，点击“立即探测网关”。该按钮会清除可能残留的旧本地模式并把引擎切换回云端；成功时显示“腾讯云上海节点 · ...ms · ONLINE”，失败时会展示后端返回的具体原因。

## 环境变量

应用自动读取项目根目录 `.env`，进程环境变量优先。

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `DEMO_MODE` | `1` | 演示模式；为 1 时返回演示 OTP 提示并开放演示重置接口 |
| `OTP_PEPPER` | 进程级随机值 | OTP 哈希 pepper；生产必须配置固定密钥 |
| `EASIER_BANKING_SESSION_TTL_SECONDS` | `28800` | 登录会话有效期，默认 8 小时 |
| `EASIER_BANKING_LOGIN_MAX_FAILURES` | `5` | 同一用户名或 IP 连续失败次数 |
| `EASIER_BANKING_LOGIN_LOCK_SECONDS` | `300` | 登录锁定时间 |
| `ACTION_PROPOSAL_TTL_SECONDS` | `120` | 授权提案有效期 |
| `LIMIT_OVERRIDE_TTL_SECONDS` | `900` | 临时提额有效期 |
| `EASIER_BANKING_DB_PATH` | 自动选择 | 指定 SQLite 文件路径 |

生产部署必须设置 `DEMO_MODE=0`，并接入真实短信、人脸或安全密钥验证。

## 安全设计

- 服务端唯一风险策略：`core/policy.py::classify`。
- 客户端传入的 `confirmed=true` 不构成授权。
- 旧 `/api/assistant/action`、`/api/assistant/execute` 已停用并返回 410。
- 提案状态机：`PENDING -> CONFIRMED -> EXECUTED / CANCELLED / EXPIRED`。
- 错误 OTP 三次后提案作废；`DEMO_MODE=0` 不返回明文 OTP。
- 所有提案创建、确认、取消、过期和执行写入 SHA-256 审计链。
- 输入层拦截越狱、伪造权限、绕过风控和泄露密钥请求。
- AST 沙箱限制导入、文件访问、动态执行和危险属性访问。
- 登录会话默认 8 小时过期；同一用户名或 IP 连续 5 次失败锁定 5 分钟。
- 演示重置 API 仅在 `DEMO_MODE=1` 可用，重置业务数据但不删除 `audit_logs`。

## 测试

在项目根目录运行：

```powershell
python -m pytest -q
```

当前基线：`123 passed`。覆盖旧接口 410、并发确认防重、越权与篡改、OTP 计数、临时额度到期、五类审计事件、32 条只读意图、天气/体育等合规拒答、L3 财富操作、人工接管工单、跨场景 DAG 完成与逆向补偿、敏感字符串扫描、TLS 网关探测和桌面启动器安全模式。

## 项目结构

```text
Easier-banking/
  core/                 配置、数据库、权限策略、AI、安全、沙箱、审计
  tools/                六场景工具、Saga 补偿和统一注册中心
  gateway/              CloudBase OpenAI 兼容网关源码与部署说明
  static/               银行工作台前端
  tests/                自动化测试
  main.py               FastAPI 应用与 API
  desktop_launcher.py   Windows 桌面启动器
  build_desktop.ps1     单文件 EXE 构建脚本
  requirements.txt      轻量 Python 依赖
```

## 已知边界

本项目是比赛演示系统，不声称已通过生产银行安全认证。真实上线还需接入银行核心账务、真实 KYC/AML、多机数据库、HSM 密钥托管、短信/人脸验证、企业代码签名和独立渗透测试。评委体验网关凭证是受限临时凭证，赛后失效。
