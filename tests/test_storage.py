"""QuoteStorage 单元测试:原子写、备份恢复、缓存、去重、上限淘汰、随机抽取。"""
import json
import os
import time

from conftest import UMO_GROUP, UMO_OTHER, make_quote


class TestRoundtrip:
    def test_load_missing_returns_empty(self, storage):
        assert storage.load_quotes() == {}

    def test_add_and_load(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        loaded = storage.load_quotes()
        assert len(loaded[UMO_GROUP]) == 1
        q = loaded[UMO_GROUP][0]
        assert q.sender_id == "20002"
        assert q.sender_name == "张三"
        assert q.text == "哈哈哈哈"
        assert q.message_id == "msg-001"

    def test_sessions_isolated(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        storage.add_quote(UMO_OTHER, make_quote(message_id="msg-002"))
        assert storage.session_count(UMO_GROUP) == 1
        assert storage.session_count(UMO_OTHER) == 1

    def test_json_structure(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        raw = json.loads(storage._quotes_file.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert raw["sessions"][UMO_GROUP][0]["sender_name"] == "张三"

    def test_utf8_bom_readable(self, storage):
        # Windows 记事本等工具保存的 UTF-8 with BOM 文件须能正常读取
        storage.add_quote(UMO_GROUP, make_quote())
        raw_bytes = storage._quotes_file.read_bytes()
        storage._quotes_file.write_bytes(b"\xef\xbb\xbf" + raw_bytes)
        os.utime(storage._quotes_file, (time.time() + 10, time.time() + 10))
        data = storage.load_quotes()
        assert data[UMO_GROUP][0].text == "哈哈哈哈"


class TestAtomicAndBackup:
    def test_atomic_no_tmp_left(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        assert not storage._quotes_file.with_suffix(".json.tmp").exists()

    def test_second_save_creates_bak_with_previous_version(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        storage.add_quote(UMO_GROUP, make_quote(message_id="m2"))
        bak = storage._quotes_file.with_suffix(".json.bak")
        assert bak.exists()
        data = json.loads(bak.read_text(encoding="utf-8"))
        # 备份保留上一版(只有第一条)
        assert len(data["sessions"][UMO_GROUP]) == 1

    def test_corrupted_main_restored_from_bak(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        storage.add_quote(UMO_GROUP, make_quote(message_id="m2"))
        storage._quotes_file.write_text("bad", encoding="utf-8")
        os.utime(storage._quotes_file, (time.time() + 10, time.time() + 10))
        loaded = storage.load_quotes()
        # 主文件损坏 → 从备份恢复上一版(1 条)
        assert storage.session_count(UMO_GROUP) == 1

    def test_missing_main_restored_from_bak(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        storage.add_quote(UMO_GROUP, make_quote(message_id="m2"))
        storage._quotes_file.unlink()
        loaded = storage.load_quotes()
        assert storage.session_count(UMO_GROUP) == 1
        # 恢复结果进入缓存,再次 load 命中同一对象
        assert storage.load_quotes() is loaded

    def test_failed_save_keeps_old_data(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        import pathlib

        original_replace = pathlib.Path.replace

        def broken_replace(self, target):
            raise OSError("disk full")

        pathlib.Path.replace = broken_replace
        try:
            storage.add_quote(UMO_GROUP, make_quote(message_id="m2"))
        finally:
            pathlib.Path.replace = original_replace
        # 保存失败 → 磁盘上仍是旧数据
        assert storage.session_count(UMO_GROUP) == 1

class TestCache:
    def test_cache_hit_same_object(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        assert storage.load_quotes() is storage.load_quotes()

    def test_save_refreshes_cache(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        assert storage.session_count(UMO_GROUP) == 1
        storage.add_quote(UMO_GROUP, make_quote(message_id="m2"))
        # 若缓存未刷新,这里会读到旧值 1
        assert storage.session_count(UMO_GROUP) == 2


class TestDedupAndLimit:
    def test_has_message(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        assert storage.has_message(UMO_GROUP, "m1") is True
        assert storage.has_message(UMO_GROUP, "m2") is False
        # 空消息 ID 不参与去重
        assert storage.has_message(UMO_GROUP, "") is False

    def test_limit_trims_oldest(self, storage):
        for i in range(5):
            storage.add_quote(UMO_GROUP, make_quote(message_id=f"m{i}"), limit=3)
        quotes = storage.session_quotes(UMO_GROUP)
        assert [q.message_id for q in quotes] == ["m2", "m3", "m4"]

    def test_limit_zero_unlimited(self, storage):
        for i in range(5):
            storage.add_quote(UMO_GROUP, make_quote(message_id=f"m{i}"), limit=0)
        assert storage.session_count(UMO_GROUP) == 5


class TestRandom:
    def test_random_from_session(self, storage):
        ids = {f"m{i}" for i in range(10)}
        for i in range(10):
            storage.add_quote(UMO_GROUP, make_quote(message_id=f"m{i}"))
        q = storage.random_quote(UMO_GROUP)
        assert q is not None
        assert q.message_id in ids

    def test_random_empty_returns_none(self, storage):
        assert storage.random_quote(UMO_GROUP) is None

    def test_random_any(self, storage):
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1", sender_name="a"))
        storage.add_quote(UMO_OTHER, make_quote(message_id="m2", sender_name="b"))
        q = storage.random_quote_any()
        assert q is not None
        assert q.message_id in {"m1", "m2"}

    def test_random_any_empty_returns_none(self, storage):
        assert storage.random_quote_any() is None


class TestQuoteDataclass:
    def test_to_dict_from_dict_roundtrip(self):
        from astrbot_plugin_archiver.storage import Quote

        q = make_quote(images=[{"file": "f", "url": "u"}])
        q2 = Quote.from_dict(q.to_dict())
        assert q2 == q

    def test_from_dict_with_missing_fields(self):
        from astrbot_plugin_archiver.storage import Quote

        q = Quote.from_dict({"sender_name": "张三"})
        assert q.sender_name == "张三"
        assert q.text == ""
        assert q.images == []
