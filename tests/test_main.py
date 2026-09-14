"""main.py 单元测试:入典收录、去重、无引用提示、随机调用、图片回放、配置回退。"""
import sys
import types

from astrbot.api.message_components import Image, Plain

from astrbot_plugin_archiver import main as archiver_main

from conftest import UMO_GROUP, UMO_OTHER, FakeContext, collect, make_quote
from conftest import make_reply, make_reply_event, write_png, _BASE


def _plugin_with_config(tmp_path, config):
    _BASE[0] = str(tmp_path)
    return archiver_main.ArchiverPlugin(FakeContext(), config=config)


class TestRudian:
    def test_archive_basic(self, plugin):
        event = make_reply_event(make_reply())
        results = collect(plugin.rudian(event))
        assert len(results) == 1
        kind, text = results[0]
        assert kind == "plain"
        assert "张三" in text and "1 条" in text
        # 已写入典库
        quotes = plugin._storage.session_quotes(UMO_GROUP)
        assert len(quotes) == 1
        q = quotes[0]
        assert q.sender_id == "20002"
        assert q.sender_name == "张三"
        assert q.text == "哈哈哈哈"
        assert q.message_id == "msg-001"
        assert q.archived_by == "10001"
        assert q.archived_by_name == "tester"
        assert q.session == UMO_GROUP

    def test_no_reply_prompt(self, plugin):
        event = make_reply_event(None)
        results = collect(plugin.rudian(event))
        assert "回复" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_empty_content_rejected(self, plugin):
        # 引用未回填内容(chain/message_str 均为空) → 收录失败
        reply = make_reply(chain=[], message_str="")
        event = make_reply_event(reply)
        results = collect(plugin.rudian(event))
        assert "无法读取" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_fallback_to_message_str(self, plugin):
        # 平台未回填 chain 时回退到引用解析出的纯文本
        reply = make_reply(chain=[], message_str="只有纯文本")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        assert plugin._storage.session_quotes(UMO_GROUP)[0].text == "只有纯文本"

    def test_sender_nickname_fallback_to_id(self, plugin):
        reply = make_reply(sender_nickname="", sender_id="20002")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_name == "20002"

    def test_dedup_same_message(self, plugin):
        event = make_reply_event(make_reply())
        collect(plugin.rudian(event))
        results = collect(plugin.rudian(event))
        assert "已经在典库" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 1

    def test_message_with_image(self, plugin, tmp_path):
        # 设置 local_path → 模拟平台下载成功,图片应保存到本地
        img = Image(file="http://example.com/a.jpg", url="http://example.com/a.jpg")
        img.local_path = str(write_png(tmp_path / "a.png"))
        reply = make_reply(
            chain=[Plain("看图"), img],
            message_str="看图",
        )
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "看图"
        # 图片已下载保存到本地,并保留 URL 作为回退
        assert q.images[0]["path"].startswith("images/")
        assert (plugin._storage._base_dir / q.images[0]["path"]).is_file()
        assert q.images[0]["url"] == "http://example.com/a.jpg"
        assert "file" not in q.images[0]

    def test_archive_image_download_failure_falls_back_to_url(self, plugin):
        # 未设置 local_path → 模拟下载失败 → 回退为仅记录 URL(与旧版行为一致)
        img = Image(file="http://example.com/a.jpg", url="http://example.com/a.jpg")
        reply = make_reply(chain=[img], message_str="")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.images == [
            {"file": "http://example.com/a.jpg", "url": "http://example.com/a.jpg"}
        ]
        assert "path" not in q.images[0]

    def test_non_text_components_to_placeholder(self, plugin):
        # 表情/语音等以占位符记录(模拟不存在的组件类型)
        face = types.SimpleNamespace(type="Face", id=1)
        record = types.SimpleNamespace(type="Record", file="x")
        at = types.SimpleNamespace(type="At", qq="30003", name="李四")
        reply = make_reply(chain=[face, record, at], message_str="")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[表情]\n[语音]\n@李四"

    def test_session_limit_from_config(self, tmp_path):
        plugin = _plugin_with_config(tmp_path, {"session_quote_limit": 2})
        for i in range(4):
            reply = make_reply(id=f"m{i}", message_str="x", chain=[Plain("x")])
            event = make_reply_event(reply)
            collect(plugin.rudian(event))
        quotes = plugin._storage.session_quotes(UMO_GROUP)
        assert [q.message_id for q in quotes] == ["m2", "m3"]

class TestLaidiandian:
    def test_empty_library_prompt(self, plugin):
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert "典库还是空的" in results[0][1]

    def test_random_replay_text(self, plugin):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        chain = results[0][1]
        assert len(chain) == 1
        assert "张三" in chain[0].text and "哈哈哈哈" in chain[0].text

    def test_random_replay_with_image(self, plugin):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"file": "", "url": "http://example.com/a.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        chain = results[0][1]
        assert len(chain) == 2
        img = chain[1]
        assert img.file == "http://example.com/a.jpg"

    def test_replay_prefers_url_over_file(self, plugin):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"file": "local.jpg", "url": "http://example.com/b.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert results[0][1][1].file == "http://example.com/b.jpg"

    def test_replay_prefers_local_archive(self, plugin, tmp_path):
        # 本地存档存在 → 优先发送本地文件而非 URL
        img = write_png(tmp_path / "local.png")
        rel = plugin._storage.store_image(str(img))
        assert rel is not None
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"path": rel, "url": "http://example.com/a.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        chain = results[0][1]
        assert len(chain) == 2
        assert chain[1].file == str((plugin._storage._base_dir / rel).resolve())

    def test_replay_falls_back_to_url_when_local_missing(self, plugin):
        # 本地存档文件不存在 → 回退发送 URL
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                images=[
                    {"path": "images/ghost.png", "url": "http://example.com/a.jpg"}
                ]
            ),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        chain = results[0][1]
        assert len(chain) == 2
        assert chain[1].file == "http://example.com/a.jpg"

    def test_fallback_global_disabled_by_default(self, plugin):
        # 其他会话有典,本会话为空;默认不开全局回退 → 提示典库为空
        plugin._storage.add_quote(UMO_OTHER, make_quote())
        event = make_reply_event(None, umo="aiocqhttp:GroupMessage:current")
        results = collect(plugin.laidiandian(event))
        assert "典库还是空的" in results[0][1]

    def test_fallback_global_enabled(self, tmp_path):
        plugin = _plugin_with_config(tmp_path, {"fallback_global": True})
        plugin._storage.add_quote(UMO_OTHER, make_quote())
        event = make_reply_event(None, umo="aiocqhttp:GroupMessage:current")
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        assert "张三" in results[0][1][0].text


class TestConfig:
    def test_defaults_without_config(self, plugin):
        assert plugin._cfg_bool("fallback_global", False) is False
        assert plugin._cfg_int("session_quote_limit", 0) == 0

    def test_invalid_values_fallback(self, tmp_path):
        plugin = _plugin_with_config(
            tmp_path, {"session_quote_limit": "abc", "fallback_global": "yes"}
        )
        # 非法整型回退默认值;非空字符串转 bool 为 True
        assert plugin._cfg_int("session_quote_limit", 0) == 0
        assert plugin._cfg_bool("fallback_global", False) is True


class TestMetadata:
    def test_version_consistency(self):
        import re
        from pathlib import Path

        meta = (
            Path(__file__).resolve().parent.parent / "metadata.yaml"
        ).read_text(encoding="utf-8")
        version = re.search(r"^version:\s*v?([0-9.]+)", meta, re.M).group(1)
        assert archiver_main.PLUGIN_VERSION == version
        assert "astrbot_plugin_archiver" in meta
