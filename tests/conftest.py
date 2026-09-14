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

    class _At:
        type = _ComponentType.At

        def __init__(self, qq="", name=""):
            self.qq = qq
            self.name = name

    fake_components.ComponentType = _ComponentType
    fake_components.Plain = _Plain
    fake_components.Image = _Image
    fake_components.Reply = _Reply
    fake_components.At = _At

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


_install_astrbot_stubs()
sys.path.insert(0, str(PLUGINS_DIR))

import pytest  # noqa: E402

from astrbot_plugin_archiver.storage import Quote, QuoteStorage  # noqa: E402

UMO_GROUP = "aiocqhttp:GroupMessage:12345"
UMO_OTHER = "aiocqhttp:GroupMessage:99999"


class FakeContext:
    pass


class FakeEvent:
    """模拟 AstrMessageEvent:支持在消息链中携带引用组件(模拟回复消息)。"""

    def __init__(self, uid="10001", unified_msg_origin=UMO_GROUP, message=None):
        self._uid = uid
        self.message_str = "入典"
        self.message_obj = types.SimpleNamespace(message=list(message or []))
        self.unified_msg_origin = unified_msg_origin or UMO_GROUP

    def get_messages(self):
        return self.message_obj.message

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "tester"

    def get_platform_name(self):
        return "aiocqhttp"

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


def make_reply_event(reply=None, uid="10001", umo=UMO_GROUP) -> FakeEvent:
    """构造一条(可含引用的)回复消息事件。"""
    message = [reply] if reply is not None else []
    return FakeEvent(uid=uid, unified_msg_origin=umo, message=message)


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
