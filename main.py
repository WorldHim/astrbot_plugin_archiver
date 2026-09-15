from __future__ import annotations

import asyncio
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import ComponentType, Image, Reply
from astrbot.api.star import Context, Star, register

from .constants import (
    DEFAULT_DELETE_PERMISSION,
    DEFAULT_UPLOAD_PERMISSION,
    FORWARD_PLATFORMS,  # noqa: F401  # re-export,保持旧导入路径兼容
    PLUGIN_NAME,
    PLUGIN_VERSION,  # noqa: F401  # re-export,保持旧导入路径兼容
)
from .models import Quote
from .quote_service import QuoteService
from .storage import QuoteStorage
from .web_api import register_web_apis


@register(
    PLUGIN_NAME,
    "WorldHim",
    "语录：回复一条消息即可收录其发送人与内容，并可随机调用语录库中的语录。",
    PLUGIN_VERSION,
)
class ArchiverPlugin(Star):
    """语录插件指令入口。

    仅负责命令注册、引用定位与结果发送;收录解析、输出构造、
    权限判定、删除识别等业务逻辑委托给 QuoteService。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._context = context
        # AstrBot 会在存在 _conf_schema.json 时注入插件配置(AstrBotConfig,dict 子类)
        self._config = config
        self._storage = QuoteStorage(PLUGIN_NAME)
        self._service = QuoteService(self._storage, config)
        # 注册 WebUI 管理 API(查看/删除/导入,页面位于 pages/quotes)
        register_web_apis(self._storage, context)

    # ---------- 配置读取(委托 service) ----------

    def _cfg(self, key: str, default):
        """读取插件配置;委托 QuoteService。"""
        return self._service.cfg(key, default)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return self._service.cfg_bool(key, default)

    def _cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        return self._service.cfg_int(key, default, minimum)

    # ---------- 引用定位 ----------

    @staticmethod
    def _find_reply(event: AstrMessageEvent) -> Reply | None:
        """从消息链中定位引用(回复)组件。"""
        for comp in event.get_messages():
            if getattr(comp, "type", None) == ComponentType.Reply:
                return comp  # type: ignore[return-value]
        return None

    # ---------- 指令 ----------

    @filter.command("保存", alias={"入典", "收录", "存档", "保存语录"})
    async def rudian(self, event: AstrMessageEvent):
        """回复一条消息并发送 /保存，将该消息的发送人与内容存档"""
        if not await self._service.check_permission(
            event, self._cfg("upload_permission", DEFAULT_UPLOAD_PERMISSION)
        ):
            yield event.plain_result("你没有上传语录的权限。")
            return
        reply = self._find_reply(event)
        if reply is None:
            yield event.plain_result(
                "请⌈回复⌋一条消息并发送 /保存 将其存档"
            )
            return

        sender_id, sender_name, text, image_comps, forward_nodes = (
            await self._service.extract_quote(event, reply)
        )
        # 显式指定归属:/保存 跟随 At 时,归属以 At 指定的人为准
        # (QQ 号保存,优先级高于默认归属与聊天记录归属)
        at_owner_id, at_owner_name = self._service.resolve_owner_target(event, "")
        if at_owner_id:
            sender_id = at_owner_id
            sender_name = at_owner_name or at_owner_id
        if not text and not image_comps and not forward_nodes:
            yield event.plain_result(
                "无法读取被回复消息的内容(可能为空消息或不支持的消息类型)，收录失败。"
            )
            return

        umo = event.unified_msg_origin
        message_id = str(getattr(reply, "id", "") or "")
        if message_id and self._storage.has_message(umo, message_id):
            yield event.plain_result("这条消息已经在语录库里啦~")
            return

        # 引用图片 + 聊天记录子消息图片并行下载落盘(超过阈值自动压缩)
        images = list(
            await asyncio.gather(
                *[self._service.archive_image(c) for c in image_comps]
            )
        )
        for sub in forward_nodes:
            sub["images"] = [
                await self._service.archive_image(
                    Image(
                        file=str(img.get("url") or img.get("file") or ""),
                        url=str(img.get("url") or ""),
                    )
                )
                for img in sub.get("images", [])
                if isinstance(img, dict)
            ]
        quote = Quote(
            id=uuid.uuid4().hex,
            session=umo,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            images=images,
            forward_nodes=forward_nodes,
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
        kind, payload = await self._service.emit_archive(event, quote, count)
        if kind == "sent":
            return
        if kind == "chain":
            yield event.chain_result(payload)
        else:
            yield event.plain_result(payload)

    @filter.command("语录", alias={"随机语录"})
    async def laidiandian(self, event: AstrMessageEvent, owner: str = ""):
        """发送 /语录 从语录库中随机调用一条已收录的语录;可 @某人 或输入昵称抽取指定人的语录"""
        quote, owner_id, owner_name = self._service.random_quote(event, owner)
        if quote is None:
            if owner_id or owner_name:
                yield event.plain_result(
                    f"语录库里还没有「{owner_id or owner_name}」的语录，"
                    "回复 TA 的消息发送 /保存 收录吧！"
                )
            else:
                yield event.plain_result(
                    "语录库还是空的，回复一条消息发送 /保存 收录第一条语录吧！"
                )
            return
        kind, payload = await self._service.emit_random(event, quote)
        if kind == "sent":
            return
        yield event.chain_result(payload)

    @filter.command("删除", alias={"删除语录"})
    async def shandian(self, event: AstrMessageEvent, code: str = ""):
        """回复语录消息或被收录的原消息发送 /删除 删除;也可 /删除 <编号>"""
        # 权限判定:级别通过可删任意语录;级别不通过时,若开启归属者删除,
        # 定位到语录后仅归属人本人(以QQ号匹配)可删自己的语录
        level_permitted = await self._service.check_permission(
            event, self._cfg("delete_permission", DEFAULT_DELETE_PERMISSION)
        )
        owner_delete_enabled = self._cfg_bool("owner_delete", True)
        if not level_permitted and not owner_delete_enabled:
            yield event.plain_result("你没有删除语录的权限。")
            return
        reply = self._find_reply(event)
        code = str(code or "").strip()
        quote = await self._service.resolve_delete_target(event, reply, code)

        if quote is None:
            if code:
                yield event.plain_result(f"没有找到编号为「{code}」的语录。")
            elif level_permitted:
                yield event.plain_result(
                    "请回复语录消息或被收录的原消息发送 /删除，"
                    "或使用 /删除 <编号> 删除（编号见语录消息末尾收录信息）。"
                )
            else:
                yield event.plain_result("没有找到可删除的语录。")
            return

        # 级别不通过时,仅语录归属人本人可删除
        if not level_permitted:
            sender_id = str(event.get_sender_id() or "").strip()
            if quote.sender_id != sender_id:
                yield event.plain_result("你只能删除归属为自己的语录。")
                return

        self._storage.delete_quote(quote.session, quote.id)
        # 同步清理该语录的已发送消息映射记录
        self._storage.delete_sent_message(quote.session, quote.id)
        logger.info(
            f"[archiver] deleted quote {quote.id} from {quote.session}"
        )
        yield event.plain_result(
            f"已删除「{quote.sender_name}」的语录（编号：{self._service.quote_code(quote)}）。"
        )

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""