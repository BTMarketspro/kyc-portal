"""Turn any customer upload into a file Yativo accepts.

Rules we have to satisfy (guide §2.1-2.2 plus the stricter ID-image rule in the API):
  * type: pdf / jpg / jpeg / png (ID images accept only these; heic/tif are not universal)
  * size: at least FILE_MIN_KB (default 120 KB) and at most FILE_MAX_KB (default 3 MB)

Strategy
  * Images of any format (jpg, png, heic/heif, webp, gif, bmp, tiff, ...) are decoded, rotated
    upright from EXIF, stripped of metadata (incl. GPS) and re-encoded as JPEG. Quality and
    resolution are then tuned until the file sits inside the window.
  * PDFs inside the window are kept byte-for-byte (text stays selectable). Oversized PDFs are
    first compressed losslessly, then re-rendered page by page at a lower DPI if needed.
    Undersized PDFs are re-rendered at a higher DPI.
  * As a last resort for nearly blank images (which compress to almost nothing), invisible
    noise is blended in so the JPEG reaches the minimum size.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageOps

from .config import settings

try:  # HEIC/HEIF (iPhone photos)
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIF_SUPPORTED = False

Image.MAX_IMAGE_PIXELS = 250_000_000  # still guards against decompression bombs

MAX_SIDE = 4096  # no KYC reviewer needs more than this
MIN_SIDE_UPSCALE_CAP = 3000


class FileRejected(ValueError):
    """The upload can't be turned into an acceptable file. Message is customer-safe."""


@dataclass
class NormalizedFile:
    data: bytes
    mime: str
    ext: str
    width: int | None = None
    height: int | None = None
    pages: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.data)


def _bounds() -> tuple[int, int, int, int]:
    lo, hi = settings.file_min_bytes, settings.file_max_bytes
    # Aim comfortably inside the window so later transport encoding never pushes us out.
    return lo, hi, int(lo * 1.08), int(hi * 0.94)


def normalize_upload(data: bytes, filename: str = "", content_type: str = "") -> NormalizedFile:
    if not data:
        raise FileRejected("The file is empty.")
    if len(data) > settings.max_raw_upload_bytes:
        raise FileRejected(
            f"This file is larger than {settings.max_raw_upload_bytes // (1024 * 1024)} MB. Please upload a smaller copy."
        )
    if data[:5] == b"%PDF-" or data[:1024].find(b"%PDF-") != -1:
        return _normalize_pdf(data)
    return _normalize_image(data, filename)


# ------------------------------------------------------------------------------- images

def _open_image(data: bytes, filename: str) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError as exc:
        raise FileRejected("This image is too large to process.") from exc
    except Exception as exc:  # noqa: BLE001
        lower = filename.lower()
        if lower.endswith((".heic", ".heif")) and not HEIF_SUPPORTED:
            raise FileRejected("HEIC photos are not supported here. Please upload a JPG or PNG.") from exc
        raise FileRejected("This file type isn't supported. Please upload a photo (JPG, PNG, HEIC) or a PDF.") from exc
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)  # animated GIF/WebP or multi-page TIFF: use the first frame
    return img


def _to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    extra = {"subsampling": 0} if quality >= 92 else {}
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **extra)
    return buf.getvalue()


def _resize(img: Image.Image, factor: float) -> Image.Image:
    w, h = img.size
    nw, nh = max(1, round(w * factor)), max(1, round(h * factor))
    return img.resize((nw, nh), Image.LANCZOS)


def _cap(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    return _resize(img, max_side / max(w, h))


def _add_noise(img: Image.Image, strength: float) -> Image.Image:
    noise = Image.effect_noise(img.size, 64).convert("RGB")
    return Image.blend(img, noise, strength)


def fit_image(img: Image.Image, lo_target: int, hi_target: int, hi_hard: int) -> tuple[bytes, Image.Image, list[str]]:
    """Return JPEG bytes within [lo_target, hi_hard] (aiming below hi_target)."""
    notes: list[str] = []
    img = _cap(img, MAX_SIDE)
    data = _encode_jpeg(img, 88)

    # ---- too big: lower quality (binary search), then resolution
    if len(data) > hi_target:
        for _ in range(10):
            lo_q, hi_q, best = 40, 88, None
            while lo_q <= hi_q:
                q = (lo_q + hi_q) // 2
                d = _encode_jpeg(img, q)
                if len(d) <= hi_target:
                    best, lo_q = d, q + 1
                else:
                    hi_q = q - 1
            if best is not None and len(best) >= lo_target:
                notes.append("compressed")
                return best, img, notes
            if best is not None:  # fits, but quality search undershot the minimum; take q=88 path below
                data = best
                break
            factor = max(0.4, min(0.92, math.sqrt(hi_target / len(_encode_jpeg(img, 40))) * 0.95))
            img = _resize(img, factor)
            notes.append("downscaled")
        else:
            data = _encode_jpeg(img, 40)

    # ---- too small: raise quality, then resolution, then add imperceptible noise
    if len(data) < lo_target:
        for q in (94, 97, 100):
            d = _encode_jpeg(img, q)
            if len(d) > hi_target:
                break
            data = d
            if len(data) >= lo_target:
                break
        tries = 0
        while len(data) < lo_target and tries < 6 and max(img.size) < MIN_SIDE_UPSCALE_CAP:
            factor = min(2.5, max(1.15, math.sqrt(lo_target / max(len(data), 1)) * 1.1))
            factor = min(factor, MIN_SIDE_UPSCALE_CAP / max(img.size))
            if factor <= 1.01:
                break
            img = _resize(img, factor)
            data = _encode_jpeg(img, 95)
            tries += 1
            notes.append("upscaled")
        strength = 0.015
        while len(data) < lo_target and strength <= 0.12:
            data = _encode_jpeg(_add_noise(img, strength), 95)
            strength *= 1.6
        if len(data) < lo_target:
            raise FileRejected("This image is too small or blank to be used. Please upload a clearer photo.")
        notes.append("enlarged to minimum size")

    if len(data) > hi_hard:  # extremely unlikely after the loops above
        raise FileRejected("We couldn't reduce this image enough. Please upload a smaller photo.")
    return data, img, notes


def _normalize_image(data: bytes, filename: str) -> NormalizedFile:
    lo, hi, lo_t, hi_t = _bounds()
    img = _to_rgb(_open_image(data, filename))
    w, h = img.size
    if min(w, h) < 200:
        raise FileRejected("This image's resolution is too low to read. Please upload a clearer photo.")
    out, final, notes = fit_image(img, lo_t, hi_t, hi)
    return NormalizedFile(out, "image/jpeg", "jpg", final.size[0], final.size[1], None, ["converted to JPEG", *notes])


# ------------------------------------------------------------------------------- PDFs

def _pdf_page_count(data: bytes) -> int:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise FileRejected("This PDF is password protected. Please upload an unlocked copy.")
            except FileRejected:
                raise
            except Exception as exc:  # noqa: BLE001
                raise FileRejected("This PDF is password protected. Please upload an unlocked copy.") from exc
        return len(reader.pages)
    except FileRejected:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FileRejected("This PDF appears to be damaged. Please export it again and re-upload.") from exc


def _pdf_lossless_compress(data: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001
            pass
        try:
            for im in page.images:
                pil = im.image
                if pil is None:
                    continue
                if pil.mode not in ("RGB", "L"):
                    pil = pil.convert("RGB")
                if max(pil.size) > 2200:
                    pil = _cap(pil, 2200)
                im.replace(pil, quality=70)
        except Exception:  # noqa: BLE001 - exotic image encodings: leave them as they are
            pass
    try:
        writer.compress_identical_objects()
    except Exception:  # noqa: BLE001
        pass
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _render_pdf(data: bytes, dpi: int) -> list[Image.Image]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    pages = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=dpi / 72)
            pages.append(_cap(bitmap.to_pil().convert("RGB"), MAX_SIDE))
    finally:
        pdf.close()
    return pages


def _images_to_pdf(pages: list[Image.Image], quality: int, dpi: int) -> bytes:
    buf = io.BytesIO()
    first, rest = pages[0], pages[1:]
    first.save(buf, "PDF", save_all=True, append_images=rest, resolution=dpi, quality=quality)
    return buf.getvalue()


def _normalize_pdf(data: bytes) -> NormalizedFile:
    lo, hi, lo_t, hi_t = _bounds()
    n_pages = _pdf_page_count(data)
    if n_pages == 0:
        raise FileRejected("This PDF has no pages.")
    if n_pages > 60:
        raise FileRejected("This PDF has too many pages. Please upload only the relevant pages (max 60).")

    if lo <= len(data) <= hi:
        return NormalizedFile(data, "application/pdf", "pdf", pages=n_pages, notes=["kept original"])

    if len(data) > hi:
        try:
            compressed = _pdf_lossless_compress(data)
            if lo <= len(compressed) <= hi_t:
                return NormalizedFile(compressed, "application/pdf", "pdf", pages=n_pages, notes=["compressed"])
        except Exception:  # noqa: BLE001
            pass
        for dpi in (150, 120, 100, 85, 72):
            pages = _render_pdf(data, dpi)
            for q in (80, 65, 50, 40):
                out = _images_to_pdf(pages, q, dpi)
                if len(out) <= hi_t:
                    if len(out) >= lo:
                        return NormalizedFile(out, "application/pdf", "pdf", pages=n_pages,
                                              notes=[f"re-rendered at {dpi} dpi"])
                    break
        # Very long documents: give each page an equal share of the budget.
        pages = _render_pdf(data, 100)
        per_page = hi_t // len(pages)
        if per_page < 25_000:
            raise FileRejected("This PDF is too large even after compression. Please upload fewer pages.")
        fitted = []
        for p in pages:
            d, _, _ = fit_image(p, 1, per_page, per_page)
            fitted.append(Image.open(io.BytesIO(d)).convert("RGB"))
        out = _images_to_pdf(fitted, 60, 100)
        if lo <= len(out) <= hi:
            return NormalizedFile(out, "application/pdf", "pdf", pages=n_pages, notes=["re-rendered per page"])
        raise FileRejected("This PDF is too large even after compression. Please upload fewer pages.")

    # Too small (typically a short, text-only PDF): re-render at a higher resolution.
    for dpi in (150, 200, 250, 300):
        pages = _render_pdf(data, dpi)
        out = _images_to_pdf(pages, 92, dpi)
        if lo_t <= len(out) <= hi_t:
            return NormalizedFile(out, "application/pdf", "pdf", pages=n_pages, notes=[f"re-rendered at {dpi} dpi"])
        if len(out) > hi_t:
            break
    # Still too small: fit the first page image into the window and rebuild.
    pages = _render_pdf(data, 200)
    share_lo = max(1, lo_t // len(pages))
    fitted = []
    for p in pages:
        d, _, _ = fit_image(p, share_lo, hi_t // len(pages), hi_t // len(pages))
        fitted.append(Image.open(io.BytesIO(d)).convert("RGB"))
    out = _images_to_pdf(fitted, 95, 200)
    if lo <= len(out) <= hi:
        return NormalizedFile(out, "application/pdf", "pdf", pages=n_pages, notes=["enlarged to minimum size"])
    raise FileRejected("We couldn't process this PDF. Please upload a photo or scan of the document instead.")


def make_thumbnail(data: bytes, mime: str, max_side: int = 640) -> bytes | None:
    """Small JPEG preview for the admin console (decrypted on the fly, never stored)."""
    try:
        if mime == "application/pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(data)
            try:
                img = pdf[0].render(scale=1.2).to_pil().convert("RGB")
            finally:
                pdf.close()
        else:
            img = _to_rgb(Image.open(io.BytesIO(data)))
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None
