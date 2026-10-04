"""Thumbnails, contact sheets and image comparison. Pure Pillow; no numpy needed."""
from __future__ import annotations

import math
import os
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

_FONTS = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc")


def _font(size: int):
    for f in _FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _pixels(im) -> list:
    return list(im.get_flattened_data()) if hasattr(im, "get_flattened_data") else list(im.getdata())


def sips_jpeg(src: str, dst: str, max_side: int) -> None:
    """Convert anything macOS can read (HEIC, CR3, PNG...) to a downscaled JPEG."""
    subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(max_side), src, "--out", dst],
                   capture_output=True, timeout=180, check=True)


def export_thumb(src: str, dst: str, max_side: int = 900) -> bool:
    """Write an upright JPEG thumbnail. sips doesn't apply EXIF rotation, so do it here."""
    if os.path.exists(dst) and os.path.getsize(dst):
        return True
    try:
        sips_jpeg(src, dst, max_side)
        im = Image.open(dst)
        if (im.getexif().get(274, 1) or 1) != 1:
            ImageOps.exif_transpose(im).convert("RGB").save(dst, quality=85)
        return True
    except Exception:
        return False


def open_rgb(path: str, max_side: int = 256) -> Image.Image:
    im = Image.open(path)
    im.draft("RGB", (max_side, max_side))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def render(photo, max_side: int = 256) -> Image.Image | None:
    """A small upright RGB rendition of a library photo.

    Prefers Photos' own derivatives (JPEG, and present even for iCloud-only items), then the
    edited or original file, converting via sips when Pillow can't read the format.
    """
    derivs = [d for d in (photo.path_derivatives or []) if os.path.exists(d)]
    for path in sorted(derivs, key=os.path.getsize, reverse=True) + [photo.path_edited, photo.path]:
        if not path or not os.path.exists(path):
            continue
        try:
            return open_rgb(path, max_side)
        except Exception:
            try:
                with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
                    sips_jpeg(path, tmp.name, max_side)
                    return open_rgb(tmp.name, max_side)
            except Exception:
                continue
    return None


def contact_sheet(items: list[tuple[str, str]], out: str, cols: int = 6, rows: int = 6,
                  cell: int = 300, colour=(255, 200, 90)) -> list[str]:
    """Lay labelled thumbnails out on numbered sheets. items: [(label, image_path)]."""
    pad, lab = 8, 22
    per = cols * rows
    font = _font(max(14, cell // 18))
    width, height = cols * (cell + pad) + pad, rows * (cell + lab + pad) + pad
    written = []
    for s in range(math.ceil(len(items) / per)):
        sheet = Image.new("RGB", (width, height), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for k, (label, path) in enumerate(items[s * per:(s + 1) * per]):
            r, c = divmod(k, cols)
            x, y = pad + c * (cell + pad), pad + r * (cell + lab + pad)
            try:
                im = Image.open(path).convert("RGB")
            except Exception:
                continue
            im.thumbnail((cell, cell), Image.LANCZOS)
            sheet.paste(im, (x + (cell - im.width) // 2, y + lab + (cell - im.height) // 2))
            draw.text((x + 2, y + 2), label, fill=colour, font=font)
        name = out.format(s) if "{" in out else f"{out}_{s:02d}.jpg"
        sheet.save(name, quality=84, optimize=True)
        written.append(name)
    return written


def pair_sheet(pairs: list[tuple[str, Image.Image, Image.Image]], out: str, per_row: int = 3,
               per_sheet: int = 24) -> list[str]:
    """Side-by-side (small | large) pairs for checking matches by eye."""
    pw, ph = 430, 200
    font = _font(13)
    written = []
    for s in range(math.ceil(len(pairs) / per_sheet)):
        chunk = pairs[s * per_sheet:(s + 1) * per_sheet]
        n_rows = math.ceil(len(chunk) / per_row)
        sheet = Image.new("RGB", (pw * per_row + 20, (ph + 22) * n_rows + 10), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for k, (label, a, b) in enumerate(chunk):
            r, c = divmod(k, per_row)
            x, y = c * pw + 10, r * (ph + 22) + 22
            a, b = a.copy(), b.copy()
            a.thumbnail((170, 170))
            b.thumbnail((240, 240))
            sheet.paste(a, (x, y))
            sheet.paste(b, (x + 178, y))
            draw.text((x, y - 18), label[:60], fill=(255, 200, 90), font=font)
        name = out.format(s)
        sheet.save(name, quality=86)
        written.append(name)
    return written


# ---------- comparison ----------

def same_frame_distance(a: Image.Image, b: Image.Image, n: int = 32) -> float | None:
    """How different two photos are as whole frames: mean colour difference, 0-255.

    Returns None when the shapes differ, which means they can't be the same frame.
    Measured on a real library: burst frames score about 1-16, reframed shots of the
    same subject about 18, and unrelated photos of the same shape 42 or more.
    (A dHash fingerprint couldn't tell two orange horizons apart; this can.)
    """
    if abs((a.width / a.height) / (b.width / b.height) - 1) > 0.06:
        return None
    da = ImageChops.difference(a.resize((n, n), Image.LANCZOS), b.resize((n, n), Image.LANCZOS))
    return round(sum(ImageStat.Stat(da).mean) / 3, 1)


def saturation(im: Image.Image) -> float:
    return ImageStat.Stat(im.convert("HSV").split()[1]).mean[0]


def _prep(im: Image.Image, n: int = 24) -> Image.Image:
    return ImageOps.equalize(im.convert("L").resize((n, n), Image.LANCZOS))


def _diff(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def best_region(small: Image.Image, large: Image.Image) -> float:
    """How well `small` matches its best-fitting region of `large` (lower is better).

    Slides `small` over `large` at several scales, because small copies are often
    subject-aware crops rather than centred ones. Compares equalised greyscale, so
    brightness and contrast edits don't matter much.
    """
    target = _prep(small)
    aspect = small.width / small.height
    w_l, h_l = large.size
    best = float("inf")
    for f in (1.0, .9, .8, .7, .6, .5, .42, .35):
        h = int(min(h_l, w_l / aspect) * f)
        w = int(h * aspect)
        if w < 16 or h < 16:
            continue
        sx, sy = max(1, (w_l - w) // 10), max(1, (h_l - h) // 10)
        for y in range(0, h_l - h + 1, sy):
            for x in range(0, w_l - w + 1, sx):
                best = min(best, _diff(target, _prep(large.crop((x, y, x + w, y + h)))))
    return best


def match_score(small: Image.Image, large: Image.Image) -> float:
    """best_region plus a penalty when one is colour and the other black-and-white."""
    clash = (saturation(small) > 25) != (saturation(large) > 25)
    return best_region(small, large) + (25 if clash else 0)


def unletterbox(im: Image.Image, threshold: int = 18) -> tuple[Image.Image, bool]:
    """Crop black bars. Returns (image, was_letterboxed)."""
    box = ImageOps.grayscale(im).point(lambda v: 255 if v > threshold else 0).getbbox()
    if not box:
        return im, False
    cropped = im.crop(box)
    boxed = cropped.width < im.width * 0.92 or cropped.height < im.height * 0.92
    return (cropped, True) if boxed else (im, False)


def near_signature(im: Image.Image, n: int = 16) -> Image.Image:
    """Tiny equalised greyscale signature for fast whole-library content search."""
    return _prep(im, n)


def signature_distance(a: Image.Image, b: Image.Image) -> float:
    return _diff(a, b)
