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

    def test_db_structure(self, storage):
        storage.add_quote(UMO_GROUP, make_quote())
        assert storage._db_file.is_file()
        import sqlite3

        conn = sqlite3.connect(storage._db_file)
        try:
            rows = conn.execute(
                "SELECT sender_name, text FROM quotes WHERE session = ?",
                (UMO_GROUP,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "张三"
        assert rows[0][1] == "哈哈哈哈"

    def test_legacy_json_migrated(self, storage):
        # 旧版 quotes.json(含 BOM)在存储实例初始化时自动迁移进 SQLite
        import sqlite3

        legacy = {
            "version": 1,
            "updated_at_ts": 1789365419.0,
            "sessions": {
                UMO_GROUP: [make_quote(message_id="legacy-1").to_dict()]
            },
        }
        storage._legacy_json.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(legacy, ensure_ascii=False).encode("utf-8")
        )
        # 重新实例化触发迁移
        from astrbot_plugin_archiver.storage import QuoteStorage

        migrated = QuoteStorage("astrbot_plugin_archiver")
        assert migrated.session_count(UMO_GROUP) == 1  # 迁移 1 条
        quotes = migrated.session_quotes(UMO_GROUP)
        assert any(q.message_id == "legacy-1" for q in quotes)
        # 原 JSON 文件重命名保留,避免重复导入
        assert not storage._legacy_json.exists()
        assert storage._legacy_json.with_suffix(".json.migrated").exists()
        conn = sqlite3.connect(storage._db_file)
        try:
            count = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        finally:
            conn.close()
        assert count == 1


class TestDurability:
    def test_db_file_created(self, storage):
        # 语录库以 SQLite 数据库形式持久化,重启/重载后数据仍在
        storage.add_quote(UMO_GROUP, make_quote())
        assert storage._db_file.is_file()
        from astrbot_plugin_archiver.storage import QuoteStorage

        reloaded = QuoteStorage("astrbot_plugin_archiver")
        assert reloaded.session_count(UMO_GROUP) == 1
        assert reloaded.session_quotes(UMO_GROUP)[0].text == "哈哈哈哈"

    def test_transaction_rollback_keeps_old_data(self, storage):
        # 写入失败(约束冲突回滚) → 磁盘上仍是旧数据
        storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))
        broken = make_quote(message_id="m2")
        broken.id = None  # 触发 NOT NULL 约束失败 → 事务回滚
        try:
            storage.add_quote(UMO_GROUP, broken)
        except Exception:
            pass
        assert storage.session_count(UMO_GROUP) == 1
        assert storage.has_message(UMO_GROUP, "m1") is True
        assert storage.has_message(UMO_GROUP, "m2") is False

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
