# HermesNotify · MoviePilot 通知转发插件

把 MoviePilot 的消息事件（订阅更新 / 下载完成 / 整理入库 / 插件通知等）直接推送到
**企业微信** 和 **QQ**，经 [Hermes Agent](https://hermes-agent.nousresearch.com) 网关转发。

零 LLM 消耗：走 Hermes webhook 平台的 `deliver_only` 路由，消息原文直投，不经过大模型。

## 工作原理

```
MoviePilot (NoticeMessage 事件)
   └─ HermesNotify 插件 ── HMAC-SHA256(V2) 签名 ──▶ Hermes 网关 /webhooks/<route>
                                                      ├─ deliver_only → 企业微信
                                                      └─ deliver_only → QQ
```

签名方案为 Hermes webhook 的通用 V2 协议：

- `X-Webhook-Timestamp`：unix 秒
- `X-Webhook-Signature-V2`：`HMAC-SHA256(secret, "<timestamp>.<raw body>")` 的 hex
- 防重放窗口 ±300s，body 逐字节签名（插件内直接序列化 UTF-8 字节后签名，无二次编码）

## 前置条件

Hermes 侧已启用 webhook 平台并创建 `deliver_only` 路由（详见 Hermes 文档）：

```bash
hermes config set platforms.webhook.enabled true
hermes config set platforms.webhook.extra.port 8644
hermes webhook subscribe moviepilot \
  --prompt "{title}
{content}" \
  --deliver wecom_callback --deliver-chat-id "<corp_id>:<user_id>" \
  --deliver-only
```

## 安装

MoviePilot → 插件市场 → 添加本仓库地址：

```
https://github.com/zf-ui/MoviePilot-Plugins
```

安装「Hermes 通知转发」后，在插件配置中填写：

| 字段 | 说明 | 示例 |
|---|---|---|
| 企业微信路由URL | Hermes webhook 路由地址 | `http://127.0.0.1:8644/webhooks/moviepilot` |
| 企业微信 HMAC Secret | 路由创建时输出的 secret | `xxxx…` |
| QQ路由URL | （可选）另一条路由 | `http://127.0.0.1:8644/webhooks/moviepilot-qq` |
| QQ HMAC Secret | （可选）该路由的 secret | `xxxx…` |
| 消息类型 | 留空 = 全部类型 | 按需勾选 |

两个通道独立生效：只填企微就只推企微；都不填则插件不工作。

## 注意

- Secret 属于敏感凭据，本插件不落盘以外的地方（仅存 MoviePilot 插件配置库），仓库中不含任何真实密钥。
- 企微接收方需先在 Hermes 完成配对授权（`hermes pairing approve wecom_callback <id>`）。
- 推送失败只写 MoviePilot 插件日志，不影响主流程。

## 开发者

`plugins.v3/hermesnotify/__init__.py`，单文件实现，无第三方依赖（requests 为 MoviePilot 内置）。
