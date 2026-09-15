"""语录插件 WebUI 管理面板:REST API 注册。

通过 Context.register_web_api 注册管理 API,供 WebUI 插件页面
(pages/quotes/index.html)经 AstrBotPluginPage bridge 调用。
"""

from __future__ import annotations

import base64
import uuid

from astrbot.api import logger
from astrbot.api.web import request

from .constants import PLUGIN_NAME
from .models import Quote
from .storage import QuoteStorage

# 管理面板默认分页大小
DEFAULT_PAGE_SIZE = 20


def register_web_apis(storage: QuoteStorage, context) -> None:
    """注册语录管理 API(路由第一段为插件名,由 dashboard 匹配分发)。"""
    api = QuoteWebApi(storage)
    prefix = f"/{PLUGIN_NAME}/api"
    context.register_web_api(
        f"{prefix}/quotes", api.list_quotes, ["GET"], "分页查询语录(支持关键词/会话筛选)"
    )
    context.register_web_api(
        f"{prefix}/sessions", api.list_sessions, ["GET"], "查询会话统计"
    )
    context.register_web_api(
        f"{prefix}/image", api.get_image, ["GET"], "获取语录图片(base64 或远程 URL)"
    )
    context.register_web_api(
        f"{prefix}/quote_detail", api.get_quote_detail, ["GET"], "获取语录详情(含聊天记录内容)"
    )
    context.register_web_api(
        f"{prefix}/quotes/delete", api.delete_quote, ["POST"], "删除指定语录"
    )
    context.register_web_api(
        f"{prefix}/quotes/import", api.import_quotes, ["POST"], "批量导入语录(JSON)"
    )


def _ok(data) -> dict:
    return {"status": "ok", "data": data}


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


# 图片文件后缀 → MIME 类型
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _image_mime(suffix: str) -> str:
    return _IMAGE_MIME.get(suffix.lower(), "application/octet-stream")


class QuoteWebApi:
    """语录管理 API handler。

    handler 仅接收路由占位符参数;请求 query/body 通过
    astrbot.api.web.request 模块级代理访问(框架已绑定上下文)。
    """

    def __init__(self, storage: QuoteStorage):
        self.storage = storage

    # ---------- 查看 ----------

    async def list_quotes(self, **_kwargs) -> dict:
        """GET {prefix}/quotes?page=&page_size=&keyword=&session= 分页查询。"""
        try:
            query = request.query
            session = str(query.get("session", "") or "").strip() or None
            keyword = str(query.get("keyword", "") or "").strip() or None
            quotes, total = self.storage.list_quotes(
                session=session,
                keyword=keyword,
                page=query.get("page", 1),
                page_size=query.get("page_size", DEFAULT_PAGE_SIZE),
            )
            return _ok(
                {
                    "quotes": [self._quote_payload(q) for q in quotes],
                    "total": total,
                }
            )
        except Exception as e:
            logger.error(f"[archiver] webui list quotes failed: {e}")
            return _err(f"查询语录失败:{e}")

    async def list_sessions(self, **_kwargs) -> dict:
        """GET {prefix}/sessions 查询会话统计(筛选下拉)。"""
        try:
            return _ok({"sessions": self.storage.list_sessions()})
        except Exception as e:
            logger.error(f"[archiver] webui list sessions failed: {e}")
            return _err(f"查询会话失败:{e}")

    async def get_quote_detail(self, **_kwargs) -> dict:
        """GET {prefix}/quote_detail?quote_id=&session= 获取语录详情。

        含聊天记录(合并转发)子消息的发送人/文本/图片数量,供 WebUI
        弹窗按需展示完整内容;图片本体经 {prefix}/image 按序号加载。
        """
        try:
            query = request.query
            quote_id = str(query.get("quote_id", "") or "").strip()
            if not quote_id:
                return _err("缺少语录编号(quote_id)")
            session = str(query.get("session", "") or "").strip()
            quote = self._locate_quote(quote_id, session)
            if quote is None:
                return _err("没有找到该语录,请刷新后重试")
            return _ok(self._detail_payload(quote))
        except Exception as e:
            logger.error(f"[archiver] webui quote detail failed: {e}")
            return _err(f"获取语录详情失败:{e}")

    async def get_image(self, **_kwargs) -> dict:
        """GET {prefix}/image?quote_id=&index=&node= 获取语录图片。

        node 为 -1 表示主消息图片,node >= 0 表示聊天记录第 node 条子消息
        的图片。优先返回本地存档(base64,经 <img data:URL> 直接展示,规避
        <img src> 无法携带 dashboard 认证头的问题);无本地存档时返回远程
        URL 由前端回退展示。
        """
        try:
            query = request.query
            quote_id = str(query.get("quote_id", "") or "").strip()
            if not quote_id:
                return _err("缺少语录编号(quote_id)")
            session = str(query.get("session", "") or "").strip()
            try:
                index = int(query.get("index", 0))
                node = int(query.get("node", -1))
            except (TypeError, ValueError):
                return _err("图片序号格式错误")
            quote = self._locate_quote(quote_id, session)
            if quote is None:
                return _err("没有找到该语录,请刷新后重试")
            images = self._images_of(quote, node)
            if images is None:
                return _err("聊天记录子消息不存在")
            if index < 0 or index >= len(images):
                return _err("图片序号超出范围")
            item = images[index]
            rel = str(item.get("path") or "").strip()
            if rel:
                local = self.storage.resolve_image_path(rel)
                if local is not None:
                    data = local.read_bytes()
                    return _ok(
                        {
                            "mime": _image_mime(local.suffix),
                            "b64": base64.b64encode(data).decode("ascii"),
                        }
                    )
            url = str(item.get("url") or item.get("file") or "").strip()
            if url:
                return _ok({"mime": "", "url": url})
            return _err("图片文件不存在")
        except Exception as e:
            logger.error(f"[archiver] webui get image failed: {e}")
            return _err(f"获取图片失败:{e}")

    # ---------- 删除 ----------

    async def delete_quote(self, **_kwargs) -> dict:
        """POST {prefix}/quotes/delete,body: {"id": "...", "session": "..."}。

        session 可选:提供时按会话精确定位;仅提供编号(完整 ID 或前 8 位)
        时在全库中查找。
        """
        try:
            body = await request.json(default={})
            if not isinstance(body, dict):
                return _err("请求体格式错误,应为 JSON 对象")
            quote_id = str(body.get("id") or "").strip()
            if not quote_id:
                return _err("缺少语录编号(id)")
            session = str(body.get("session") or "").strip()
            quote = self._locate_quote(quote_id, session)
            if quote is None:
                return _err("没有找到该语录,请刷新后重试")
            self.storage.delete_quote(quote.session, quote.id)
            # 同步清理该语录的已发送消息映射记录
            self.storage.delete_sent_message(quote.session, quote.id)
            logger.info(
                f"[archiver] webui deleted quote {quote.id} from {quote.session}"
            )
            return _ok({"deleted": self._quote_payload(quote)})
        except Exception as e:
            logger.error(f"[archiver] webui delete quote failed: {e}")
            return _err(f"删除语录失败:{e}")

    # ---------- 导入 ----------

    async def import_quotes(self, **_kwargs) -> dict:
        """POST {prefix}/quotes/import,body: {"quotes": [...]}。

        兼容本插件导出格式与旧版 quotes.json({sessions:{...}} 或扁平数组);
        按语录 ID 去重,ID 已存在的跳过不覆盖。
        """
        try:
            body = await request.json(default={})
            if not isinstance(body, dict):
                return _err("请求体格式错误,应为 JSON 对象")
            quotes = self._parse_quotes_payload(body.get("quotes", body))
            if not quotes:
                return _err("没有解析到有效的语录数据")
            added, skipped = self.storage.import_quotes(quotes)
            logger.info(
                f"[archiver] webui imported quotes: added={added}, skipped={skipped}"
            )
            return _ok({"added": added, "skipped": skipped})
        except Exception as e:
            logger.error(f"[archiver] webui import quotes failed: {e}")
            return _err(f"导入语录失败:{e}")

    # ---------- 内部辅助 ----------

    def _locate_quote(self, quote_id: str, session: str) -> Quote | None:
        """定位语录:会话+编号精确 → 全库按编号精确/前缀匹配。"""
        if session:
            quote = self.storage.get_quote_by_id(session, quote_id)
            if quote is not None:
                return quote
        return self.storage.find_quote_by_id_prefix(quote_id)

    def _parse_quotes_payload(self, raw) -> list[Quote]:
        """解析导入数据,兼容本插件导出格式与旧版 quotes.json 结构。"""
        items: list = []
        if isinstance(raw, list):
            # 扁平数组(旧版 quotes.json 或导出数组)
            items = raw
        elif isinstance(raw, dict):
            sessions = raw.get("sessions")
            if isinstance(sessions, dict):
                # 旧版 {sessions: {umo: [...]}}
                for umo, lst in sessions.items():
                    if not isinstance(lst, list):
                        continue
                    for item in lst:
                        if not isinstance(item, dict):
                            continue
                        merged = dict(item)
                        merged.setdefault("session", str(umo))
                        items.append(merged)
            else:
                inner = raw.get("quotes")
                if isinstance(inner, list):
                    items = inner
        quotes: list[Quote] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                data = dict(item)
                if not str(data.get("id") or "").strip():
                    data["id"] = uuid.uuid4().hex  # 缺失 ID 时补齐便于管理
                quotes.append(Quote.from_dict(data))
            except Exception as e:
                logger.warning(f"[archiver] webui skip invalid quote item: {e}")
        return quotes

    @staticmethod
    def _images_of(quote: Quote, node: int) -> list[dict[str, str]] | None:
        """按 node 取图片列表:-1 为主消息;>=0 为聊天记录子消息;越界返回 None。"""
        if node < 0:
            return quote.images
        if node < len(quote.forward_nodes):
            return quote.forward_nodes[node].get("images") or []
        return None

    @staticmethod
    def _detail_payload(quote: Quote) -> dict:
        """序列化语录详情(含聊天记录子消息,不含图片本体)。"""
        return {
            "id": quote.id,
            "code": quote.id[:8],
            "sender_id": quote.sender_id,
            "sender_name": quote.sender_name,
            "text": quote.text,
            "session": quote.session,
            "image_count": len(quote.images),
            "archived_by_name": quote.archived_by_name,
            "archived_at_ts": quote.archived_at_ts,
            "forward_nodes": [
                {
                    "sender_name": str(node.get("sender_name") or ""),
                    "text": str(node.get("text") or ""),
                    "image_count": len(node.get("images") or []),
                }
                for node in quote.forward_nodes
            ],
        }

    @staticmethod
    def _quote_payload(quote: Quote) -> dict:
        """序列化语录为 WebUI 列表项。"""
        return {
            "id": quote.id,
            "code": quote.id[:8],
            "session": quote.session,
            "message_id": quote.message_id,
            "sender_id": quote.sender_id,
            "sender_name": quote.sender_name,
            "text": quote.text,
            "image_count": len(quote.images),
            "forward_count": len(quote.forward_nodes),
            "forward_images": [
                len(node.get("images") or []) for node in quote.forward_nodes
            ],
            "archived_by_name": quote.archived_by_name,
            "archived_at_ts": quote.archived_at_ts,
        }