"""image_utils / 本地图库 单元测试:格式探测、超阈值压缩、动图保留、内容寻址去重、路径解析。"""
import io
import random

import pytest

from astrbot_plugin_archiver.image_utils import (
    compress_bytes,
    probe_format,
    save_image_bytes,
)

from conftest import write_png, write_png_bytes


def _noise_jpeg_bytes(width=1600, height=1200) -> bytes:
    """生成接近不可压缩的随机噪点 JPEG(体积大,适合测压缩)。"""
    from PIL import Image as PILImage

    im = PILImage.frombytes(
        "RGB", (width, height), random.randbytes(width * height * 3)
    )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _noise_rgba_png_bytes(size=800) -> bytes:
    """生成带透明通道的随机噪点 RGBA PNG(体积大,测带透明压缩)。"""
    from PIL import Image as PILImage

    im = PILImage.frombytes(
        "RGBA", (size, size), random.randbytes(size * size * 4)
    )
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _animated_gif_bytes(size=400, frames=2) -> bytes:
    """生成多帧随机噪点动图 GIF(测动图跳过压缩)。"""
    from PIL import Image as PILImage

    imgs = [
        PILImage.frombytes("RGB", (size, size), random.randbytes(size * size * 3))
        for _ in range(frames)
    ]
    buf = io.BytesIO()
    imgs[0].save(
        buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0
    )
    return buf.getvalue()


class TestProbeFormat:
    def test_png(self):
        assert probe_format(write_png_bytes()) == ("PNG", "png")

    def test_jpeg(self):
        assert probe_format(_noise_jpeg_bytes(100, 100))[0] == "JPEG"

    def test_invalid_returns_none(self):
        assert probe_format(b"not-an-image") is None


class TestSaveImageBytes:
    def test_small_image_saved_as_is(self, tmp_path):
        data = write_png_bytes()
        p = save_image_bytes(data, tmp_path, "d1", threshold_bytes=1024 * 1024)
        assert p.read_bytes() == data
        assert p.suffix == ".png"

    def test_oversize_image_compressed_under_threshold(self, tmp_path):
        data = _noise_jpeg_bytes()
        original_size = len(data)
        assert original_size > 2 * 1024 * 1024  # 前提:原图远超阈值
        p = save_image_bytes(data, tmp_path, "d2", threshold_bytes=2 * 1024 * 1024)
        assert p.stat().st_size <= 2 * 1024 * 1024
        assert p.stat().st_size < original_size
        # 结果仍是可解码的有效图片
        from PIL import Image as PILImage

        with PILImage.open(p) as im:
            im.verify()

    def test_threshold_zero_keeps_original(self, tmp_path):
        data = _noise_jpeg_bytes(300, 300)
        p = save_image_bytes(data, tmp_path, "d3", threshold_bytes=0)
        assert p.read_bytes() == data

    def test_content_addressed_dedup(self, tmp_path):
        data = write_png_bytes()
        p1 = save_image_bytes(data, tmp_path, "dup", threshold_bytes=0)
        p2 = save_image_bytes(data, tmp_path, "dup", threshold_bytes=0)
        assert p1 == p2
        assert len(list(tmp_path.iterdir())) == 1

    def test_undecodable_bytes_saved_as_is(self, tmp_path):
        p = save_image_bytes(b"not-an-image", tmp_path, "bad", threshold_bytes=10)
        assert p.read_bytes() == b"not-an-image"

    def test_animated_gif_kept_as_is(self, tmp_path):
        data = _animated_gif_bytes()
        assert len(data) > 16 * 1024  # 前提:动图超过阈值
        p = save_image_bytes(data, tmp_path, "gif1", threshold_bytes=16 * 1024)
        # 动图不做有损压缩(避免丢帧) → 原样保存
        assert p.read_bytes() == data

class TestCompressBytes:
    def test_alpha_image_compressed_to_webp(self):
        data = _noise_rgba_png_bytes()
        assert len(data) > 1024 * 1024  # 前提:原图超过阈值
        out, ext = compress_bytes(data, 1024 * 1024)
        assert ext == "webp"
        assert len(out) <= 1024 * 1024

    def test_compressed_is_valid_image(self):
        data = _noise_jpeg_bytes(400, 300)
        out, ext = compress_bytes(data, 16 * 1024)
        assert ext in ("jpg", "webp", "png")
        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(out))
        im.verify()

    def test_none_for_undecodable(self):
        assert compress_bytes(b"garbage", 1024) is None

    def test_none_for_zero_threshold(self):
        assert compress_bytes(write_png_bytes(), 0) is None

    def test_none_without_pillow(self, monkeypatch):
        import astrbot_plugin_archiver.image_utils as image_utils

        monkeypatch.setattr(image_utils, "PILImage", None)
        assert compress_bytes(write_png_bytes(), 1024) is None


class TestQuoteStorageImages:
    """QuoteStorage 的本地图库方法。"""

    def test_store_image_returns_relative_path(self, storage, tmp_path):
        img = write_png(tmp_path / "src.png")
        rel = storage.store_image(str(img))
        assert rel is not None
        assert rel.startswith("images/")
        assert (storage._base_dir / rel).is_file()

    def test_store_image_dedup(self, storage, tmp_path):
        img = write_png(tmp_path / "src.png")
        rel1 = storage.store_image(str(img))
        rel2 = storage.store_image(str(img))
        assert rel1 == rel2
        assert len(list(storage._images_dir.iterdir())) == 1

    def test_store_image_compresses_oversize(self, storage, tmp_path):
        from PIL import Image as PILImage

        im = PILImage.frombytes(
            "RGB", (1600, 1200), random.randbytes(1600 * 1200 * 3)
        )
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
        src = tmp_path / "big.jpg"
        src.write_bytes(buf.getvalue())
        rel = storage.store_image(str(src), threshold_bytes=2 * 1024 * 1024)
        stored = storage._base_dir / rel
        assert stored.stat().st_size <= 2 * 1024 * 1024

    def test_store_image_missing_file_returns_none(self, storage, tmp_path):
        assert storage.store_image(str(tmp_path / "ghost.png")) is None

    def test_resolve_image_path(self, storage, tmp_path):
        img = write_png(tmp_path / "src.png")
        rel = storage.store_image(str(img))
        assert storage.resolve_image_path(rel) == (storage._base_dir / rel).resolve()
        assert storage.resolve_image_path("images/ghost.png") is None
        assert storage.resolve_image_path("") is None
        # 路径穿越被拒绝
        assert storage.resolve_image_path("../../outside.png") is None
        assert storage.resolve_image_path("images/../../outside.png") is None
