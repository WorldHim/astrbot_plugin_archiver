# astrbot_plugin_archiver

AstrBot 插件：入典！回复一条消息即可收录其发送人与内容，并可随机调用典库中的典藏。

# 功能

- **入典**：回复一条消息并发送 `/入典`，将该消息的发送人（ID + 昵称）与内容（文本、图片，其他类型以占位符记录）存档进当前会话的典库。
- **来点典**：发送 `/来点典`，从当前会话典库中随机抽取一条典，以「发送人 + 内容」的形式发回（含图片）。

# 使用

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/入典` | `/收录`、`/存典` | 回复一条消息发送，将其收录进本会话典库 |
| `/来点典` | `/随机典`、`/来典` | 从本会话典库中随机调用一条典 |

> 指令受 AstrBot 唤醒前缀制约（默认 `/`），群聊中也可通过 `@Bot` 唤醒后发送。

示例：

```
用户A：今天天气真好（群里的某条消息）
用户B：（回复用户A的这条消息） /入典
Bot：已收录「用户A」的发言，本会话典库共 1 条

用户C：/来点典
Bot：「用户A」
    今天天气真好
```

# 配置

在 AstrBot WebUI 的插件配置中可调整：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `session_quote_limit` | `0` | 每个会话典库的收录上限（条），超出后自动淘汰最早的典；`0` 表示不限制 |
| `fallback_global` | `false` | 当前会话典库为空时，是否从所有会话的典库中随机抽取 |
| `image_compress_threshold_mb` | `2` | 收录图片超过该大小时自动压缩（MB），逐步重新编码并等比缩小直到达标；`0` 表示不压缩，始终保存原图 |

# 数据存储

- 典库以 JSON 形式保存在 `data/plugin_data/astrbot_plugin_archiver/quotes.json`，按会话（群聊/私聊）分组。写入采用「临时文件 + 原子替换」，并保留上一版备份（`quotes.json.bak`），主文件意外缺失/损坏时自动从备份恢复。
- 被收录的图片会**下载保存到本地** `data/plugin_data/astrbot_plugin_archiver/images/`，文件名取内容摘要（相同内容自动复用同一文件）；超过 `image_compress_threshold_mb` 的图片会自动压缩（带透明通道转 WebP、兜底 PNG，其余转 JPEG，必要时等比缩小），压缩后原图文件会被清理。动图（GIF 等）不做有损压缩以避免丢帧，原样保存。

# 说明

- 收录时通过消息的引用（Reply）组件获取被回复消息的发送人与内容。各平台适配器对引用消息的回填程度不同：aiocqhttp（QQ）等平台可完整取回；若平台未回填引用内容，将回退到引用解析出的纯文本，仍无法读取时会提示收录失败。
- 图片收录时若下载失败（如 URL 失效、网络异常），会回退为仅记录 URL，随机调用时再按 URL 发送。
- 随机调用时优先发送本地存档的图片，本地文件缺失（如被手动清理）时自动回退到 URL。
- 同一条消息（按消息 ID）在同一会话内不会被重复收录。
- 被收录消息中的表情、语音、视频、文件、合并转发等类型会分别以 `[表情]`、`[语音]`、`[视频]`、`[文件]`、`[转发消息]` 占位符记录。

# Supports

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
