from __future__ import annotations

import asyncio
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import ComponentType, Image, Plain, Reply
from astrbot.api.star import Context, Star, register

from .storage import Quote, QuoteStorage

# 插件版本(@register 与 metadata.yaml 的 version 保持一致)
PLUGIN_VERSION = "1.1.0"

# 插件唯一识别名(数据目录 data/plugin_data/{PLUGIN_NAME},与 metadata.yaml 的 name 保持一致)
PLUGIN_NAME = "astrbot_plugin_archiver"


@register(
    PLUGIN_NAME,
    "WorldHim",
    "入典：回复一条消息即可收录其发送人与内容，并可随机调用典库中的典藏。",
    PLUGIN_VERSION,
)
class ArchiverPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._context = context
        # AstrBot 会在存在 _conf_schema.json 时注入插件配置(AstrBotConfig,dict 子类)
        self._config = config
        self._storage = QuoteStorage(PLUGIN_NAME)

    # ---------- 配置读取 ----------

    def _cfg(self, key: str, default):
        """读取插件配置(WebUI 可调);未注入配置时使用默认值。"""
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """读取布尔型插件配置;非法时回退默认值。"""
        try:
            return bool(self._cfg(key, default))
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        """读取整型插件配置;非法或低于下限时回退默认值。"""
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= minimum else default

    # ---------- 引用消息解析 ----------

    @staticmethod
    def _find_reply(event: AstrMessageEvent) -> Reply | None:
        """从消息链中定位引用(回复)组件。"""
        for comp in event.get_messages():
            if getattr(comp, "type", None) == ComponentType.Reply:
                return comp  # type: ignore[return-value]
        return None

    @staticmethod
    def _extract_quote(reply: Reply) -> tuple[str, str, str, list]:
        """从引用组件提取被收录消息的发送人与内容。

        Returns:
            (sender_id, sender_name, text, image_comps);image_comps 为引用
            消息中的原始图片组件,由调用方异步下载落盘。
        """
        sender_id = str(getattr(reply, "sender_id", "") or "").strip()
        sender_name = (
            str(getattr(reply, "sender_nickname", "") or "").strip()
            or sender_id
            or "未知用户"
        )

        parts: list[str] = []
        image_comps: list = []
        for comp in reply.chain or []:
            ctype = getattr(comp, "type", None)
            if ctype == ComponentType.Plain:
                text = str(getattr(comp, "text", "") or "").strip()
                if text:
                    parts.append(text)
            elif ctype == ComponentType.Image:
                image_comps.append(comp)
            elif ctype == ComponentType.At:
                name = str(getattr(comp, "name", "") or "").strip()
                parts.append(f"@{name or getattr(comp, 'qq', '') or '有人'}")
            elif ctype == ComponentType.Face:
                parts.append("[表情]")
            elif ctype == ComponentType.Record:
                parts.append("[语音]")
            elif ctype == ComponentType.Video:
                parts.append("[视频]")
            elif ctype == ComponentType.File:
                parts.append("[文件]")
            elif ctype in (
                ComponentType.Forward,
                ComponentType.Node,
                ComponentType.Nodes,
            ):
                parts.append("[转发消息]")

        # 平台未回填 chain 时回退到引用解析出的纯文本
        if not parts and str(getattr(reply, "message_str", "") or "").strip():
            parts.append(str(reply.message_str).strip())

        return sender_id, sender_name, "\n".join(parts).strip(), image_comps

    # ---------- 图片存档 ----------

    async def _archive_image(self, comp) -> dict[str, str]:
        """将引用消息中的图片下载保存到本地(超过阈值自动压缩)。

        下载或落盘失败时回退为仅记录 URL/组件数据(与旧版行为一致)。
        """
        file = str(getattr(comp, "file", "") or "")
        url = str(getattr(comp, "url", "") or "").strip()
        fallback: dict[str, str] = {"file": file}
        if url:
            fallback["url"] = url
        try:
            src_path = await comp.convert_to_file_path()
        except Exception as e:
            logger.warning(
                f"[archiver] Failed to fetch image ({url or file}...): {e}"
            )
            return fallback
        rel = self._storage.store_image(
            src_path,
            self._cfg_int("image_compress_threshold_mb", 2) * 1024 * 1024,
        )
        if rel is None:
            return fallback
        entry: dict[str, str] = {"path": rel}
        if url:
            entry["url"] = url
        return entry

    def _image_component(self, image: dict[str, str]) -> Image | None:
        """由典藏记录构造图片组件:优先本地存档,缺失时回退 URL。"""
        full = self._storage.resolve_image_path(str(image.get("path") or ""))
        if full is not None:
            return Image.fromFileSystem(str(full))
        url = str((image.get("url") or image.get("file") or "")).strip()
        return Image(file=url) if url else None

    # ---------- 指令 ----------

    @filter.command("入典", alias={"收录", "存典"})
    async def rudian(self, event: AstrMessageEvent):
        """回复一条消息并发送 /入典，将该消息的发送人与内容存档"""
        reply = self._find_reply(event)
        if reply is None:
            yield event.plain_result(
                "请先【回复】一条消息，再发送 /入典 将其收录进典库~"
            )
            return

        sender_id, sender_name, text, image_comps = self._extract_quote(reply)
        if not text and not image_comps:
            yield event.plain_result(
                "无法读取被回复消息的内容(可能为空消息或不支持的消息类型)，收录失败。"
            )
            return

        umo = event.unified_msg_origin
        message_id = str(getattr(reply, "id", "") or "")
        if message_id and self._storage.has_message(umo, message_id):
            yield event.plain_result("这条消息已经在典库里啦~")
            return

        # 图片并行下载落盘(超过阈值自动压缩);失败时回退记录 URL
        images = list(
            await asyncio.gather(*[self._archive_image(c) for c in image_comps])
        )
        quote = Quote(
            id=uuid.uuid4().hex,
            session=umo,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            images=images,
            archived_by=event.get_sender_id(),
            archived_by_name=event.get_sender_name(),
            archived_at_ts=time.time(),
        )
        count = self._storage.add_quote(
            umo,
            quote,
            limit=self._cfg_int("session_quote_limit", 0),
        )
        logger.info(
            f"[archiver] archived message {message_id or '(no id)'} "
            f"from {sender_name} in {umo}, session count: {count}"
        )
        yield event.plain_result(
            f"已收录「{sender_name}」的发言，本会话典库共 {count} 条"
        )

    @filter.command("来点典", alias={"随机典", "来典"})
    async def laidiandian(self, event: AstrMessageEvent):
        """从典库中随机调用一条已收录的典"""
        umo = event.unified_msg_origin
        quote = self._storage.random_quote(umo)
        if quote is None and self._cfg_bool("fallback_global", False):
            # 当前会话典库为空时,按配置回退到从所有会话的典库中抽取
            quote = self._storage.random_quote_any()

        if quote is None:
            yield event.plain_result(
                "典库还是空的，回复一条消息发送 /入典 收录第一条典吧！"
            )
            return

        header = f"「{quote.sender_name}」"
        if quote.text:
            header += f"\n{quote.text}"
        chain: list = [Plain(header)]
        for image in quote.images:
            comp = self._image_component(image)
            if comp is not None:
                chain.append(comp)
        yield event.chain_result(chain)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
