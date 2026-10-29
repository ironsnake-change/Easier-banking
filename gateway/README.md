# Easier-banking 网关修复版

## 本次故障的直接原因

对当前已部署地址做低 token 探测时，6 次短请求均在约 0.7～1.7 秒返回 `200`。
使用正常长度的问答请求时，4 次请求均在约 4.6 秒返回 CloudBase 的 HTTP `433`：

```json
{
  "ret_code": 200401,
  "ret_msg": "SW52b2tpbmcgdGFzayB0aW1lZCBvdXQgYWZ0ZXIgMyBzZWNvbmRz",
  "duration_in_ms": 3000
}
```

其中 `ret_msg` 经 Base64 解码为：

```text
Invoking task timed out after 3 seconds
```

所以探测显示 `ONLINE` 并不代表真实聊天可用：探测请求很短，能在 3 秒内结束；真实聊天一旦超过 3 秒，就会被 CloudBase 执行层截断。原网关代码没有为上游请求设置超时、取消或快速错误响应，因此无法在平台截断前主动降级。

`cloudbaserc.json` 中的 `timeout` 只有在使用该配置重新部署时才会生效。控制台创建的旧函数仍可能保留默认 3 秒超时，必须同时核对并修改云函数配置。

## 必须完成的部署修复

1. 在 CloudBase 控制台打开实际承载网关的 HTTP 云函数，将“执行超时时间”设为 **20 秒**（不要只改本地 JSON），保存后重新部署。
2. 将 `cloudbaserc.json` 中的 `envId` 和 `name` 改为实际环境 ID 与函数名。CLI 部署会使用其中 `timeout: 20`。
3. 在控制台环境变量中配置 `APP_SECRET`、`ZHIPU_API_KEY`；不要写入源码或提交 Git。
4. 轮换已经出现在旧压缩包中的智谱 API Key。旧 Key 不能再视为秘密。
5. 部署后先用短请求确认 `200`，再发送一段会产生 100～300 字回答的请求，确认不再出现 `433`。

CloudBase 平台若仍强制 3 秒响应头限制，应改用 CloudBase 云托管/支持长连接的运行方式，或开启上游流式输出；仅修改 Node 代码无法绕过平台自身执行超时。

## 网关行为

- 强制锁定 `GATEWAY_MODEL`，默认 `glm-4-flash`。
- 鉴权密钥和智谱密钥只从环境变量读取，使用常量时间比较。
- CloudBase 已完成对外路径和访问控制，函数内部不再二次校验路径或请求方法；避免平台改写 `req.url` 后误报 `404`。
- 所有响应携带 `X-Easier-Gateway-Version: 2.0.1`，部署后可据此确认代码是否真正更新。
- 默认在 7 秒内完成或主动返回 `504`，低于客户端 8 秒熔断阈值。
- 仅对连接重置和 `502/503/504` 做最多一次重试，避免盲目扩大调用量。
- 限制请求体大小、请求方法、路由和最大输出 token。
- 支持普通 JSON 与 `text/event-stream` 流式透传，正确保留上游状态码、`Content-Type` 和 `Retry-After`。
- 客户端断开时取消上游请求，避免函数继续占用并发额度。
- `/healthz` 只暴露配置是否齐全，不返回任何密钥。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `APP_SECRET` | 无 | 客户端 `X-App-Secret`，必须设置 |
| `ZHIPU_API_KEY` | 无 | 智谱真实 API Key，必须设置且不能提交 |
| `GATEWAY_MODEL` | `glm-4-flash` | 网关强制模型 |
| `UPSTREAM_DEADLINE_MS` | `7000` | 上游总截止时间，最大 7500ms |
| `MAX_COMPLETION_TOKENS` | `768` | 单次输出上限 |
| `PORT` | `9000` | HTTP 服务端口 |

## 本地验证

```bash
npm run check
APP_SECRET=test ZHIPU_API_KEY=your-real-key npm start
curl http://127.0.0.1:9000/healthz
```

带业务请求验证：

```bash
curl -sS -D - \
  -H 'Content-Type: application/json' \
  -H 'X-App-Secret: test' \
  -d '{"model":"ignored","messages":[{"role":"user","content":"只回复 OK"}],"max_tokens":8}' \
  http://127.0.0.1:9000/chat/completions
```

## 安全说明

`X-App-Secret` 会随客户端分发，只能降低随意调用，不能替代真正的用户鉴权。生产环境应轮换该值、增加服务端限流与来源控制，并对网关调用日志做脱敏。智谱 Key 只允许保存在 CloudBase 环境变量中。
