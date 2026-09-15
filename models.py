"""语录与消息组件的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Quote:
    """一条被收录的语录。

    Attributes:
        id: 语录的唯一识别 ID(uuid)。
        session: 收录时所在会话(unified_msg_origin)。
        message_id: 被收录的原消息 ID(用于去重,平台未提供时为空串)。
        sender_id: 被收录消息发送者的 ID。
        sender_name: 被收录消息发送者的昵称。
        text: 被收录消息的纯文本内容(不支持展示的类型会转为占位符);
            被收录内容为聊天记录(合并转发)时为 "[聊天记录]" 占位,
            实际内容保存在 forward_nodes。
        images: 被收录消息中的图片,每项含 file/url 键。
        forward_nodes: 被收录内容为聊天记录(合并转发)时的结构化子消息
            列表,每项为 {"sender_id", "sender_name", "text", "images"};
            非聊天记录时为空列表。
        archived_by: 发起收录的用户 ID。
        archived_by_name: 发起收录的用户昵称。
        archived_at_ts: 收录时间戳(秒)。
    """

    id: str
    session: str
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    images: list[dict[str, str]] = field(default_factory=list)
    forward_nodes: list[dict[str, Any]] = field(default_factory=list)
    archived_by: str = ""
    archived_by_name: str = ""
    archived_at_ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Quote":
        """从 dict 还原;字段缺失/类型异常时回退默认值。"""
        images_raw = data.get("images") or []
        images: list[dict[str, str]] = []
        if isinstance(images_raw, list):
            for item in images_raw:
                if isinstance(item, dict):
                    # 保留 path(本地存档)/url/file(URL 回退)键,缺失的键不写入
                    images.append(
                        {
                            key: str(item.get(key) or "")
                            for key in ("path", "url", "file")
                            if item.get(key)
                        }
                    )

        forward_nodes_raw = data.get("forward_nodes") or []
        forward_nodes: list[dict[str, Any]] = []
        if isinstance(forward_nodes_raw, list):
            for item in forward_nodes_raw:
                if not isinstance(item, dict):
                    continue
                sub_images_raw = item.get("images") or []
                sub_images: list[dict[str, str]] = []
                if isinstance(sub_images_raw, list):
                    for img in sub_images_raw:
                        if isinstance(img, dict):
                            sub_images.append(
                                {
                                    key: str(img.get(key) or "")
                                    for key in ("path", "url", "file")
                                    if img.get(key)
                                }
                            )
                forward_nodes.append(
                    {
                        "sender_id": str(item.get("sender_id") or ""),
                        "sender_name": str(item.get("sender_name") or ""),
                        "text": str(item.get("text") or ""),
                        "images": sub_images,
                    }
                )

        return cls(
            id=str(data.get("id", "") or ""),
            session=str(data.get("session", "") or ""),
            message_id=str(data.get("message_id", "") or ""),
            sender_id=str(data.get("sender_id", "") or ""),
            sender_name=str(data.get("sender_name", "") or ""),
            text=str(data.get("text", "") or ""),
            images=images,
            forward_nodes=forward_nodes,
            archived_by=str(data.get("archived_by", "") or ""),
            archived_by_name=str(data.get("archived_by_name", "") or ""),
            archived_at_ts=float(data.get("archived_at_ts", 0.0) or 0.0),
        )