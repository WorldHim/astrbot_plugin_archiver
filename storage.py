from __future__ import annotations

import hashlib
import json
import random
import shutil
import time
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
    """典库存储:按会话(unified_msg_origin)分组的 JSON 文件。

    - 数据目录: data/plugin_data/{plugin_name}/quotes.json
    - 原子写: 先写临时文件再 replace,避免写一半崩溃导致文件损坏
    - 备份: 每次保存前轮转保留上一版(quotes.json.bak),主文件缺失/损坏时自动恢复
    - 缓存: 基于 mtime 的内存缓存,文件未变化时直接返回缓存对象
    """

    def __init__(self, plugin_name: str):
        self._plugin_name = plugin_name
        self._base_dir = StarTools.get_data_dir(plugin_name)
        self._quotes_file = self._base_dir / "quotes.json"
        self._images_dir = self._base_dir / "images"
        # (主文件 mtime, 数据)——文件未变化时 load 直接返回缓存
        self._cache: tuple[float, dict[str, list[Quote]]] | None = None

        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._images_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 读写 ----------

    def _read_json(self, path: Path) -> dict[str, list[Quote]]:
        """解析典库文件;编码使用 utf-8-sig 以兼容带 BOM 的手工编辑文件。"""
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        sessions: dict[str, list[Quote]] = {}
        for umo, quotes in (raw.get("sessions") or {}).items():
            if not isinstance(quotes, list):
                continue
            parsed: list[Quote] = []
            for item in quotes:
                if not isinstance(item, dict):
                    continue
                try:
                    parsed.append(Quote.from_dict(item))
                except Exception as e:
                    logger.warning(f"[archiver] skip invalid quote in {umo}: {e}")
            sessions[str(umo)] = parsed
        return sessions

    def load_quotes(self) -> dict[str, list[Quote]]:
        """加载全部典库;带 mtime 内存缓存,主文件缺失/损坏时回退备份文件。"""
        try:
            mtime: float | None = self._quotes_file.stat().st_mtime
        except OSError:
            mtime = None

        cached = self._cache
        if cached is not None and cached[0] == mtime:
            return cached[1]

        data: dict[str, list[Quote]] | None = None
        if mtime is not None:
            try:
                data = self._read_json(self._quotes_file)
            except Exception as e:
                logger.error(f"[archiver] Failed to load quotes.json: {e}")
        if data is None:
            # 主文件缺失或损坏 → 尝试从上一版备份恢复
            bak_path = self._quotes_file.with_suffix(".json.bak")
            if bak_path.exists():
                try:
                    data = self._read_json(bak_path)
                    logger.warning(
                        "[archiver] quotes.json missing/corrupted, restored from backup"
                    )
                except Exception as e:
                    logger.error(f"[archiver] Failed to load backup file: {e}")
        if data is None:
            data = {}

        # 恢复结果同样进入缓存(mtime=None 时缓存主文件缺失状态;
        # 之后文件一旦出现,mtime 变化即自动失效重读)
        self._cache = (mtime, data)
        return data

    def save_quotes(self, data: dict[str, list[Quote]]) -> None:
        payload = {
            "version": 1,
            "updated_at_ts": time.time(),
            "sessions": {
                umo: [q.to_dict() for q in quotes] for umo, quotes in data.items()
            },
        }
        tmp_path = self._quotes_file.with_suffix(".json.tmp")
        bak_path = self._quotes_file.with_suffix(".json.bak")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 轮转备份:保留上一版数据,供主文件损坏时恢复
            if self._quotes_file.exists():
                shutil.copyfile(self._quotes_file, bak_path)
            # 临时文件 + replace 原子写,避免写一半崩溃导致文件损坏
            tmp_path.replace(self._quotes_file)
            # 保存成功,同步刷新内存缓存(省一次回读)
            try:
                self._cache = (self._quotes_file.stat().st_mtime, dict(data))
            except OSError:
                self._cache = None
        except Exception as e:
            logger.error(f"[archiver] Failed to save quotes.json: {e}")
            # 保存失败时缓存状态不确定,置空强制下次重读
            self._cache = None
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ---------- 典库操作 ----------

    def session_quotes(self, umo: str) -> list[Quote]:
        """某会话的全部典。"""
        return self.load_quotes().get(umo, [])

    def session_count(self, umo: str) -> int:
        """某会话的典数。"""
        return len(self.session_quotes(umo))

    def has_message(self, umo: str, message_id: str) -> bool:
        """按原消息 ID 判断某条消息是否已被收录(用于去重);空 ID 不参与去重。"""
        if not message_id:
            return False
        return any(q.message_id == message_id for q in self.session_quotes(umo))

    def add_quote(self, umo: str, quote: Quote, limit: int = 0) -> int:
        """收录一条典;超出 limit(>0)时淘汰最早的典。返回收录后本会话的典数。"""
        data = self.load_quotes()
        # 拷贝一份再改,避免直接修改缓存对象
        quotes = list(data.get(umo, []))
        quotes.append(quote)
        if isinstance(limit, int) and limit > 0:
            quotes = quotes[-limit:]
        data[umo] = quotes
        self.save_quotes(data)
        return len(quotes)

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
