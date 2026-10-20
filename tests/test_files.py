import io
import os

import pytest
from PIL import Image

os.environ.setdefault("SECRET_KEY", "x" * 40)

from app.config import settings  # noqa: E402
from app.files import FileRejected, normalize_upload  # noqa: E402

LO, HI = settings.file_min_bytes, settings.file_max_bytes


def _noise_image(w, h, fmt="JPEG", **kw):
    img = Image.effect_noise((w, h), 80).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _flat_image(w, h, fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(buf, fmt)
    return buf.getvalue()


def _pdf(pages, noisy=False, size=(1240, 1754)):
    imgs = [(Image.effect_noise(size, 90).convert("RGB") if noisy else Image.new("RGB", size, "white")) for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=150, quality=95)
    return buf.getvalue()


def test_huge_photo_is_shrunk_into_range():
    raw = _noise_image(6000, 4000, quality=98)
    assert len(raw) > HI
    out = normalize_upload(raw, "big.jpg")
    assert out.mime == "image/jpeg" and LO <= out.size <= HI


def test_tiny_blank_image_is_grown_to_minimum():
    raw = _flat_image(800, 600)
    assert len(raw) < LO
    out = normalize_upload(raw, "flat.png")
    assert out.mime == "image/jpeg" and LO <= out.size <= HI


def test_webp_and_png_become_jpeg():
    raw = _noise_image(1200, 900, "WEBP", quality=90)
    out = normalize_upload(raw, "a.webp")
    assert out.ext == "jpg" and LO <= out.size <= HI


def test_heic_is_accepted():
    pytest.importorskip("pillow_heif")
    img = Image.effect_noise((1600, 1200), 60).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "HEIF", quality=80)
    out = normalize_upload(buf.getvalue(), "photo.heic")
    assert out.ext == "jpg" and LO <= out.size <= HI


def test_pdf_in_range_kept_verbatim():
    raw = _pdf(1, noisy=True, size=(700, 700))
    if not (LO <= len(raw) <= HI):
        pytest.skip("fixture size outside window")
    out = normalize_upload(raw, "doc.pdf")
    assert out.data == raw


def test_large_pdf_is_compressed():
    raw = _pdf(4, noisy=True)
    assert len(raw) > HI
    out = normalize_upload(raw, "scan.pdf")
    assert out.mime == "application/pdf" and LO <= out.size <= HI and out.pages == 4


def test_small_pdf_is_enlarged():
    raw = _pdf(1)
    assert len(raw) < LO
    out = normalize_upload(raw, "letter.pdf")
    assert out.mime == "application/pdf" and LO <= out.size <= HI


def test_garbage_rejected():
    with pytest.raises(FileRejected):
        normalize_upload(b"not an image at all" * 100, "x.exe")


def test_low_resolution_rejected():
    with pytest.raises(FileRejected):
        normalize_upload(_flat_image(80, 80), "icon.png")
