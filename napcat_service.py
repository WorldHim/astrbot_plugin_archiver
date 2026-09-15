"""NapCat/OneBot v11 接口调用封装。

仅处理接口调用与响应解包;业务解析与数据组装在 quote_service 中。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger


class NapcatService:
    """封装插件用到的 OneBot v11 接口调用。"""

    @staticmethod
    def get_call_action(event: Any) -> Callable | None:
        """从事件中解析 OneBot call_action 入口;非 QQ 平台返回 None。"""
        platform = str(
            (getattr(event, "get_platform_name", None) or (lambda: ""))() or ""
        ).strip().lower()
        if platform != "aiocqhttp":
            return None
        bot = getattr(event, "bot", None)
        call_action = getattr(getattr(bot, "api", None), "call_action", None)
        return call_action if callable(call_action) else None

    @staticmethod
    def parse_session(event: Any) -> tuple[str, str] | None:
        """从 unified_msg_origin 解析 (message_type, session_id)。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        parts = umo.split(":")
        if len(parts) < 3 or not parts[2]:
            return None
        return parts[1], parts[2]

    @staticmethod
    def _unwrap_data(payload: Any) -> dict[str, Any]:
        """解包 OneBot 响应:{"data": {...}} → {...}。"""
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

    async def fetch_forward_messages(
        self, event: Any, forward_id: str
    ) -> list[dict[str, Any]]:
        """get_forward_msg 拉取合并转发子消息;message_id/id 双参数尝试。

        返回子消息 dict 列表(原始结构,由调用方解析);失败返回空列表。
        """
        call_action = self.get_call_action(event)
        if call_action is None:
            return []
        forward_id = str(forward_id or "").strip()
        if not forward_id:
            return []
        payload = None
        try:
            payload = await call_action(
                "get_forward_msg", message_id=forward_id
            )
        except Exception:
            try:
                payload = await call_action("get_forward_msg", id=forward_id)
            except Exception as e:
                logger.warning(
                    f"[archiver] get_forward_msg failed for {forward_id}: {e}"
                )
                return []
        data = self._unwrap_data(payload)
        messages = (
            data.get("messages") or data.get("message") or data.get("nodes")
        )
        if not isinstance(messages, list):
            return []
        return [m for m in messages if isinstance(m, dict)]

    async def send_forward_msg(self, event: Any, payload: dict[str, Any]) -> str | None:
        """发送合并转发消息,返回平台返回的 message_id;失败返回 None。

        payload 由调用方构造(Nodes.to_dict() 的输出),这里补充会话参数。
        """
        call_action = self.get_call_action(event)
        if call_action is None:
            return None
        session = self.parse_session(event)
        if session is None:
            return None
        message_type, session_id = session
        try:
            if "group" in message_type.lower():
                payload["group_id"] = session_id
                ret = await call_action(
                    "send_group_forward_msg", **payload
                )
            else:
                payload["user_id"] = session_id
                ret = await call_action(
                    "send_private_forward_msg", **payload
                )
        except Exception as e:
            logger.warning(
                f"[archiver] send forward via platform api failed: {e}"
            )
            return None
        if not isinstance(ret, dict):
            return None
        data = self._unwrap_data(ret)
        message_id = str(data.get("message_id") or "").strip()
        return message_id or None