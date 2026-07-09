# WeChat 接入方案

mybot 提供两种微信接入方式，分别适用于不同场景。

## 方案对比

| | wechat (itchat) | weixin (iLink) |
|---|---|---|
| **入口** | `mybot-wechat` | `mybot-weixin` |
| **文件** | `channels/wechat.py` | `channels/weixin.py` |
| **协议** | itchat-uos (Web WeChat 模拟) | iLink HTTP 长轮询 (bot API) |
| **bot 身份** | 登录的微信号本身就是 bot | 独立 bot 身份 (`@im.bot`) |
| **封号风险** | 高 (个人号自动化) | 低 (官方 bot 接口) |
| **挂机要求** | 需要手机/电脑保持登录 | 无需挂机,纯 HTTP |
| **独立好友** | 否 (登录哪个号,哪个号就是 bot) | 是 (扫码授权,独立 mybot 好友) |
| **群聊** | 支持 @ 触发 | 不支持 (bot API 限制) |
| **媒体** | 图片/文件 (itchat 内置) | 图片/语音/视频/文件 (AES 加解密) |
| **稳定性** | 依赖 itchat 逆向,易被封 | 半官方接口,较稳定 |
| **实现状态** | 完整 (含小红书发布回调) | Phase 1: 纯文本 (媒体待移植) |

## 推荐方案: iLink (`mybot-weixin`)

符合用户的原始需求——**在微信中有一个独立的 "mybot" 好友,通过跟它对话来触发 bot 执行**。

### 原理

```
用户微信 → 扫码授权 → iLink 平台发放 bot_token
                            ↓
mybot-weixin ← HTTP 长轮询 ← ilinkai.weixin.qq.com
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

3. **状态持久化**: token、cursor、context_tokens 存在 `{workspace}/weixin/account.json`,重启自动恢复。

4. **消息去重**: 通过 message_id 做 1000 条 LRU 去重,防止重复处理。

5. **会话过期**: errcode -14 时暂停轮询 1 小时。

### 使用

```bash
mybot-weixin
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

## 备用方案: itchat (`mybot-wechat`)

基于 itchat-uos 模拟 Web WeChat 登录,直接接管一个微信号。

### 适用场景

- 需要群聊 @ 触发
- 已有小号可以登录
- 临时测试

### 限制

- 登录的号本身就是 bot,没有独立身份
- 登录主号则无法与 bot 独立对话
- Web WeChat 协议不稳定,有封号风险
- 需要保持登录状态

### 使用

```bash
mybot-wechat
```

## 架构: MessageBus 解耦

两个通道使用相同的 MessageBus 模式与 Orchestrator 解耦:

```
外部消息 → ChannelAdapter._on_message()
  → _parse() → ChannelMessage (归一化)
  → MessageBus.inbound(session_key)
  → Orchestrator.serve(bus, session_key)
  → MessageBus.outbound("weixin"|"wechat")
  → ChannelAdapter._consume_outbound()
  → 平台发送
```

`channels/base.py` 定义统一接口 (`BaseChannel` ABC + `ChannelMessage` 归一化模型),新增通道只需实现 `start()` / `shutdown()` / `send_reply()` 三个方法。
