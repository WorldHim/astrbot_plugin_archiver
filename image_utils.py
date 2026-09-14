"""图片落盘与压缩工具(基于 Pillow;未安装 Pillow 时优雅降级为直接保存)。

压缩策略:超过阈值时先按质量梯度重新编码,仍过大再等比缩小,迭代直到达标;
动图(逐帧信息)不做有损压缩,避免丢帧;无法解码的文件直接保存原图。
"""
from __future__ import annotations

import io

from astrbot.api import logger

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - Pillow 是 AstrBot 核心依赖,正常环境不会缺失
    PILImage = None

# PIL 格式名 → 文件扩展名
_FORMAT_EXT = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
}

# 压缩时的等比缩小比例(由原图到最小)
_SCALE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.4)
# 有损编码的质量梯度(由高到低)
_QUALITY_STEPS = (90, 80, 70, 60, 50, 40)


def probe_format(data: bytes) -> tuple[str, str] | None:
    """探测图片格式,返回 (PIL 格式名, 扩展名);无法识别时返回 None。"""
    if PILImage is None:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
    except Exception:
        return None
    return (fmt, _FORMAT_EXT.get(fmt, "")) if fmt else None


def _has_alpha(im) -> bool:
    """判断图片是否携带透明通道(决定压缩目标格式)。"""
    if im.mode in ("RGBA", "LA", "PA"):
        return True
    if im.mode == "P":
        transparency = im.info.get("transparency")
        return transparency is not None
    return False


def _encode(im, fmt: str, quality: int) -> bytes:
    """按指定格式/质量编码图片,返回字节。"""
    buf = io.BytesIO()
    if fmt == "JPEG":
        im.save(buf, format="JPEG", quality=quality, optimize=True)
    elif fmt == "WEBP":
        im.save(buf, format="WEBP", quality=quality, method=4)
    elif fmt == "PNG":
        im.save(buf, format="PNG", optimize=True)
    else:
        im.save(buf, format=fmt)
    return buf.getvalue()


def compress_bytes(data: bytes, threshold_bytes: int) -> tuple[bytes, str] | None:
    """尝试将图片压缩到阈值以下。

    策略:每轮先按当前尺寸以质量梯度重新编码(带透明→WebP,兜底 PNG;
    无透明→JPEG),首个不超阈值的(质量最高)直接返回;整轮仍超阈值则
    等比缩小后继续。返回 (bytes, 扩展名);始终未达标时返回全程最小的
    结果;未安装 Pillow、动图或无法解码时返回 None(调用方保存原图)。
    """
    if PILImage is None or threshold_bytes <= 0:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as src:
            if getattr(src, "is_animated", False):
                return None  # 动图不做有损压缩,避免丢帧
            src.load()
            best: tuple[bytes, str] | None = None
            for scale in _SCALE_STEPS:
                if scale >= 1.0:
                    im = src
                else:
                    w = max(1, round(src.width * scale))
                    h = max(1, round(src.height * scale))
                    im = src.resize((w, h), PILImage.LANCZOS)
                alpha = _has_alpha(im)
                for quality in _QUALITY_STEPS:
                    try:
                        if alpha:
                            try:
                                out, ext = _encode(im, "WEBP", quality), "webp"
                            except Exception:
                                out, ext = _encode(im, "PNG", 0), "png"
                        else:
                            conv = im if im.mode in ("RGB", "L") else im.convert("RGB")
                            out, ext = _encode(conv, "JPEG", quality), "jpg"
                    except Exception:
                        continue
                    if best is None or len(out) < len(best[0]):
                        best = (out, ext)
                    if len(out) <= threshold_bytes:
                        return best
            return best
    except Exception as e:
        logger.warning(f"[archiver] Failed to decode image for compression: {e}")
        return None


def save_image_bytes(
    data: bytes,
    images_dir,
    digest: str,
    threshold_bytes: int = 0,
):
    """将图片字节落盘;超过阈值时自动压缩。

    Args:
        data: 图片原始字节。
        images_dir: 图片库目录(pathlib.Path)。
        digest: 内容摘要(作为文件名,相同内容自动复用同一文件)。
        threshold_bytes: 压缩阈值(字节);0 表示不压缩。

    Returns:
        pathlib.Path: 最终落盘的文件路径(压缩成功则尺寸≤阈值或为全程最小结果)。
    """
    from pathlib import Path

    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    probe = probe_format(data)
    original_ext = probe[1] if probe else "bin"

    original_path = images_dir / f"{digest}.{original_ext}"
    if threshold_bytes <= 0 or len(data) <= threshold_bytes:
        original_path.write_bytes(data)
        return original_path

    compressed = compress_bytes(data, threshold_bytes)
    if compressed is not None and len(compressed[0]) < len(data):
        out, ext = compressed
        path = images_dir / f"{digest}.{ext}"
        if ext != original_ext and path != original_path:
            original_path.unlink(missing_ok=True)
        path.write_bytes(out)
        logger.info(
            f"[archiver] image {digest} compressed: "
            f"{len(data)} -> {len(out)} bytes ({ext})"
        )
        return path

    # 无法压缩(未安装 Pillow/无法解码/动图)或压缩无收益 → 保存原图
    original_path.write_bytes(data)
    return original_path
