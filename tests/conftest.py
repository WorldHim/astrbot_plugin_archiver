"""pytest 全局配置:以最小 stub 替代 astrbot 运行时,使插件模块可独立导入测试。

测试不触碰真实 data/plugin_data——storage 的数据目录由 fixture 重定向到 tmp_path。
"""
import logging
import sys
import types
from pathlib import Path

# tests/ -> 插件目录 -> data/plugins(包导入根)
PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent

# storage 数据目录占位,由 fixture 重定向到 tmp_path
_BASE = [str(Path(__file__).resolve().parent)]


def _install_astrbot_stubs():
    if "astrbot" in sys.modules:
        return

    fake_api = types.ModuleType("astrbot.api")
    fake_api.logger = logging.getLogger("astrbot-plugin-archiver-test")
    fake_astrbot = types.ModuleType("astrbot")
    fake_astrbot.api = fake_api

    fake_event_mod = types.ModuleType("astrbot.api.event")

    class _AstrMessageEvent:
        pass

    fake_event_mod.AstrMessageEvent = _AstrMessageEvent
    fake_event_mod.filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda f: f)
    )

    fake_components = types.ModuleType("astrbot.api.message_components")

    class _ComponentType:
        # 与 astrbot.core.message.components.ComponentType(str Enum)对齐:
        # 成员值为字符串,使 "Plain" == ComponentType.Plain 在真实/测试环境都成立
        Plain = "Plain"
        Image = "Image"
        Record = "Record"
        Video = "Video"
        File = "File"
        Face = "Face"
        At = "At"
        Node = "Node"
        Nodes = "Nodes"
        Reply = "Reply"
        Forward = "Forward"

    class _Plain:
        type = _ComponentType.Plain

        def __init__(self, text: str, convert: bool = True):
            self.text = text

    class _Image:
        type = _ComponentType.Image

        def __init__(self, file=None, **kwargs):
            self.file = file
            for k, v in kwargs.items():
                setattr(self, k, v)

        async def convert_to_file_path(self):
            """模拟平台下载:测试通过 local_path 属性预置本地路径,未设置视为下载失败。"""
            path = getattr(self, "local_path", None)
            if path is None:
                raise ValueError("simulated image download failure")
            return str(path)

        @staticmethod
        def fromFileSystem(path, **kwargs):
            img = _Image(file=str(path), **kwargs)
            img.path = str(path)
            return img

    class _Reply:
        type = _ComponentType.Reply

        def __init__(self, **kwargs):
            self.id = kwargs.get("id", "")
            self.chain = kwargs.get("chain", [])
            self.sender_id = kwargs.get("sender_id", 0)
            self.sender_nickname = kwargs.get("sender_nickname", "")
            self.time = kwargs.get("time", 0)
            self.message_str = kwargs.get("message_str", "")

    class _Node:
        type = _ComponentType.Node

        def __init__(self, content, **kwargs):
            # 对齐真实 Node:content 为组件列表(传 Node 时包装为单元素列表)
            self.content = [content] if isinstance(content, _Node) else list(content)
            self.uin = kwargs.get("uin", "0")
            self.name = kwargs.get("name", "")
            for k, v in kwargs.items():
                setattr(self, k, v)

        async def to_dict(self):
            """模拟真实 Node.to_dict:转换为 OneBot node 段。"""
            content = [
                {"type": "text", "data": {"text": getattr(s, "text", "")}}
                for s in getattr(self, "content", [])
                if getattr(s, "type", None) == "Plain"
            ]
            return {
                "type": "node",
                "data": {
                    "user_id": str(getattr(self, "uin", "0") or "0"),
                    "nickname": getattr(self, "name", "") or "",
                    "content": content,
                },
            }

    class _Nodes:
        type = _ComponentType.Nodes

        def __init__(self, nodes, **kwargs):
            self.nodes = list(nodes)
            for k, v in kwargs.items():
                setattr(self, k, v)

        async def to_dict(self):
            """模拟真实 Nodes.to_dict:转换为 OneBot JSON 格式。"""
            return {"messages": [await n.to_dict() for n in getattr(self, "nodes", [])]}

    class _At:
        type = _ComponentType.At

        def __init__(self, qq="", name=""):
            self.qq = qq
            self.name = name

    class _Forward:
        type = _ComponentType.Forward

        def __init__(self, id=""):
            self.id = id

    fake_components.ComponentType = _ComponentType
    fake_components.Plain = _Plain
    fake_components.Image = _Image
    fake_components.Reply = _Reply
    fake_components.At = _At
    fake_components.Node = _Node
    fake_components.Nodes = _Nodes
    fake_components.Forward = _Forward

    # quoted_message stub:模拟 AstrBot 内置引用消息提取器
    # (Node/Nodes 从组件链内嵌 content 提取"发送人: 内容";
    #  Forward 无内嵌内容,从事件预置的 _forward_text 模拟远程拉取)
    import re

    _FORWARD_PLACEHOLDER = re.compile(
        r"^(?:[\(\[]?[^\]:\)]*[\)\]]?\s*:\s*)?\[(?:forward message|转发消息|合并转发)\]$",
        flags=re.IGNORECASE,
    )

    def _text_from_chain(chain) -> str:
        texts: list[str] = []
        for sub in chain or []:
            if getattr(sub, "type", None) == "Plain":
                t = str(getattr(sub, "text", "") or "").strip()
                if t:
                    texts.append(t)
        return " ".join(texts)

    def _extract_from_forward_chain(chain, event) -> str | None:
        parts: list[str] = []
        for comp in chain or []:
            ctype = getattr(comp, "type", None)
            if ctype == "Node":
                name = (
                    getattr(comp, "name", "") or getattr(comp, "uin", "") or "Unknown User"
                )
                t = _text_from_chain(getattr(comp, "content", []))
                if t:
                    parts.append(f"{name}: {t}")
            elif ctype == "Nodes":
                for node in getattr(comp, "nodes", []) or []:
                    name = (
                        getattr(node, "name", "")
                        or getattr(node, "uin", "")
                        or "Unknown User"
                    )
                    t = _text_from_chain(getattr(node, "content", []))
                    if t:
                        parts.append(f"{name}: {t}")
            elif ctype == "Forward":
                fetched = getattr(event, "_forward_text", None)
                parts.append(fetched if fetched else "[转发消息]")
        return "\n".join(parts).strip() or None

    async def _extract_quoted_message_text(event, reply_component=None, settings=None):
        reply = reply_component
        if reply is None:
            for comp in event.get_messages():
                if getattr(comp, "type", None) == "Reply":
                    reply = comp
                    break
        if reply is None:
            return None
        return _extract_from_forward_chain(getattr(reply, "chain", []), event)

    class _ReplyChainParser:
        @staticmethod
        def is_forward_placeholder_only_text(text):
            if not isinstance(text, str):
                return False
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                return False
            return all(_FORWARD_PLACEHOLDER.match(l) for l in lines)

    fake_core = types.ModuleType("astrbot.core")
    fake_utils = types.ModuleType("astrbot.core.utils")
    fake_quoted = types.ModuleType("astrbot.core.utils.quoted_message")
    fake_chain_parser = types.ModuleType("astrbot.core.utils.quoted_message.chain_parser")
    fake_quoted.extract_quoted_message_text = _extract_quoted_message_text
    fake_chain_parser.ReplyChainParser = _ReplyChainParser
    fake_quoted.chain_parser = fake_chain_parser
    fake_core.utils = fake_utils
    fake_utils.quoted_message = fake_quoted
    fake_astrbot.core = fake_core

    fake_star_mod = types.ModuleType("astrbot.api.star")

    class _Context:
        pass

    class _Star:
        def __init__(self, context, config=None):
            self.context = context

    def _register(name, author, desc, ver):
        def deco(cls):
            cls.name = name
            return cls

        return deco

    fake_star_mod.Context = _Context
    fake_star_mod.Star = _Star
    fake_star_mod.register = _register
    fake_star_mod.StarTools = types.SimpleNamespace(
        get_data_dir=lambda n: Path(_BASE[0]) / n
    )

    sys.modules["astrbot"] = fake_astrbot
    sys.modules["astrbot.api"] = fake_api
    sys.modules["astrbot.api.event"] = fake_event_mod
    sys.modules["astrbot.api.message_components"] = fake_components
    sys.modules["astrbot.api.star"] = fake_star_mod
    sys.modules["astrbot.core"] = fake_core
    sys.modules["astrbot.core.utils"] = fake_utils
    sys.modules["astrbot.core.utils.quoted_message"] = fake_quoted
    sys.modules["astrbot.core.utils.quoted_message.chain_parser"] = fake_chain_parser


_install_astrbot_stubs()
sys.path.insert(0, str(PLUGINS_DIR))

import pytest  # noqa: E402

from astrbot_plugin_archiver.storage import Quote, QuoteStorage  # noqa: E402

UMO_GROUP = "aiocqhttp:GroupMessage:12345"
UMO_OTHER = "aiocqhttp:GroupMessage:99999"


class FakeContext:
    pass


class FakeBotApi:
    """模拟 OneBot API:返回构造时预置的 action 响应。"""

    def __init__(self, responses=None):
        self.responses = responses or {}

    async def call_action(self, action, **params):
        return self.responses.get(action)


class FakeBot:
    def __init__(self, responses=None):
        self.api = FakeBotApi(responses)


class FakeEvent:
    """模拟 AstrMessageEvent:支持在消息链中携带引用组件(模拟回复消息)。"""

    def __init__(
        self,
        uid="10001",
        unified_msg_origin=UMO_GROUP,
        message=None,
        platform_name="aiocqhttp",
        forward_text=None,
        bot_responses=None,
        self_id="",
    ):
        self._uid = uid
        self._self_id = self_id
        self.message_str = "保存"
        self.message_obj = types.SimpleNamespace(
            message=list(message or []), self_id=self_id
        )
        self.unified_msg_origin = unified_msg_origin or UMO_GROUP
        self._platform_name = platform_name
        # 模拟 get_forward_msg 远程拉取结果(stub 提取器读取)
        self._forward_text = forward_text
        # 模拟 OneBot bot 接口(结构化聊天记录拉取);None 表示平台无 bot 接口
        self.bot = FakeBot(bot_responses) if bot_responses is not None else None

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return self.message_obj.message

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "tester"

    def get_platform_name(self):
        return self._platform_name

    def plain_result(self, msg):
        return ("plain", msg)

    def chain_result(self, chain):
        return ("chain", chain)


def make_reply(**kwargs):
    """构造一个引用组件(模拟回复某条消息时平台回填的 Reply)。"""
    Reply = sys.modules["astrbot.api.message_components"].Reply
    Plain = sys.modules["astrbot.api.message_components"].Plain
    defaults = dict(
        id="msg-001",
        chain=[Plain("哈哈哈哈")],
        sender_id="20002",
        sender_nickname="张三",
        time=1757800000,
        message_str="哈哈哈哈",
    )
    defaults.update(kwargs)
    return Reply(**defaults)


def make_reply_event(
    reply=None,
    uid="10001",
    umo=UMO_GROUP,
    platform_name="aiocqhttp",
    forward_text=None,
    bot_responses=None,
) -> FakeEvent:
    """构造一条(可含引用的)回复消息事件。

    forward_text 模拟合并转发展开文本;bot_responses 模拟 OneBot API 响应。
    """
    message = [reply] if reply is not None else []
    return FakeEvent(
        uid=uid,
        unified_msg_origin=umo,
        message=message,
        platform_name=platform_name,
        forward_text=forward_text,
        bot_responses=bot_responses,
    )


def make_quote(umo: str = UMO_GROUP, message_id: str = "msg-001", **kwargs) -> Quote:
    defaults = dict(
        id=message_id or "q-uuid",
        session=umo,
        message_id=message_id,
        sender_id="20002",
        sender_name="张三",
        text="哈哈哈哈",
        images=[],
        archived_by="10001",
        archived_by_name="tester",
        archived_at_ts=0.0,
    )
    defaults.update(kwargs)
    return Quote(**defaults)


def write_png_bytes(size=(8, 8), color=(200, 30, 40)) -> bytes:
    """用 Pillow 生成一张真实 PNG 字节(测试图片存档/压缩用)。"""
    import io

    from PIL import Image as PILImage

    im = PILImage.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def write_png(path, size=(8, 8), color=(200, 30, 40)) -> Path:
    """用 Pillow 生成一张真实 PNG 文件,返回路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_png_bytes(size, color))
    return path


def collect(agen):
    """驱动 async generator handler,收集其产出的结果。"""
    import asyncio

    async def run():
        return [r async for r in agen]

    return asyncio.run(run())


@pytest.fixture
def storage(tmp_path):
    """QuoteStorage 实例,数据目录重定向到 pytest 临时目录。"""
    _BASE[0] = str(tmp_path)
    return QuoteStorage("astrbot_plugin_archiver")


@pytest.fixture
def plugin(tmp_path):
    """ArchiverPlugin 实例,数据目录重定向到 pytest 临时目录。"""
    from astrbot_plugin_archiver.main import ArchiverPlugin

    _BASE[0] = str(tmp_path)
    return ArchiverPlugin(FakeContext())
