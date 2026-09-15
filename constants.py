"""语录插件常量。"""

from __future__ import annotations

# 插件版本(@register 与 metadata.yaml 的 version 保持一致)
PLUGIN_VERSION = "1.4.0"

# 插件唯一识别名(数据目录 data/plugin_data/{PLUGIN_NAME},与 metadata.yaml 的 name 保持一致)
PLUGIN_NAME = "astrbot_plugin_archiver"

# 支持自建合并转发(聊天记录)的平台适配器;其余平台发送 Nodes 组件会被忽略,自动回退文本
FORWARD_PLATFORMS = {"aiocqhttp", "satori"}

# ---------- 消息占位符 ----------

PLACEHOLDER_IMAGE = "[图片]"
PLACEHOLDER_VOICE = "[语音]"
PLACEHOLDER_VIDEO = "[视频]"
PLACEHOLDER_FILE = "[文件]"
PLACEHOLDER_FACE = "[表情]"
PLACEHOLDER_AT_UNKNOWN = "@有人"
PLACEHOLDER_FORWARD = "[转发消息]"
PLACEHOLDER_CHAT_RECORD = "[聊天记录]"
PLACEHOLDER_EMPTY = "[无内容]"
PLACEHOLDER_UNKNOWN_OWNER = "未知用户"

# ---------- 输出节点名 ----------

INFO_NODE_NAME = "语录档案"
RECEIPT_NODE_NAME = "保存成功"

# ---------- 默认配置 ----------

DEFAULT_UPLOAD_PERMISSION = "群员"
DEFAULT_DELETE_PERMISSION = "管理员"
DEFAULT_OWNER_DELETE = True
DEFAULT_USE_FORWARD = True
DEFAULT_IMAGE_COMPRESS_THRESHOLD_MB = 2
