from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .image_utils import save_image_bytes


@dataclass
class Quote:
    """一条被收录的典。

    Attributes:
        id: 典的唯一识别 ID(uuid)。
        session: 收录时所在会话(unified_msg_origin)。
        message_id: 被收录的原消息 ID(用于去重,平台未提供时为空串)。
        sender_id: 被收录消息发送者的 ID。
        sender_name: 被收录消息发送者的昵称。
        text: 被收录消息的纯文本内容(不支持展示的类型会转为占位符);
            被收录内容为聊天记录(合并转发)时为 "[聊天记录]" 占位,
            实际内容保存在 forward_nodes。
        images: 被收录消息中的图片,每项含 file/url 键。
        forward_nodes: 被收录内容为聊天记录(合并转发)时的结构化子消息
            列表,每项为 {"sender_id", "sender_name", "text", "images"};
            非聊天记录时为空列表。
        archived_by: 发起收录的用户 ID。
        archived_by_name: 发起收录的用户昵称。
        archived_at_ts: 收录时间戳(秒)。
    """

    id: str
    session: str
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    images: list[dict[str, str]] = field(default_factory=list)
    forward_nodes: list[dict[str, Any]] = field(default_factory=list)
    archived_by: str = ""
    archived_by_name: str = ""
    archived_at_ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Quote":
        """从 dict 还原;字段缺失/类型异常时回退默认值。"""
        images_raw = data.get("images") or []
        images: list[dict[str, str]] = []
        if isinstance(images_raw, list):
            for item in images_raw:
                if isinstance(item, dict):
                    # 保留 path(本地存档)/url/file(URL 回退)键,缺失的键不写入
                    images.append(
                        {
                            key: str(item.get(key) or "")
                            for key in ("path", "url", "file")
                            if item.get(key)
                        }
                    )

        forward_nodes_raw = data.get("forward_nodes") or []
        forward_nodes: list[dict[str, Any]] = []
        if isinstance(forward_nodes_raw, list):
            for item in forward_nodes_raw:
                if not isinstance(item, dict):
                    continue
                sub_images_raw = item.get("images") or []
                sub_images: list[dict[str, str]] = []
                if isinstance(sub_images_raw, list):
                    for img in sub_images_raw:
                        if isinstance(img, dict):
                            sub_images.append(
                                {
                                    key: str(img.get(key) or "")
                                    for key in ("path", "url", "file")
                                    if img.get(key)
                                }
                            )
                forward_nodes.append(
                    {
                        "sender_id": str(item.get("sender_id") or ""),
                        "sender_name": str(item.get("sender_name") or ""),
                        "text": str(item.get("text") or ""),
                        "images": sub_images,
                    }
                )

        return cls(
            id=str(data.get("id", "") or ""),
            session=str(data.get("session", "") or ""),
            message_id=str(data.get("message_id", "") or ""),
            sender_id=str(data.get("sender_id", "") or ""),
            sender_name=str(data.get("sender_name", "") or ""),
            text=str(data.get("text", "") or ""),
            images=images,
            forward_nodes=forward_nodes,
            archived_by=str(data.get("archived_by", "") or ""),
            archived_by_name=str(data.get("archived_by_name", "") or ""),
            archived_at_ts=float(data.get("archived_at_ts", 0.0) or 0.0),
        )


class QuoteStorage:
    """典库存储:SQLite 数据库持久化。

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
            # 已发送典消息映射表:典消息 message_id → 典(回复典消息删除时精确定位)
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
        """解析旧版典库 JSON;兼容 {sessions:{...}} 与扁平 {umo:[...]} 两种
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

    # ---------- 典库操作(SQLite) ----------

    def load_quotes(self) -> dict[str, list[Quote]]:
        """加载全部典库(按插入顺序,按会话分组)。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes ORDER BY rowid"
            ).fetchall()
        data: dict[str, list[Quote]] = {}
        for q in self._rows_to_quotes(rows):
            data.setdefault(q.session, []).append(q)
        return data

    def save_quotes(self, data: dict[str, list[Quote]]) -> None:
        """以替换方式写入全部典库(兼容旧接口)。"""
        with self._db() as conn:
            conn.execute("DELETE FROM quotes")
            for umo, quotes in data.items():
                for q in quotes:
                    conn.execute(self._insert_sql("REPLACE"), self._quote_params(q))

    def session_quotes(self, umo: str) -> list[Quote]:
        """某会话的全部典(按收录顺序)。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM quotes WHERE session = ? ORDER BY rowid", (umo,)
            ).fetchall()
        return self._rows_to_quotes(rows)

    def session_count(self, umo: str) -> int:
        """某会话的典数。"""
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
        """收录一条典;超出 limit(>0)时淘汰最早的典。返回收录后本会话的典数。"""
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
        """从某会话的典库中随机抽取一条;典库为空时返回 None。"""
        quotes = self.session_quotes(umo)
        return random.choice(quotes) if quotes else None

    def random_quote_any(self) -> Quote | None:
        """从所有会话的典库中随机抽取一条;全部为空时返回 None。"""
        all_quotes: list[Quote] = []
        for quotes in self.load_quotes().values():
            all_quotes.extend(quotes)
        return random.choice(all_quotes) if all_quotes else None

    @staticmethod
    def _owner_matches(quote: Quote, owner_id: str = "", owner_name: str = "") -> bool:
        """判断典的归属是否匹配目标。

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
        """从某会话中随机抽取归属匹配的典;无匹配时返回 None。"""
        candidates = [
            q
            for q in self.session_quotes(umo)
            if self._owner_matches(q, owner_id, owner_name)
        ]
        return random.choice(candidates) if candidates else None

    def random_quote_by_owner_any(
        self, owner_id: str = "", owner_name: str = ""
    ) -> Quote | None:
        """从所有会话中随机抽取归属匹配的典;无匹配时返回 None。"""
        candidates = [
            q
            for quotes in self.load_quotes().values()
            for q in quotes
            if self._owner_matches(q, owner_id, owner_name)
        ]
        return random.choice(candidates) if candidates else None

    def find_quote_by_id_prefix(self, prefix: str) -> Quote | None:
        """按典 ID 前缀(编号)查找典;无匹配返回 None,多条取第一条。"""
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
        """按典 ID 精确查找。"""
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
        """按被收录的原消息 ID 查找典(回复原消息删除用);无匹配返回 None。"""
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
        """删除指定典;删除成功返回 True。"""
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM quotes WHERE session = ? AND id = ?",
                (session, quote_id),
            )
        return cur.rowcount > 0

    # ---------- 已发送典消息映射 ----------

    _MAX_SENT_RECORDS = 1000

    def record_sent_message(self, session: str, message_id: str, quote_id: str) -> None:
        """记录已发送典消息(message_id)与典的映射,用于回复典消息删除。"""
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
        """由已发送典消息的 message_id 查找对应典 ID;无记录返回 None。"""
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
        """典被删除时清理其已发送消息映射记录。"""
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
