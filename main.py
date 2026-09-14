from __future__ import annotations

import asyncio
import json
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    ComponentType,
    Image,
    Node,
    Nodes,
    Plain,
    Reply,
)
from astrbot.api.star import Context, Star, register

from .storage import Quote, QuoteStorage

try:
    # AstrBot 内置引用消息提取器:回复内容为聊天记录(合并转发)时,
    # 通过 get_msg/get_forward_msg 递归拉取子消息内容
    from astrbot.core.utils.quoted_message import extract_quoted_message_text
    from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser
except ImportError:  # 旧版 AstrBot 无该模块时回退为占位符记录
    extract_quoted_message_text = None
    ReplyChainParser = None

# 插件版本(@register 与 metadata.yaml 的 version 保持一致)
PLUGIN_VERSION = "1.2.0"

# 插件唯一识别名(数据目录 data/plugin_data/{PLUGIN_NAME},与 metadata.yaml 的 name 保持一致)
PLUGIN_NAME = "astrbot_plugin_archiver"

# 支持自建合并转发(聊天记录)的平台适配器;其余平台发送 Nodes 组件会被忽略,自动回退文本
FORWARD_PLATFORMS = {"aiocqhttp", "satori"}


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

    def _use_forward(self, event: AstrMessageEvent) -> bool:
        """是否以聊天记录(合并转发)形式输出。

        需同时满足:use_forward 总开关开启,且当前平台适配器支持自建
        合并转发(其余平台会忽略 Nodes 组件,导致输出无响应)。
        """
        if not self._cfg_bool("use_forward", True):
            return False
        platform = str(event.get_platform_name() or "").strip().lower()
        return platform in FORWARD_PLATFORMS

    # ---------- 引用消息解析 ----------

    @staticmethod
    def _find_reply(event: AstrMessageEvent) -> Reply | None:
        """从消息链中定位引用(回复)组件。"""
        for comp in event.get_messages():
            if getattr(comp, "type", None) == ComponentType.Reply:
                return comp  # type: ignore[return-value]
        return None

    async def _extract_quote(
        self, event: AstrMessageEvent, reply: Reply
    ) -> tuple[str, str, str, list, list]:
        """从引用组件提取被收录消息的发送人与内容。

        当引用的内容为聊天记录(合并转发)时,不展开为文本,而是拉取
        结构化子消息(发送人/文本/图片)并以 "[聊天记录]" 占位 text 返回,
        实际内容保存在 forward_nodes;结构化拉取失败时回退为
        [转发消息] 占位符。

        Returns:
            (sender_id, sender_name, text, image_comps, forward_nodes);
            image_comps 为引用消息中的原始图片组件;forward_nodes 为
            聊天记录的结构化子消息列表(非聊天记录时为空)。
        """
        sender_id = str(getattr(reply, "sender_id", "") or "").strip()
        sender_name = (
            str(getattr(reply, "sender_nickname", "") or "").strip()
            or sender_id
            or "未知用户"
        )

        parts: list[str] = []
        image_comps: list = []
        has_forward = False
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
                has_forward = True

        # 引用内容为聊天记录(合并转发)时,不展开为文本,以聊天记录保存
        if has_forward:
            forward_nodes = await self._fetch_forward_nodes(event, reply)
            if forward_nodes:
                # 归属:默认为回复消息的发送者;聊天记录则取其中最后一条
                # 消息的发送者(以 QQ 号保存,避免群昵称变化影响归属)
                last_node = forward_nodes[-1]
                if isinstance(last_node, dict):
                    sender_id = (
                        str(last_node.get("sender_id") or "").strip() or sender_id
                    )
                    sender_name = (
                        str(last_node.get("sender_name") or "").strip()
                        or sender_id
                        or sender_name
                    )
                # 外层文本(如有)保留在 text 中,聊天记录本体在 forward_nodes
                outer = "\n".join(parts).strip()
                text = f"{outer}\n[聊天记录]" if outer else "[聊天记录]"
                return sender_id, sender_name, text, image_comps, forward_nodes
            # 结构化拉取失败 → 回退为提取器展开文本/占位符
            forward_text = await self._extract_forward_text(event, reply)
            parts.append(forward_text if forward_text else "[转发消息]")

        # 平台未回填 chain 时回退到引用解析出的纯文本
        if not parts and str(getattr(reply, "message_str", "") or "").strip():
            parts.append(str(reply.message_str).strip())

        return sender_id, sender_name, "\n".join(parts).strip(), image_comps, []

    async def _extract_forward_text(
        self, event: AstrMessageEvent, reply: Reply
    ) -> str | None:
        """拉取被引用聊天记录(合并转发)的实际内容。

        复用 AstrBot 内置引用消息提取器:引用内容为占位符时,通过
        get_msg/get_forward_msg 递归拉取子消息并格式化为"发送人: 内容"
        多行文本;平台不支持/拉取失败/结果仍为占位符时返回 None。
        """
        if extract_quoted_message_text is None:
            return None
        try:
            text = await extract_quoted_message_text(event, reply)
        except Exception as e:
            logger.warning(f"[archiver] Failed to extract forward message: {e}")
            return None
        if not text or not text.strip():
            return None
        stripped = text.strip()
        if (
            ReplyChainParser is not None
            and ReplyChainParser.is_forward_placeholder_only_text(stripped)
        ):
            return None
        return stripped

    # ---------- 聊天记录(合并转发)结构化存储 ----------

    async def _fetch_forward_nodes(
        self, event: AstrMessageEvent, reply: Reply
    ) -> list[dict]:
        """拉取被引用聊天记录(合并转发)的结构化子消息。

        Node/Nodes 直接从组件内嵌 content 解析;Forward 组件在 aiocqhttp
        (QQ)平台通过 get_forward_msg 拉取。失败返回空列表。
        """
        nodes: list[dict] = []
        for comp in reply.chain or []:
            ctype = getattr(comp, "type", None)
            if ctype == ComponentType.Node:
                node = self._parse_node_component(comp)
                if node is not None:
                    nodes.append(node)
            elif ctype == ComponentType.Nodes:
                for raw in getattr(comp, "nodes", []) or []:
                    node = self._parse_node_component(raw)
                    if node is not None:
                        nodes.append(node)
            elif ctype == ComponentType.Forward:
                nodes.extend(await self._fetch_forward_via_api(event, comp))
        return nodes

    def _parse_node_component(self, node) -> dict | None:
        """将转发消息节点组件解析为结构化子消息(不展开为纯文本)。"""
        if node is None:
            return None
        sender_name = str(getattr(node, "name", "") or "").strip()
        sender_id = str(getattr(node, "uin", "") or "").strip()
        parts: list[str] = []
        images: list[dict[str, str]] = []
        for seg in getattr(node, "content", None) or []:
            ctype = getattr(seg, "type", None)
            if ctype == ComponentType.Plain:
                text = str(getattr(seg, "text", "") or "").strip()
                if text:
                    parts.append(text)
            elif ctype == ComponentType.Image:
                entry = self._image_entry_from_component(seg)
                if entry:
                    images.append(entry)
            elif ctype == ComponentType.At:
                name = str(getattr(seg, "name", "") or "").strip()
                parts.append(f"@{name or getattr(seg, 'qq', '') or '有人'}")
            elif ctype == ComponentType.Face:
                parts.append("[表情]")
            elif ctype == ComponentType.Record:
                parts.append("[语音]")
            elif ctype == ComponentType.Video:
                parts.append("[视频]")
        return {
            "sender_id": sender_id,
            "sender_name": sender_name or "未知用户",
            "text": "".join(parts).strip(),
            "images": images,
        }

    @staticmethod
    def _image_entry_from_component(comp) -> dict[str, str] | None:
        """从图片组件提取 {url,file} 键值;无有效来源时返回 None。"""
        url = str(getattr(comp, "url", "") or "").strip()
        file = str(getattr(comp, "file", "") or "").strip()
        entry: dict[str, str] = {}
        if url:
            entry["url"] = url
        if file and file != url:
            entry["file"] = file
        return entry or None

    async def _fetch_forward_via_api(
        self, event: AstrMessageEvent, comp
    ) -> list[dict]:
        """通过 OneBot API(get_forward_msg)拉取 Forward 转发消息的子消息。

        仅 aiocqhttp(QQ)平台支持;失败返回空列表。
        """
        platform = str(event.get_platform_name() or "").strip().lower()
        if platform != "aiocqhttp":
            return []
        bot = getattr(event, "bot", None)
        call_action = getattr(getattr(bot, "api", None), "call_action", None)
        if not callable(call_action):
            return []
        forward_id = str(getattr(comp, "id", "") or "").strip()
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
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        messages = (
            data.get("messages") or data.get("message") or data.get("nodes")
        )
        if not isinstance(messages, list):
            return []
        nodes: list[dict] = []
        for raw in messages:
            node = self._parse_onebot_forward_node(raw)
            if node is not None:
                nodes.append(node)
        return nodes

    def _parse_onebot_forward_node(self, raw) -> dict | None:
        """将 OneBot 子消息 dict 解析为结构化子消息(发送人/文本/图片)。"""
        if not isinstance(raw, dict):
            return None
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        sender_name = str(
            sender.get("nickname") or sender.get("card") or ""
        ).strip()
        sender_id = str(sender.get("user_id") or "").strip()
        content = raw.get("message") or raw.get("content") or []
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (TypeError, ValueError):
                return {
                    "sender_id": sender_id,
                    "sender_name": sender_name or "未知用户",
                    "text": content.strip(),
                    "images": [],
                }
        if not isinstance(content, list):
            content = []
        parts: list[str] = []
        images: list[dict[str, str]] = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            seg_data = seg.get("data")
            if not isinstance(seg_data, dict):
                seg_data = {}
            if seg_type == "text":
                text = str(seg_data.get("text") or "").strip()
                if text:
                    parts.append(text)
            elif seg_type == "image":
                url = str(seg_data.get("url") or "").strip()
                file = str(seg_data.get("file") or "").strip()
                entry: dict[str, str] = {}
                if url:
                    entry["url"] = url
                if file and file != url:
                    entry["file"] = file
                if entry:
                    images.append(entry)
            elif seg_type == "at":
                parts.append(
                    f"@{seg_data.get('name') or seg_data.get('qq') or '有人'}"
                )
            elif seg_type == "face":
                parts.append("[表情]")
        return {
            "sender_id": sender_id,
            "sender_name": sender_name or "未知用户",
            "text": "".join(parts).strip(),
            "images": images,
        }

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

    # ---------- 聊天记录(合并转发)输出 ----------

    def _quote_node(self, quote: Quote) -> Node:
        """将一条典构造为合并转发的消息节点(发送人 + 内容 + 图片)。

        节点以被收录者的昵称为展示名,内容为收录文本与图片,
        在支持合并转发的平台(如 QQ)呈现为一条"聊天记录"。
        """
        content: list = []
        if quote.text:
            content.append(Plain(quote.text))
        for image in quote.images:
            comp = self._image_component(image)
            if comp is not None:
                content.append(comp)
        if not content:
            content.append(Plain("[无内容]"))
        return Node(
            content=content,
            uin=str(quote.sender_id or "0"),
            name=str(quote.sender_name or "未知用户"),
        )

    def _build_forward_quote_nodes(
        self, quote: Quote, info_text: str | None = None
    ) -> list:
        """将聊天记录(结构化子消息)典构造为转发消息节点。

        保持聊天记录形态:每条子消息一个节点,最后追加一条收录信息节点。
        混合收录时的外层文本作为第一条节点(被收录者身份)。

        Args:
            quote: 聊天记录典。
            info_text: 收录信息节点文本;None 时使用收录人/收录时间。
        """
        nodes: list = []
        outer_text = str(quote.text or "").replace("[聊天记录]", "").strip()
        if outer_text:
            nodes.append(
                Node(
                    content=[Plain(outer_text)],
                    uin=str(quote.sender_id or "0"),
                    name=str(quote.sender_name or "未知用户"),
                )
            )
        for sub in quote.forward_nodes:
            if not isinstance(sub, dict):
                continue
            content: list = []
            text = str(sub.get("text") or "").strip()
            if text:
                content.append(Plain(text))
            for image in sub.get("images") or []:
                if not isinstance(image, dict):
                    continue
                comp = self._image_component(image)
                if comp is not None:
                    content.append(comp)
            if not content:
                content.append(Plain("[无内容]"))
            nodes.append(
                Node(
                    content=content,
                    uin=str(sub.get("sender_id") or "0"),
                    name=str(sub.get("sender_name") or "未知用户"),
                )
            )
        if not nodes:
            nodes.append(Node(content=[Plain("[聊天记录]")], name="典藏档案"))
        # 最后增加一条收录信息节点
        info = info_text if info_text is not None else self._archive_info_text(quote)
        nodes.append(
            Node(content=[Plain(info or "已收录典藏")], name="典藏档案")
        )
        return nodes

    @staticmethod
    def _archive_info_text(quote: Quote) -> str:
        """由典藏记录生成收录信息文本(收录人/收录时间);无信息时返回空串。"""
        parts: list[str] = []
        by = str(quote.archived_by_name or "").strip()
        if by:
            parts.append(f"收录人：{by}")
        if quote.archived_at_ts > 0:
            parts.append(
                "收录时间："
                + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(quote.archived_at_ts)
                )
            )
        return "\n".join(parts)

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

        sender_id, sender_name, text, image_comps, forward_nodes = (
            await self._extract_quote(event, reply)
        )
        # 显式指定归属:/入典 跟随 At 时,归属以 At 指定的人为准
        # (QQ 号保存,优先级高于默认归属与聊天记录归属)
        at_owner_id, at_owner_name = self._resolve_owner_target(event, "")
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
            yield event.plain_result("这条消息已经在典库里啦~")
            return

        # 引用图片 + 聊天记录子消息图片并行下载落盘(超过阈值自动压缩)
        images = list(
            await asyncio.gather(*[self._archive_image(c) for c in image_comps])
        )
        for sub in forward_nodes:
            sub["images"] = [
                await self._archive_image(
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
        if self._use_forward(event):
            # 以聊天记录(合并转发)形式回执
            if quote.forward_nodes:
                # 聊天记录:原样回显引用的聊天记录,最后一条为收录信息
                nodes: list = self._build_forward_quote_nodes(
                    quote,
                    info_text=f"已收录，本会话典库共 {count} 条",
                )
            else:
                nodes = [self._quote_node(quote)]
                nodes.append(
                    Node(
                        content=[Plain(f"已收录，本会话典库共 {count} 条")],
                        name="入典成功",
                    )
                )
            yield event.chain_result([Nodes(nodes=nodes)])
        else:
            yield event.plain_result(
                f"已收录「{sender_name}」的发言，本会话典库共 {count} 条"
            )

    def _resolve_owner_target(
        self, event: AstrMessageEvent, owner: str
    ) -> tuple[str, str]:
        """解析抽取目标归属人。

        消息链中的 At 组件优先(以 QQ 号精确匹配,排除 @Bot 唤醒的自身);
        其次取命令后的昵称文本。均无时返回 ("", "") 表示随机抽取。
        """
        self_id = str(event.get_self_id() or "").strip()
        for comp in event.get_messages():
            if getattr(comp, "type", None) == ComponentType.At:
                qq = str(getattr(comp, "qq", "") or "").strip()
                if qq and qq != self_id:
                    name = str(getattr(comp, "name", "") or "").strip()
                    return qq, name
        owner = str(owner or "").strip()
        if owner:
            return "", owner
        return "", ""

    @filter.command("来点典", alias={"随机典", "来典"})
    async def laidiandian(self, event: AstrMessageEvent, owner: str = ""):
        """从典库中随机调用一条已收录的典;可 @某人 或输入昵称抽取指定人的典"""
        umo = event.unified_msg_origin
        owner_id, owner_name = self._resolve_owner_target(event, owner)
        if owner_id or owner_name:
            # 抽取指定归属人的典(QQ 号优先精确匹配,其次昵称)
            quote = self._storage.random_quote_by_owner(
                umo, owner_id, owner_name
            )
            if quote is None and self._cfg_bool("fallback_global", False):
                quote = self._storage.random_quote_by_owner_any(
                    owner_id, owner_name
                )
        else:
            quote = self._storage.random_quote(umo)
            if quote is None and self._cfg_bool("fallback_global", False):
                # 当前会话典库为空时,按配置回退到从所有会话的典库中抽取
                quote = self._storage.random_quote_any()

        if quote is None:
            if owner_id or owner_name:
                yield event.plain_result(
                    f"典库里还没有「{owner_id or owner_name}」的典，"
                    "回复 TA 的消息发送 /入典 收录吧！"
                )
            else:
                yield event.plain_result(
                    "典库还是空的，回复一条消息发送 /入典 收录第一条典吧！"
                )
            return

        if self._use_forward(event):
            # 以聊天记录(合并转发)形式发送,更直观优雅
            if quote.forward_nodes:
                # 聊天记录典:保持聊天记录形态,最后一条为收录信息
                nodes: list = self._build_forward_quote_nodes(quote)
            else:
                nodes = [self._quote_node(quote)]
                info = self._archive_info_text(quote)
                if info:
                    nodes.append(Node(content=[Plain(info)], name="典藏档案"))
            yield event.chain_result([Nodes(nodes=nodes)])
            return

        # 文本回退(不支持合并转发的平台可关闭 use_forward)
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
