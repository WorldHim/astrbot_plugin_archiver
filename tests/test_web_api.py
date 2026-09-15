"""web_api.py 单元测试:WebUI 管理 API(查看/删除/导入)。"""
import asyncio
import sys

from astrbot_plugin_archiver import web_api

from conftest import UMO_GROUP, UMO_OTHER, make_quote, write_png_bytes


def asyncio_run(coro):
    return asyncio.run(coro)


class FakeQuery:
    """dict 风格 query stub。"""

    def __init__(self, items=None):
        self._data = dict(items or {})

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeReq:
    """模拟 PluginRequest:query 参数与 JSON body 可注入。"""

    def __init__(self, query=None, body=None):
        self.query = FakeQuery(query)
        self._body = body

    async def json(self, default=None):
        if self._body is not None:
            return self._body
        return default


def _patch_request(monkeypatch, query=None, body=None):
    req = FakeReq(query, body)
    monkeypatch.setattr(web_api, "request", req)
    return req


class TestRegister:
    def test_registers_four_apis(self, plugin):
        apis = plugin._context.registered_web_apis
        routes = {route for route, _h, _m, _d in apis}
        prefix = "/astrbot_plugin_archiver/api"
        assert f"{prefix}/quotes" in routes
        assert f"{prefix}/sessions" in routes
        assert f"{prefix}/quotes/delete" in routes
        assert f"{prefix}/quotes/import" in routes

    def test_methods(self, plugin):
        apis = {
            route: methods
            for route, _h, methods, _d in plugin._context.registered_web_apis
        }
        prefix = "/astrbot_plugin_archiver/api"
        assert apis[f"{prefix}/quotes"] == ["GET"]
        assert apis[f"{prefix}/sessions"] == ["GET"]
        assert apis[f"{prefix}/quotes/delete"] == ["POST"]
        assert apis[f"{prefix}/quotes/import"] == ["POST"]


class TestListQuotes:
    def test_list_empty(self, plugin, monkeypatch):
        _patch_request(monkeypatch)
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.list_quotes())
        assert res["status"] == "ok"
        assert res["data"]["quotes"] == []
        assert res["data"]["total"] == 0

    def test_list_returns_quotes(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        plugin._storage.add_quote(
            UMO_GROUP, make_quote(message_id="msg-002", id="q-2")
        )
        _patch_request(monkeypatch)
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.list_quotes())
        assert res["status"] == "ok"
        assert res["data"]["total"] == 2
        codes = {q["code"] for q in res["data"]["quotes"]}
        assert "q-2"[:8] in codes

    def test_list_keyword_filter(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote(text="独特的话"))
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(message_id="msg-002", id="q-2", text="普通的话"),
        )
        _patch_request(monkeypatch, query={"keyword": "独特"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.list_quotes())
        assert res["status"] == "ok"
        assert res["data"]["total"] == 1
        assert res["data"]["quotes"][0]["text"] == "独特的话"

    def test_list_session_filter(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        plugin._storage.add_quote(
            UMO_OTHER, make_quote(message_id="msg-002", id="q-2")
        )
        _patch_request(monkeypatch, query={"session": UMO_GROUP})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.list_quotes())
        assert res["status"] == "ok"
        assert res["data"]["total"] == 1

    def test_list_sessions(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        plugin._storage.add_quote(
            UMO_OTHER, make_quote(message_id="msg-002", id="q-2")
        )
        _patch_request(monkeypatch)
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.list_sessions())
        assert res["status"] == "ok"
        counts = {s["session"]: s["count"] for s in res["data"]["sessions"]}
        assert counts[UMO_GROUP] == 1
        assert counts[UMO_OTHER] == 1


class TestDelete:
    def test_delete_by_session_and_id(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote(id="full-uuid-0001"))
        _patch_request(
            monkeypatch, body={"id": "full-uuid-0001", "session": UMO_GROUP}
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.delete_quote())
        assert res["status"] == "ok"
        assert res["data"]["deleted"]["id"] == "full-uuid-0001"
        assert plugin._storage.get_quote_by_id(UMO_GROUP, "full-uuid-0001") is None

    def test_delete_by_code_prefix(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote(id="abcdef123456"))
        _patch_request(monkeypatch, body={"id": "abcdef12"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.delete_quote())
        assert res["status"] == "ok"
        assert plugin._storage.get_quote_by_id(UMO_GROUP, "abcdef123456") is None

    def test_delete_missing_id_returns_error(self, plugin, monkeypatch):
        _patch_request(monkeypatch, body={})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.delete_quote())
        assert res["status"] == "error"

    def test_delete_not_found_returns_error(self, plugin, monkeypatch):
        _patch_request(monkeypatch, body={"id": "no-such-quote"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.delete_quote())
        assert res["status"] == "error"

    def test_delete_cleans_sent_mapping(self, plugin, monkeypatch):
        quote = make_quote(id="with-sent-map")
        plugin._storage.add_quote(UMO_GROUP, quote)
        plugin._storage.record_sent_message(UMO_GROUP, "sent-001", quote.id)
        assert (
            plugin._storage.find_quote_id_by_sent_message(UMO_GROUP, "sent-001")
            == quote.id
        )
        _patch_request(
            monkeypatch, body={"id": "with-sent-map", "session": UMO_GROUP}
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.delete_quote())
        assert res["status"] == "ok"
        assert (
            plugin._storage.find_quote_id_by_sent_message(UMO_GROUP, "sent-001")
            is None
        )

class TestImport:
    def test_import_array(self, plugin, monkeypatch):
        payload = [
            {
                "id": "imp-1",
                "session": UMO_GROUP,
                "sender_id": "30001",
                "sender_name": "李四",
                "text": "导入的话",
            },
            {
                "id": "imp-2",
                "session": UMO_GROUP,
                "sender_id": "30002",
                "sender_name": "王五",
                "text": "另一条",
            },
        ]
        _patch_request(monkeypatch, body={"quotes": payload})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "ok"
        assert res["data"]["added"] == 2
        assert res["data"]["skipped"] == 0
        assert plugin._storage.get_quote_by_id(UMO_GROUP, "imp-1") is not None

    def test_import_legacy_sessions_format(self, plugin, monkeypatch):
        payload = {
            "sessions": {
                UMO_GROUP: [
                    {
                        "id": "legacy-1",
                        "sender_id": "30001",
                        "sender_name": "李四",
                        "text": "旧版",
                    },
                ],
                UMO_OTHER: [
                    {
                        "id": "legacy-2",
                        "sender_id": "30002",
                        "sender_name": "王五",
                        "text": "其他会话",
                    },
                ],
            }
        }
        _patch_request(monkeypatch, body={"quotes": payload})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "ok"
        assert res["data"]["added"] == 2
        assert plugin._storage.get_quote_by_id(UMO_GROUP, "legacy-1") is not None
        assert plugin._storage.get_quote_by_id(UMO_OTHER, "legacy-2") is not None

    def test_import_dedup_skips_existing(self, plugin, monkeypatch):
        plugin._storage.add_quote(UMO_GROUP, make_quote(id="dup-1", text="已有"))
        payload = [
            {
                "id": "dup-1",
                "session": UMO_GROUP,
                "sender_id": "30001",
                "sender_name": "李四",
                "text": "重复",
            },
            {
                "id": "new-1",
                "session": UMO_GROUP,
                "sender_id": "30002",
                "sender_name": "王五",
                "text": "新",
            },
        ]
        _patch_request(monkeypatch, body={"quotes": payload})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "ok"
        assert res["data"]["added"] == 1
        assert res["data"]["skipped"] == 1
        assert plugin._storage.get_quote_by_id(UMO_GROUP, "dup-1").text == "已有"

    def test_import_fills_missing_id(self, plugin, monkeypatch):
        payload = [
            {
                "session": UMO_GROUP,
                "sender_id": "30001",
                "sender_name": "李四",
                "text": "无ID",
            }
        ]
        _patch_request(monkeypatch, body={"quotes": payload})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "ok"
        assert res["data"]["added"] == 1
        quotes, total = plugin._storage.list_quotes()
        assert total == 1
        assert quotes[0].id

    def test_import_invalid_returns_error(self, plugin, monkeypatch):
        _patch_request(monkeypatch, body={"quotes": []})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "error"

    def test_import_bad_body_returns_error(self, plugin, monkeypatch):
        req = FakeReq(body=None)
        monkeypatch.setattr(web_api, "request", req)
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.import_quotes())
        assert res["status"] == "error"


class TestImage:
    def _write_image(self, plugin, name="test.png"):
        plugin._storage._images_dir.mkdir(parents=True, exist_ok=True)
        path = plugin._storage._images_dir / name
        path.write_bytes(write_png_bytes())
        return f"images/{name}"

    def test_image_local_base64(self, plugin, monkeypatch):
        rel = self._write_image(plugin, "test.png")
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(message_id="img-0001", images=[{"path": rel}]),
        )
        _patch_request(
            monkeypatch, query={"quote_id": "img-0001", "index": 0, "node": -1}
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "ok"
        assert res["data"]["mime"] == "image/png"
        assert res["data"]["b64"]

    def test_image_url_fallback(self, plugin, monkeypatch):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                message_id="img-0002", images=[{"url": "https://example.com/a.jpg"}]
            ),
        )
        _patch_request(monkeypatch, query={"quote_id": "img-0002", "index": 0})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "ok"
        assert res["data"]["url"] == "https://example.com/a.jpg"

    def test_image_forward_node(self, plugin, monkeypatch):
        rel = self._write_image(plugin, "node.png")
        q = make_quote(
            message_id="img-0003",
            text="[聊天记录]",
            forward_nodes=[
                {
                    "sender_id": "10001",
                    "sender_name": "A",
                    "text": "子消息",
                    "images": [{"path": rel}],
                }
            ],
        )
        plugin._storage.add_quote(UMO_GROUP, q)
        _patch_request(
            monkeypatch, query={"quote_id": "img-0003", "index": 0, "node": 0}
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "ok"
        assert res["data"]["b64"]

    def test_image_quote_not_found(self, plugin, monkeypatch):
        _patch_request(monkeypatch, query={"quote_id": "no-such"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "error"

    def test_image_index_out_of_range(self, plugin, monkeypatch):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                message_id="img-0004", images=[{"url": "https://example.com/a.jpg"}]
            ),
        )
        _patch_request(monkeypatch, query={"quote_id": "img-0004", "index": 5})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "error"

    def test_image_missing_id(self, plugin, monkeypatch):
        _patch_request(monkeypatch, query={})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_image())
        assert res["status"] == "error"


class TestQuoteDetail:
    def test_detail_forward_nodes(self, plugin, monkeypatch):
        q = make_quote(
            message_id="det-0001",
            text="[聊天记录]",
            forward_nodes=[
                {
                    "sender_id": "10001",
                    "sender_name": "用户A",
                    "text": "第一句",
                    "images": [{"url": "https://example.com/a.jpg"}],
                    "time": 1735689600,
                },
                {
                    "sender_id": "10002",
                    "sender_name": "用户B",
                    "text": "第二句",
                    "images": [],
                    "time": 1735689660,
                },
            ],
        )
        plugin._storage.add_quote(UMO_GROUP, q)
        _patch_request(
            monkeypatch, query={"quote_id": "det-0001", "session": UMO_GROUP}
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_quote_detail())
        assert res["status"] == "ok"
        nodes = res["data"]["forward_nodes"]
        assert len(nodes) == 2
        assert nodes[0]["sender_name"] == "用户A"
        assert nodes[0]["text"] == "第一句"
        assert nodes[0]["image_count"] == 1
        assert nodes[0]["time"] == 1735689600
        assert nodes[1]["image_count"] == 0
        assert nodes[1]["time"] == 1735689660

    def test_detail_plain_quote(self, plugin, monkeypatch):
        plugin._storage.add_quote(
            UMO_GROUP, make_quote(message_id="det-0002", text="普通语录")
        )
        _patch_request(monkeypatch, query={"quote_id": "det-0002"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_quote_detail())
        assert res["status"] == "ok"
        assert res["data"]["forward_nodes"] == []
        assert res["data"]["text"] == "普通语录"

    def test_detail_not_found(self, plugin, monkeypatch):
        _patch_request(monkeypatch, query={"quote_id": "no-such"})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_quote_detail())
        assert res["status"] == "error"

    def test_detail_missing_id(self, plugin, monkeypatch):
        _patch_request(monkeypatch, query={})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.get_quote_detail())
        assert res["status"] == "error"


class TestBatchDelete:
    """POST {prefix}/quotes/batch_delete:批量删除选中的语录。"""

    def test_batch_delete_success(self, plugin, monkeypatch):
        plugin._storage.add_quote(
            UMO_GROUP, make_quote(umo=UMO_GROUP, message_id="bd-0001", text="一")
        )
        plugin._storage.add_quote(
            UMO_GROUP, make_quote(umo=UMO_GROUP, message_id="bd-0002", text="二")
        )
        _patch_request(
            monkeypatch,
            body={
                "items": [
                    {"id": "bd-0001", "session": UMO_GROUP},
                    {"id": "bd-0002", "session": UMO_GROUP},
                ]
            },
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.batch_delete_quotes())
        assert res["status"] == "ok"
        assert len(res["data"]["deleted"]) == 2
        assert res["data"]["failed"] == []
        quotes, total = plugin._storage.list_quotes()
        assert total == 0

    def test_batch_delete_partial_not_found(self, plugin, monkeypatch):
        # 部分语录不存在:存在的一条删除,失败的一条记录原因,互不影响
        plugin._storage.add_quote(
            UMO_GROUP, make_quote(umo=UMO_GROUP, message_id="bd-0003", text="存在")
        )
        _patch_request(
            monkeypatch,
            body={
                "items": [
                    {"id": "bd-0003", "session": UMO_GROUP},
                    {"id": "no-such-2", "session": UMO_GROUP},
                ]
            },
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.batch_delete_quotes())
        assert res["status"] == "ok"
        assert len(res["data"]["deleted"]) == 1
        assert len(res["data"]["failed"]) == 1
        assert res["data"]["failed"][0]["id"] == "no-such-2"

    def test_batch_delete_cleans_sent_mapping(self, plugin, monkeypatch):
        q = make_quote(umo=UMO_GROUP, message_id="bd-0004", text="映射")
        plugin._storage.add_quote(UMO_GROUP, q)
        plugin._storage.record_sent_message(UMO_GROUP, "sent-bd-1", q.id)
        _patch_request(
            monkeypatch,
            body={"items": [{"id": q.id, "session": UMO_GROUP}]},
        )
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.batch_delete_quotes())
        assert res["status"] == "ok"
        assert (
            plugin._storage.find_quote_id_by_sent_message(UMO_GROUP, "sent-bd-1")
            is None
        )

    def test_batch_delete_empty_items(self, plugin, monkeypatch):
        _patch_request(monkeypatch, body={"items": []})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.batch_delete_quotes())
        assert res["status"] == "error"

    def test_batch_delete_missing_items(self, plugin, monkeypatch):
        _patch_request(monkeypatch, body={})
        api = web_api.QuoteWebApi(plugin._storage)
        res = asyncio_run(api.batch_delete_quotes())
        assert res["status"] == "error"
