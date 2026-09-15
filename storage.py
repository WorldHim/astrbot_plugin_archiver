from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .image_utils import save_image_bytes

from .models import Quote  # noqa: F401  # re-export(旧导入路径兼容)



class QuoteStorage:
    """语录库存储:SQLite 数据库持久化。

    - 数据库: data/plugin_data/{plugin_name}/quotes.db(WAL 模式,事务性写入)
    - 自动迁移: 首次运行检测旧版 quotes.json → 导入数据库 → 重命名为
      quotes.json.migrated 保留原文件
    - 索引: 按会话(session)与归属(sender_id)建索引,按人抽取高效
    - 每次操作使用独立连接并自动提交,无共享连接状态
    """

    _FIELDS = (
        "id",
        "session",
        "message_id",
        "sender_id",
        "sender_name",
        "text",
        "images",
        "forward_nodes",
        "archived_by",
        "archived_by_name",
        "archived_at_ts",
    )

    def __init__(self, plugin_name: str):
        self._plugin_name = plugin_name
        self._base_dir = StarTools.get_data_dir(plugin_name)
        self._db_file = self._base_dir / "quotes.db"
        self._legacy_json = self._base_dir / "quotes.json"
        self._images_dir = self._base_dir / "images"

        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._images_dir.mkdir(parents=True, exist_ok=True)

        self._init_db()
        self._migrate_from_json()

    # ---------- 连接与建表 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_file)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self):
        """独立的数据库连接上下文:with 块内自动提交,异常自动回滚。"""
        conn = self._connect()
        try:
            with conn:  # 事务上下文
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quotes (
                    id TEXT NOT NULL PRIMARY KEY,
                    session TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    images TEXT NOT NULL DEFAULT '[]',
                    forward_nodes TEXT NOT NULL DEFAULT '[]',
                    archived_by TEXT NOT NULL DEFAULT '',
                    archived_by_name TEXT NOT NULL DEFAULT '',
                    archived_at_ts REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quotes_session ON quotes(session)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quotes_session_sender "
                "ON quotes(session, sender_id)"
            )
            # 已发送语录消息映射表:语录消息 message_id → 语录(回复语录消息删除时精确定位)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_messages (
                    message_id TEXT NOT NULL PRIMARY KEY,
                    session TEXT NOT NULL,
                    quote_id TEXT NOT NULL,
                    sent_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_messages_session "
                "ON sent_messages(session)"
            )

    # ---------- 旧版 JSON 迁移 ----------

    def _migrate_from_json(self) -> None:
        """将旧版 quotes.json 导入 SQLite;完成后重命名为 .migrated 保留。"""
        if not self._legacy_json.exists():
            return
        try:
            data = self._read_json(self._legacy_json)
        except Exception as e:
            logger.error(f"[archiver] Failed to migrate legacy quotes.json: {e}")
            return
        total = 0
        try:
            with self._db() as conn:
                for umo, quotes in data.items():
                    for q in quotes:
                        conn.execute(
                            self._insert_sql("IGNORE"), self._quote_params(q)
                        )
                        total += 1
        except Exception as e:
            logger.error(f"[archiver] Failed to import legacy quotes.json: {e}")
            return
        # 迁移完成,重命名保留原文件,避免下次重复导入
        migrated = self._legacy_json.with_suffix(".json.migrated")
        try:
            self._legacy_json.replace(migrated)
        except OSError as e:
            logger.warning(f"[archiver] Failed to rename legacy quotes.json: {e}")
        if total:
            logger.info(
                f"[archiver] Migrated {total} quotes from quotes.json to SQLite"
            )

    # ---------- 行转换 ----------

    def _quote_params(self, quote: Quote, session: str | None = None) -> tuple:
        d = quote.to_dict()
        if session is not None:
            d["session"] = session
        return (
            d["id"],
            d["session"],
            d["message_id"],
            d["sender_id"],
            d["sender_name"],
            d["text"],
            json.dumps(d["images"], ensure_ascii=False),
            json.dumps(d["forward_nodes"], ensure_ascii=False),
            d["archived_by"],
            d["archived_by_name"],
            d["archived_at_ts"],
        )

    def _insert_sql(self, mode: str) -> str:
        fields = ", ".join(self._FIELDS)
        placeholders = ", ".join("?" for _ in self._FIELDS)
        return f"INSERT OR {mode} INTO quotes ({fields}) VALUES ({placeholders})"

    def _row_to_quote(self, row) -> Quote:
        data = dict(row)
        data["images"] = json.loads(data.get("images") or "[]")
        data["forward_nodes"] = json.loads(data.get("forward_nodes") or "[]")
        return Quote.from_dict(data)

    def _rows_to_quotes(self, rows) -> list[Quote]:
        quotes: list[Quote] = []
        for row in rows:
            try:
                quotes.append(self._row_to_quote(row))
            except Exception as e:
                logger.warning(f"[archiver] skip invalid quote row: {e}")
        return quotes

    # ---------- 旧版 JSON 解析(仅用于迁移) ----------

    def _read_json(self, path: Path) -> dict[str, list[Quote]]:
        """解析旧版语录库 JSON;兼容 {sessions:{...}} 与扁平 {umo:[...]} 两种
        历史格式,编码使用 utf-8-sig 以兼容带 BOM 的手工编辑文件。"""
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        sessions_raw = raw.get("sessions")
        if not isinstance(sessions_raw, dict):
            sessions_raw = raw  # 兼容旧版扁平结构
        sessions: dict[str, list[Quote]] = {}
        for umo, quotes in sessions_raw.items():
            if not isinstance(quotes, list):
                continue
            parsed: list[Quote] = []
            for item in quotes:
                if not isinstance(item, dict):
                    continue
                try:
                    item = dict(item)
                    item.setdefault("session", str(umo))
                    parsed.append(Quote.from_dict(item))
                except Exception as e:
                    logger.warning(f"[archiver] skip invalid quote in {umo}: {e}")
            sessions[str(umo)] = parsed
        return sessions

    # ---------- 语录库操作(SQLite) ----------

    def load_quotes(self) -> dict[str, list[Quote]]:
        """加载全部语录库(按插入顺序,按会话分组)。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes ORDER BY rowid"
            ).fetchall()
        data: dict[str, list[Quote]] = {}
        for q in self._rows_to_quotes(rows):
            data.setdefault(q.session, []).append(q)
        return data

    def save_quotes(self, data: dict[str, list[Quote]]) -> None:
        """以替换方式写入全部语录库(兼容旧接口)。"""
        with self._db() as conn:
            conn.execute("DELETE FROM quotes")
            for umo, quotes in data.items():
                for q in quotes:
                    conn.execute(self._insert_sql("REPLACE"), self._quote_params(q))

    def session_quotes(self, umo: str) -> list[Quote]:
        """某会话的全部语录(按收录顺序)。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE session = ? ORDER BY rowid", (umo,)
            ).fetchall()
        return self._rows_to_quotes(rows)

    def session_count(self, umo: str) -> int:
        """某会话的语录数。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM quotes WHERE session = ?", (umo,)
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def has_message(self, umo: str, message_id: str) -> bool:
        """按原消息 ID 判断某条消息是否已被收录(用于去重);空 ID 不参与去重。"""
        if not message_id:
            return False
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM quotes WHERE session = ? AND message_id = ? LIMIT 1",
                (umo, message_id),
            ).fetchone()
        return row is not None

    def add_quote(self, umo: str, quote: Quote, limit: int = 0) -> int:
        """收录一条语录;超出 limit(>0)时淘汰最早的语录。返回收录后本会话的语录数。"""
        with self._db() as conn:
            # 按 add_quote 的 umo 参数归组(与旧版 JSON 行为一致)
            conn.execute(
                self._insert_sql("REPLACE"), self._quote_params(quote, session=umo)
            )
            if isinstance(limit, int) and limit > 0:
                # 保留本会话最新的 limit 条,淘汰更早的
                conn.execute(
                    """
                    DELETE FROM quotes WHERE session = ? AND id NOT IN (
                        SELECT id FROM quotes WHERE session = ?
                        ORDER BY rowid DESC LIMIT ?
                    )
                    """,
                    (umo, umo, limit),
                )
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM quotes WHERE session = ?", (umo,)
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def random_quote(self, umo: str) -> Quote | None:
        """从某会话的语录库中随机抽取一条;语录库为空时返回 None。"""
        quotes = self.session_quotes(umo)
        return random.choice(quotes) if quotes else None

    def random_quote_any(self) -> Quote | None:
        """从所有会话的语录库中随机抽取一条;全部为空时返回 None。"""
        all_quotes: list[Quote] = []
        for quotes in self.load_quotes().values():
            all_quotes.extend(quotes)
        return random.choice(all_quotes) if all_quotes else None

    @staticmethod
    def _owner_matches(quote: Quote, owner_id: str = "", owner_name: str = "") -> bool:
        """判断语录的归属是否匹配目标。

        QQ 号(owner_id)优先精确匹配;其次昵称(owner_name)完整或包含匹配。
        两者均为空时视为匹配全部。
        """
        if owner_id:
            return quote.sender_id == owner_id
        if owner_name:
            return bool(quote.sender_name) and (
                quote.sender_name == owner_name
                or owner_name in quote.sender_name
            )
        return True

    def random_quote_by_owner(
        self, umo: str, owner_id: str = "", owner_name: str = ""
    ) -> Quote | None:
        """从某会话中随机抽取归属匹配的语录;无匹配时返回 None。"""
        candidates = [
            q
            for q in self.session_quotes(umo)
            if self._owner_matches(q, owner_id, owner_name)
        ]
        return random.choice(candidates) if candidates else None

    def random_quote_by_owner_any(
        self, owner_id: str = "", owner_name: str = ""
    ) -> Quote | None:
        """从所有会话中随机抽取归属匹配的语录;无匹配时返回 None。"""
        candidates = [
            q
            for quotes in self.load_quotes().values()
            for q in quotes
            if self._owner_matches(q, owner_id, owner_name)
        ]
        return random.choice(candidates) if candidates else None

    def find_quote_by_id_prefix(self, prefix: str) -> Quote | None:
        """按语录 ID 前缀(编号)查找语录;无匹配返回 None,多条取第一条。"""
        prefix = str(prefix or "").strip().lower()
        if not prefix:
            return None
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE id LIKE ? ORDER BY rowid LIMIT 2",
                (prefix + "%",),
            ).fetchall()
        quotes = self._rows_to_quotes(rows)
        return quotes[0] if quotes else None

    def get_quote_by_id(self, session: str, quote_id: str) -> Quote | None:
        """按语录 ID 精确查找。"""
        quote_id = str(quote_id or "").strip()
        if not quote_id:
            return None
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE session = ? AND id = ?",
                (session, quote_id),
            ).fetchall()
        quotes = self._rows_to_quotes(rows)
        return quotes[0] if quotes else None

    def find_quote_by_message_id(
        self, session: str, message_id: str
    ) -> Quote | None:
        """按被收录的原消息 ID 查找语录(回复原消息删除用);无匹配返回 None。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            return None
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE session = ? AND message_id = ? "
                "ORDER BY rowid LIMIT 1",
                (session, message_id),
            ).fetchall()
        quotes = self._rows_to_quotes(rows)
        return quotes[0] if quotes else None

    def delete_quote(self, session: str, quote_id: str) -> bool:
        """删除指定语录;删除成功返回 True。"""
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM quotes WHERE session = ? AND id = ?",
                (session, quote_id),
            )
        return cur.rowcount > 0

    # ---------- 已发送语录消息映射 ----------

    _MAX_SENT_RECORDS = 1000

    def record_sent_message(self, session: str, message_id: str, quote_id: str) -> None:
        """记录已发送语录消息(message_id)与语录的映射,用于回复语录消息删除。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            return
        with self._db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sent_messages
                (message_id, session, quote_id, sent_at)
                VALUES (?, ?, ?, strftime('%s','now') + 0.0)
                """,
                (message_id, session, quote_id),
            )
            # 限制映射表大小,淘汰最旧的记录
            conn.execute(
                """
                DELETE FROM sent_messages WHERE message_id NOT IN (
                    SELECT message_id FROM sent_messages
                    ORDER BY sent_at DESC LIMIT ?
                )
                """,
                (self._MAX_SENT_RECORDS,),
            )

    def find_quote_id_by_sent_message(
        self, session: str, message_id: str
    ) -> str | None:
        """由已发送语录消息的 message_id 查找对应语录 ID;无记录返回 None。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            return None
        with self._db() as conn:
            row = conn.execute(
                "SELECT quote_id FROM sent_messages "
                "WHERE session = ? AND message_id = ? LIMIT 1",
                (session, message_id),
            ).fetchone()
        return str(row["quote_id"]) if row is not None else None

    def delete_sent_message(self, session: str, quote_id: str) -> None:
        """语录被删除时清理其已发送消息映射记录。"""
        with self._db() as conn:
            conn.execute(
                "DELETE FROM sent_messages WHERE session = ? AND quote_id = ?",
                (session, quote_id),
            )

    # ---------- 本地图片库 ----------

    def store_image(self, src_path: str | Path, threshold_bytes: int = 0) -> str | None:
        """将图片文件保存进本地图库(超过阈值自动压缩)。

        文件名取内容摘要(sha256),相同内容自动复用同一文件。

        Args:
            src_path: 图片本地文件路径(通常是 convert_to_file_path() 的返回值)。
            threshold_bytes: 压缩阈值(字节);0 表示不压缩。

        Returns:
            相对 data 目录的路径(如 "images/xxx.jpg");读取失败返回 None。
        """
        try:
            data = Path(src_path).read_bytes()
        except OSError as e:
            logger.warning(f"[archiver] Failed to read image file {src_path}: {e}")
            return None
        if not data:
            return None
        digest = hashlib.sha256(data).hexdigest()[:32]
        path = save_image_bytes(data, self._images_dir, digest, threshold_bytes)
        return str(path.relative_to(self._base_dir)).replace("\\", "/")

    def resolve_image_path(self, rel: str) -> Path | None:
        """由相对路径解析本地图片文件;不存在或路径越界时返回 None。"""
        if not rel:
            return None
        try:
            full = (self._base_dir / rel).resolve()
            full.relative_to(self._base_dir)  # 拒绝路径穿越
        except (OSError, ValueError):
            return None
        return full if full.is_file() else None

    # ---------- WebUI 管理查询 ----------

    def list_quotes(
        self,
        session: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Quote], int]:
        """分页查询语录(可按会话与关键词筛选),按收录时间倒序。

        Args:
            session: 限定会话(unified_msg_origin);None 表示全部会话。
            keyword: 关键词,匹配文本/发送人昵称/发送人 ID/语录编号。
            page: 页码(从 1 开始);非法值按 1 处理。
            page_size: 每页条数;非法或非正数时按默认值处理。

        Returns:
            (quotes, total):当前页语录列表与筛选后的总条数。
        """
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = 20
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 200:
            page_size = 20

        where: list[str] = []
        params: list = []
        if session:
            where.append("session = ?")
            params.append(session)
        keyword = str(keyword or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where.append(
                "(text LIKE ? OR sender_name LIKE ? OR sender_id LIKE ? OR id LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        with self._db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM quotes{where_sql}", params
            ).fetchone()
            total = int(row["n"]) if row is not None else 0
            rows = conn.execute(
                f"SELECT * FROM quotes{where_sql} "
                "ORDER BY archived_at_ts DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return self._rows_to_quotes(rows), total

    def list_sessions(self) -> list[dict]:
        """返回出现过的会话统计(session/条数),按条数倒序。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT session, COUNT(*) AS n FROM quotes "
                "GROUP BY session ORDER BY n DESC"
            ).fetchall()
        return [
            {"session": str(row["session"]), "count": int(row["n"])}
            for row in rows
        ]

    def import_quotes(self, quotes: list[Quote]) -> tuple[int, int]:
        """批量导入语录(按语录 ID 去重),返回 (added, skipped)。

        ID 已存在的语录跳过不覆盖;forward_nodes 内的子消息按原样保留。
        """
        added = 0
        skipped = 0
        with self._db() as conn:
            for quote in quotes:
                row = conn.execute(
                    "SELECT 1 FROM quotes WHERE id = ? LIMIT 1", (quote.id,)
                ).fetchone()
                if row is not None:
                    skipped += 1
                    continue
                conn.execute(
                    self._insert_sql("IGNORE"), self._quote_params(quote)
                )
                added += 1
        return added, skipped
