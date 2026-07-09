# WeChat 接入方案 (iLink)

mybot 通过 iLink bot API (`mybot-wechat`) 接入微信，提供独立的 mybot 好友身份，符合原始需求——**在微信中有一个独立的 "mybot" 好友，通过跟它对话来触发 bot 执行**。

### 原理

```
用户微信 → 扫码授权 → iLink 平台发放 bot_token
                            ↓
mybot-wechat ← HTTP 长轮询 ← ilinkai.weixin.qq.com
     ↓
MessageBus → Orchestrator → LLM
     ↓
HTTP sendmessage → iLink 平台 → 用户微信
```

### 协议来源

基于 `@tencent-weixin/openclaw-weixin` v1.0.3 逆向,非官方文档。接口:
- `get_bot_qrcode` — 获取登录二维码
- `get_qrcode_status` — 轮询扫码状态
- `getupdates` — HTTP 长轮询拉取消息
- `sendmessage` — 发送消息
- `getconfig` — 刷新 context_token / typing_ticket
- `getuploadurl` — 获取媒体上传 URL
- `sendtyping` — 发送输入状态

### 关键细节

1. **context_token 90s 过期**: 用户发消息时 iLink 下发 `context_token`,回复时必须回传。该 token 服务端约 90s 过期,定时推送 (如海龟汤 20:00) 前需先 `getconfig` 刷新,否则静默丢消息。目前 Phase 1 未实现自动刷新,连续对话中没问题。

2. **X-WECHAT-UIN**: 每次请求生成新的随机 uint32 → base64,不能复用。

3. **状态持久化**: token、cursor、context_tokens 存在 `{workspace}/wechat/account.json`,重启自动恢复。

4. **消息去重**: 通过 message_id 做 1000 条 LRU 去重,防止重复处理。

5. **会话过期**: errcode -14 时暂停轮询 1 小时。

### 使用

```bash
mybot-wechat
# 终端显示二维码 → 微信扫码确认
# 后续启动自动加载 account.json,无需重新扫码
```

### 实现阶段

**Phase 1 (已完成)**: 核心文本通道
- QR 码登录 + 状态持久化
- HTTP 长轮询拉取消息
- 文本消息解析与发送
- MessageBus 集成
- 引用消息解析
- 用户 allowlist
- 会话过期处理

**Phase 2 (待实现)**: 媒体支持
- 图片/语音/视频/文件下载 + AES-128-ECB 解密
- 媒体文件上传 + AES 加密 + CDN 发送

**Phase 3 (待实现)**: 可靠性增强
- context_token 自动刷新 (防 90s 过期)
- 输入状态指示 (typing indicator)
- tool hints 缓冲合并

## 架构: MessageBus 解耦

```
外部消息 → WechatChannel._on_message()
  → _parse() → ChannelMessage (归一化)
  → MessageBus.inbound(session_key)
  → Orchestrator.serve(bus, session_key)
  → MessageBus.outbound("wechat")
  → WechatChannel._consume_outbound()
  → iLink sendmessage
```

`channels/base.py` 定义统一接口 (`BaseChannel` ABC + `ChannelMessage` 归一化模型)，新增通道只需实现 `start()` / `shutdown()` / `send_reply()` 三个方法。
