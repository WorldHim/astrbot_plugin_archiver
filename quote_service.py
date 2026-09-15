"""语录插件核心业务:收录解析、输出构造、权限判定、删除识别。

从 main.py 拆出的业务层;main.py 仅保留指令注册与 handler 委托。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    ComponentType,
    Image,
    Node,
    Nodes,
    Plain,
)

from .constants import (
    FORWARD_PLATFORMS,
    INFO_NODE_NAME,
    PLACEHOLDER_AT_UNKNOWN,
    PLACEHOLDER_CHAT_RECORD,
    PLACEHOLDER_EMPTY,
    PLACEHOLDER_FACE,
    PLACEHOLDER_FILE,
    PLACEHOLDER_FORWARD,
    PLACEHOLDER_IMAGE,
    PLACEHOLDER_UNKNOWN_OWNER,
    PLACEHOLDER_VIDEO,
    PLACEHOLDER_VOICE,
    RECEIPT_NODE_NAME,
)
from .napcat_service import NapcatService
from .storage import QuoteStorage

try:
    # AstrBot 内置引用消息提取器:回复内容为聊天记录(合并转发)时,
    # 通过 get_msg/get_forward_msg 递归拉取子消息内容
    from astrbot.core.utils.quoted_message import extract_quoted_message_text
    from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser
except ImportError:  # 旧版 AstrBot 无该模块时回退为占位符记录
    extract_quoted_message_text = None
    ReplyChainParser = None


class QuoteService:
    """语录核心业务(持有存储与配置,供指令 handler 委托)。"""

    def __init__(self, storage: QuoteStorage, config=None):
        self.storage = storage
        self.config = config
        self.napcat = NapcatService()

    # ---------- 配置读取 ----------

    def cfg(self, key: str, default):
        """读取插件配置(WebUI 可调);未注入配置时使用默认值。"""
        if self.config is None:
            return default
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def cfg_bool(self, key: str, default: bool) -> bool:
        """读取布尔型插件配置;非法时回退默认值。"""
        try:
            return bool(self.cfg(key, default))
        except Exception:
            return default

    def cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        """读取整型插件配置;非法或低于下限时回退默认值。"""
        try:
            value = int(self.cfg(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= minimum else default

    def use_forward(self, event: AstrMessageEvent) -> bool:
        """是否以聊天记录(合并转发)形式输出。

        需同时满足:use_forward 总开关开启,且当前平台适配器支持自建
        合并转发(其余平台会忽略 Nodes 组件,导致输出无响应)。
        """
        if not self.cfg_bool("use_forward", True):
            return False
        platform = str(event.get_platform_name() or "").strip().lower()
        return platform in FORWARD_PLATFORMS

    # ---------- 引用消息解析 ----------

    async def extract_quote(
        self, event: AstrMessageEvent, reply
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
            or PLACEHOLDER_UNKNOWN_OWNER
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
                parts.append(f"@{name or getattr(comp, 'qq', '') or PLACEHOLDER_AT_UNKNOWN}")
            elif ctype == ComponentType.Face:
                parts.append(PLACEHOLDER_FACE)
            elif ctype == ComponentType.Record:
                parts.append(PLACEHOLDER_VOICE)
            elif ctype == ComponentType.Video:
                parts.append(PLACEHOLDER_VIDEO)
            elif ctype == ComponentType.File:
                parts.append(PLACEHOLDER_FILE)
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
                text = (
                    f"{outer}\n{PLACEHOLDER_CHAT_RECORD}"
                    if outer
                    else PLACEHOLDER_CHAT_RECORD
                )
                return sender_id, sender_name, text, image_comps, forward_nodes
            # 结构化拉取失败 → 回退为提取器展开文本/占位符
            forward_text = await self._extract_forward_text(event, reply)
            parts.append(forward_text if forward_text else PLACEHOLDER_FORWARD)

        # 平台未回填 chain 时回退到引用解析出的纯文本
        if not parts and str(getattr(reply, "message_str", "") or "").strip():
            parts.append(str(reply.message_str).strip())

        return sender_id, sender_name, "\n".join(parts).strip(), image_comps, []

    async def _extract_forward_text(
        self, event: AstrMessageEvent, reply
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

    # ---------- 聊天记录(合并转发)结构化解析 ----------

    async def _fetch_forward_nodes(
        self, event: AstrMessageEvent, reply
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
                forward_id = str(getattr(comp, "id", "") or "").strip()
                raw_nodes = await self.napcat.fetch_forward_messages(
                    event, forward_id
                )
                for raw in raw_nodes:
                    node = self._parse_onebot_forward_node(raw)
                    if node is not None:
                        nodes.append(node)
        return nodes

    @staticmethod
    def _node_time(value) -> float:
        """将节点时间戳转为 float;无效值回退 0(无时间,前端不显示)。"""
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

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
                parts.append(f"@{name or getattr(seg, 'qq', '') or PLACEHOLDER_AT_UNKNOWN}")
            elif ctype == ComponentType.Face:
                parts.append(PLACEHOLDER_FACE)
            elif ctype == ComponentType.Record:
                parts.append(PLACEHOLDER_VOICE)
            elif ctype == ComponentType.Video:
                parts.append(PLACEHOLDER_VIDEO)
        return {
            "sender_id": sender_id,
            "sender_name": sender_name or PLACEHOLDER_UNKNOWN_OWNER,
            "text": "".join(parts).strip(),
            "images": images,
            "time": self._node_time(getattr(node, "time", 0)),
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
                    "sender_name": sender_name or PLACEHOLDER_UNKNOWN_OWNER,
                    "text": content.strip(),
                    "images": [],
                    "time": self._node_time(raw.get("time")),
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
                    f"@{seg_data.get('name') or seg_data.get('qq') or PLACEHOLDER_AT_UNKNOWN}"
                )
            elif seg_type == "face":
                parts.append(PLACEHOLDER_FACE)
        return {
            "sender_id": sender_id,
            "sender_name": sender_name or PLACEHOLDER_UNKNOWN_OWNER,
            "text": "".join(parts).strip(),
            "images": images,
            "time": self._node_time(raw.get("time")),
        }

    # ---------- 图片存档 ----------

    async def archive_image(self, comp) -> dict[str, str]:
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
        rel = self.storage.store_image(
            src_path,
            self.cfg_int("image_compress_threshold_mb", 2) * 1024 * 1024,
        )
        if rel is None:
            return fallback
        entry: dict[str, str] = {"path": rel}
        if url:
            entry["url"] = url
        return entry

    def image_component(self, image: dict[str, str]) -> Image | None:
        """由语录记录构造图片组件:优先本地存档,缺失时回退 URL。"""
        full = self.storage.resolve_image_path(str(image.get("path") or ""))
        if full is not None:
            return Image.fromFileSystem(str(full))
        url = str((image.get("url") or image.get("file") or "")).strip()
        return Image(file=url) if url else None

    # ---------- 输出构造 ----------

    def quote_node(self, quote) -> Node:
        """将一条语录构造为合并转发的消息节点(发送人 + 内容 + 图片)。

        节点以被收录者的昵称为展示名,内容为收录文本与图片,
        在支持合并转发的平台(如 QQ)呈现为一条"聊天记录"。
        """
        content: list = []
        if quote.text:
            content.append(Plain(quote.text))
        for image in quote.images:
            comp = self.image_component(image)
            if comp is not None:
                content.append(comp)
        if not content:
            content.append(Plain(PLACEHOLDER_EMPTY))
        return Node(
            content=content,
            uin=str(quote.sender_id or "0"),
            name=str(quote.sender_name or PLACEHOLDER_UNKNOWN_OWNER),
        )

    def build_forward_quote_nodes(
        self, quote, info_text: str | None = None
    ) -> list:
        """将聊天记录(结构化子消息)语录构造为转发消息节点。

        保持聊天记录形态:每条子消息一个节点,最后追加一条收录信息节点。
        混合收录时的外层文本作为第一条节点(被收录者身份)。

        Args:
            quote: 聊天记录语录。
            info_text: 收录信息节点文本;None 时使用收录人/收录时间。
        """
        nodes: list = []
        outer_text = str(quote.text or "").replace(PLACEHOLDER_CHAT_RECORD, "").strip()
        if outer_text:
            nodes.append(
                Node(
                    content=[Plain(outer_text)],
                    uin=str(quote.sender_id or "0"),
                    name=str(quote.sender_name or PLACEHOLDER_UNKNOWN_OWNER),
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
                comp = self.image_component(image)
                if comp is not None:
                    content.append(comp)
            if not content:
                content.append(Plain(PLACEHOLDER_EMPTY))
            nodes.append(
                Node(
                    content=content,
                    uin=str(sub.get("sender_id") or "0"),
                    name=str(sub.get("sender_name") or PLACEHOLDER_UNKNOWN_OWNER),
                    time=int(self._node_time(sub.get("time"))),
                )
            )
        if not nodes:
            nodes.append(
                Node(content=[Plain(PLACEHOLDER_CHAT_RECORD)], name=INFO_NODE_NAME)
            )
        # 最后增加一条收录信息节点
        info = info_text if info_text is not None else self.archive_info_text(quote)
        nodes.append(
            Node(content=[Plain(info or "已收录语录")], name=INFO_NODE_NAME)
        )
        return nodes

    @staticmethod
    def archive_info_text(quote) -> str:
        """由语录记录生成收录信息文本(收录人/收录时间/编号);无信息时返回空串。"""
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
        if quote.id:
            parts.append(f"编号：{quote.id[:8]}")
        return "\n".join(parts)

    @staticmethod
    def quote_code(quote) -> str:
        """语录的展示编号(ID 前 8 位)。"""
        return str(quote.id or "")[:8]

    def random_reply_text(self, quote) -> str:
        """文本模式随机语录回执(不支持合并转发的平台回退)。"""
        header = f"「{quote.sender_name}」"
        if quote.text:
            header += f"\n{quote.text}"
        return header

    # ---------- 权限判定 ----------

    async def check_permission(
        self, event: AstrMessageEvent, level: str
    ) -> bool:
        """校验用户是否满足权限级别。

        级别(不区分大小写,兼容常见别名): 群员(member/普通成员)=所有人;
        管理员(admin)=群管理员+群主+Bot管理员;群主(owner)=群主+Bot管理员;
        Bot管理员=仅Bot管理员(event.is_admin())。私聊消息无法查询群信息,
        按 Bot 管理员判定;未知级别保守地按 Bot 管理员判定。
        """
        level = str(level or "").strip().replace(" ", "")
        lowered = level.lower()
        if level in {"群员", "普通成员"} or lowered == "member":
            return True
        try:
            is_bot_admin = bool(event.is_admin())
        except Exception:
            is_bot_admin = False
        if lowered in {"bot管理员", "botadmin", "bot_admin"}:
            return is_bot_admin
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if not group_id:
            return is_bot_admin
        is_group_owner = False
        is_group_admin = False
        try:
            group = await event.get_group()
        except Exception as e:
            logger.info(f"[archiver] Failed to query group info: {e}")
            group = None
        if group is not None:
            sender_id = str(event.get_sender_id() or "").strip()
            owner_id = str(getattr(group, "group_owner", "") or "").strip()
            admin_ids = [
                str(i).strip()
                for i in (getattr(group, "group_admins", None) or [])
            ]
            is_group_owner = bool(owner_id and sender_id == owner_id)
            is_group_admin = sender_id in admin_ids
        if level in {"群主"} or lowered == "owner":
            return is_group_owner or is_bot_admin
        return is_group_admin or is_group_owner or is_bot_admin

    # ---------- 归属解析 ----------

    def resolve_owner_target(
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

    # ---------- 删除识别 ----------

    def resolve_reply_text_code(self, reply) -> str | None:
        """从被回复消息回填的内容(Reply.chain/message_str)中解析语录编号。

        适配器回复普通消息时会回填被回复消息的内容,语录消息末尾收录信息
        含"编号:xxx"(文本回执与合并转发收录信息均嵌入编号)。纯本地解析,
        不依赖任何平台接口。
        """
        pattern = re.compile(r"编号[:：]\s*([0-9a-f]{4,32})", re.IGNORECASE)

        def find_in_text(text: str) -> str | None:
            m = pattern.search(str(text or ""))
            return m.group(1).lower() if m else None

        # 1) Reply.chain:Plain 段与 Node/Nodes 的 content
        for comp in getattr(reply, "chain", None) or []:
            ctype = getattr(comp, "type", None)
            if ctype == ComponentType.Plain:
                code = find_in_text(getattr(comp, "text", ""))
                if code:
                    return code
            elif ctype in (ComponentType.Node, ComponentType.Nodes):
                nodes = (
                    [comp]
                    if ctype == ComponentType.Node
                    else getattr(comp, "nodes", []) or []
                )
                for node in nodes:
                    for seg in getattr(node, "content", None) or []:
                        if getattr(seg, "type", None) == ComponentType.Plain:
                            code = find_in_text(getattr(seg, "text", ""))
                            if code:
                                return code
        # 2) message_str:被回复消息的纯文本表示
        return find_in_text(str(getattr(reply, "message_str", "") or ""))

    async def resolve_reply_quote_id(
        self, event: AstrMessageEvent, reply
    ) -> str | None:
        """从被回复的语录消息(本 bot 发送的合并转发)解析语录编号。

        通过 get_forward_msg 拉取合并转发内容,解析收录信息节点中的
        "编号:xxx";仅 aiocqhttp(QQ)平台支持,失败返回 None。
        """
        reply_id = str(getattr(reply, "id", "") or "").strip()
        if not reply_id:
            return None
        messages = await self.napcat.fetch_forward_messages(event, reply_id)
        if not messages:
            return None
        # 在子消息文本中查找收录信息节点的"编号:xxx"
        pattern = re.compile(r"编号[:：]\s*([0-9a-f]{4,32})", re.IGNORECASE)
        for node in messages:
            if not isinstance(node, dict):
                continue
            raw = node.get("message") or node.get("content") or []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = [{"type": "text", "data": {"text": raw}}]
            for seg in raw if isinstance(raw, list) else []:
                if not isinstance(seg, dict):
                    continue
                seg_data = seg.get("data")
                text = str(
                    (seg_data or {}).get("text", "")
                    if isinstance(seg_data, dict)
                    else ""
                )
                m = pattern.search(text)
                if m:
                    return m.group(1).lower()
        return None

    async def resolve_delete_target(
        self, event: AstrMessageEvent, reply, code: str
    ):
        """按识别链定位待删除的语录;未找到返回 None。

        识别顺序:显式编号 → 已发送语录消息映射 → 回填内容中的编号 →
        被收录的原始消息 ID → get_forward_msg 解析收录信息节点编号。
        """
        code = str(code or "").strip()
        if code:
            # 0) 显式编号优先
            return self.storage.find_quote_by_id_prefix(code)
        reply_id = (
            str(getattr(reply, "id", "") or "").strip()
            if reply is not None
            else ""
        )
        if not reply_id:
            return None
        umo = str(event.unified_msg_origin or "")
        # 1) 已发送语录消息映射(回复 bot 发送的语录消息,发送时经平台 API 记录)
        quote_id = self.storage.find_quote_id_by_sent_message(umo, reply_id)
        if quote_id:
            quote = self.storage.get_quote_by_id(umo, quote_id)
            if quote is not None:
                return quote
        # 2) 被回复消息回填内容中的编号(适配器回填 Reply.chain/message_str,
        #    文本回执与合并转发收录信息均嵌入编号;纯本地解析)
        text_code = self.resolve_reply_text_code(reply)
        if text_code:
            quote = self.storage.find_quote_by_id_prefix(text_code)
            if quote is not None:
                return quote
        # 3) 被收录的原始消息(回复原消息删除,按原消息 ID 查找)
        quote = self.storage.find_quote_by_message_id(umo, reply_id)
        if quote is not None:
            return quote
        # 4) 回退:get_forward_msg 解析收录信息节点编号(旧语录消息)
        resolved = await self.resolve_reply_quote_id(event, reply)
        if resolved:
            return self.storage.find_quote_by_id_prefix(resolved)
        return None

    # ---------- 语录抽取 ----------

    def random_quote(self, event: AstrMessageEvent, owner: str = ""):
        """从语录库中随机抽取一条语录(可指定归属人)。

        指定归属人时 QQ 号优先精确匹配,其次昵称;当前会话无结果时按
        fallback_global 配置回退到从所有会话的语录库中抽取。

        Returns:
            (quote, owner_id, owner_name);quote 为 None 表示无结果。
        """
        umo = event.unified_msg_origin
        owner_id, owner_name = self.resolve_owner_target(event, owner)
        if owner_id or owner_name:
            # 抽取指定归属人的语录(QQ 号优先精确匹配,其次昵称)
            quote = self.storage.random_quote_by_owner(
                umo, owner_id, owner_name
            )
            if quote is None and self.cfg_bool("fallback_global", False):
                quote = self.storage.random_quote_by_owner_any(
                    owner_id, owner_name
                )
        else:
            quote = self.storage.random_quote(umo)
            if quote is None and self.cfg_bool("fallback_global", False):
                # 当前会话语录库为空时,按配置回退到从所有会话的语录库中抽取
                quote = self.storage.random_quote_any()
        return quote, owner_id, owner_name

    # ---------- 发送与映射记录 ----------

    def record_sent(self, event: AstrMessageEvent, sent_ids, quote) -> None:
        """记录已发送语录消息与语录的映射(删除时精确定位)。"""
        if not sent_ids:
            return
        umo = str(event.unified_msg_origin or "")
        for mid in sent_ids:
            self.storage.record_sent_message(umo, mid, quote.id)

    async def send_and_record(
        self, event: AstrMessageEvent, nodes, quote
    ) -> bool:
        """QQ 平台直发合并转发并记录语录消息映射;失败返回 False。

        记录语录消息与语录的映射后,回复语录消息删除时可精确定位。
        非 QQ 平台、无 bot 接口或发送失败时返回 False(调用方回退框架发送)。
        """
        try:
            payload = await Nodes(nodes=nodes).to_dict()
        except Exception as e:
            logger.warning(f"[archiver] build forward payload failed: {e}")
            return False
        message_id = await self.napcat.send_forward_msg(event, payload)
        if not message_id:
            return False
        self.record_sent(event, [message_id], quote)
        return True

    # ---------- 回执输出 ----------

    async def _emit_nodes(self, event, nodes, quote) -> tuple[str, object]:
        """QQ 平台直发合并转发并记录映射;失败回退框架发送。

        Returns:
            ("sent", None):已直发并记录映射;
            ("chain", [Nodes(nodes=nodes)]):需框架回退发送。
        """
        if await self.send_and_record(event, nodes, quote):
            return "sent", None
        return "chain", [Nodes(nodes=nodes)]

    async def emit_archive(self, event, quote, count: int) -> tuple[str, object]:
        """构造收录回执输出。

        Returns:
            ("sent", None):QQ 平台已直发合并转发;
            ("chain", [Nodes(...)]):需框架回退发送合并转发;
            ("plain", text):需框架发送文本回执(不支持合并转发的平台)。
        """
        if not self.use_forward(event):
            return (
                "plain",
                (
                    f"已收录「{quote.sender_name}」的发言，"
                    f"本会话语录库共 {count} 条"
                    f"（编号：{self.quote_code(quote)}）"
                ),
            )
        # 以聊天记录(合并转发)形式回执
        if quote.forward_nodes:
            # 聊天记录:原样回显引用的聊天记录,最后一条为收录信息
            nodes: list = self.build_forward_quote_nodes(
                quote,
                info_text=(
                    f"已收录，本会话语录库共 {count} 条"
                    f"（编号：{self.quote_code(quote)}）"
                ),
            )
        else:
            nodes = [self.quote_node(quote)]
            nodes.append(
                Node(
                    content=[
                        Plain(
                            f"已收录，本会话语录库共 {count} 条"
                            f"（编号：{self.quote_code(quote)}）"
                        )
                    ],
                    name=RECEIPT_NODE_NAME,
                )
            )
        return await self._emit_nodes(event, nodes, quote)

    async def emit_random(self, event, quote) -> tuple[str, object]:
        """构造随机语录输出。

        Returns:
            ("sent", None):QQ 平台已直发合并转发;
            ("chain", payload):需框架发送(Nodes 或文本+图片组件列表)。
        """
        if not self.use_forward(event):
            # 文本回退(不支持合并转发的平台可关闭 use_forward)
            chain: list = [Plain(self.random_reply_text(quote))]
            for image in quote.images:
                comp = self.image_component(image)
                if comp is not None:
                    chain.append(comp)
            return "chain", chain
        # 以聊天记录(合并转发)形式发送,更直观优雅
        if quote.forward_nodes:
            # 聊天记录语录:保持聊天记录形态,最后一条为收录信息
            nodes: list = self.build_forward_quote_nodes(quote)
        else:
            nodes = [self.quote_node(quote)]
            info = self.archive_info_text(quote)
            if info:
                nodes.append(Node(content=[Plain(info)], name=INFO_NODE_NAME))
        return await self._emit_nodes(event, nodes, quote)