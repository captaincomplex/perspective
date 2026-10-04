"""Swap low-resolution copies in an album for the originals already in the library.

Typical case: an album full of small squares exported for a display or a frame, each of
which has a full-size original somewhere in the library.

Each small copy is matched to its original by up to four independent routes:
  1. same filename stem              IMG_1234.png -> IMG_1234.HEIC
  2. library UUID inside the name    3D0D2CD9-..._1_105_c_386x386.png
  3. same camera + capture time      EXIF DateTimeOriginal within 2 s
  4. content search (--deep)         for letterboxed copies with nothing else to go on
and every candidate is scored visually (crop-aware, colour vs B&W aware).

Then the match is upgraded to the largest copy of *exactly the same frame*. That means the same
camera and the same timestamp to 50 ms, so a burst neighbour shot 0.2 s later never qualifies.
An edited version is never swapped for an unedited one.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from PIL import Image

from . import albums, images, photos

UUID_RE = re.compile(r"^([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})_", re.I)
UPGRADE_MARGIN = 8.0          # a bigger copy may score up to this much worse and still count as the same image


def _stem(name: str | None) -> str:
    return os.path.splitext(name or "")[0].lower()


def _area(p) -> int:
    return (p.width or 0) * (p.height or 0)


def _model(p) -> str:
    return (p.exif_info.camera_model if p.exif_info else None) or ""


def _naive(p):
    return p.date.replace(tzinfo=None) if p.date else None


def _exif_time(p):
    """DateTimeOriginal and camera model embedded in a small copy's own file, if any."""
    try:
        ex = Image.open(p.path).getexif()
        raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
        return (datetime.strptime(raw, "%Y:%m:%d %H:%M:%S") if raw else None), ex.get(272)
    except Exception:
        return None, None


class LibraryIndex:
    def __init__(self, db):
        self.photos = [p for p in db.photos(images=True, movies=False) if not p.intrash]
        self.by_uuid = {p.uuid.upper(): p for p in self.photos}
        self.by_stem: dict[str, list] = defaultdict(list)
        self.by_second: dict[datetime, list] = defaultdict(list)
        for p in self.photos:
            self.by_stem[_stem(p.original_filename)].append(p)
            if p.date:
                self.by_second[_naive(p).replace(microsecond=0)].append(p)

    def get(self, uuid: str):
        return self.by_uuid.get(uuid.upper())


def _candidates(lo, idx: LibraryIndex) -> tuple[dict, dict]:
    found, why = {}, defaultdict(set)

    def add(c, reason):
        if c.uuid != lo.uuid and _area(c) > _area(lo) * 2:
            found[c.uuid] = c
            why[c.uuid].add(reason)

    for c in idx.by_stem.get(_stem(lo.original_filename), []):
        add(c, "name")
    m = UUID_RE.match(lo.original_filename or "")
    if m and (c := idx.get(m.group(1))):
        add(c, "uuid")
    t, model = _exif_time(lo)
    if t:
        for s in range(-2, 3):
            for c in idx.by_second.get(t + timedelta(seconds=s), []):
                cm = _model(c)
                if not model or not cm or model in cm or cm in model:
                    add(c, "time")
    return found, why


def _content_search(small: Image.Image, lo, idx: LibraryIndex, shortlist: int = 12) -> dict:
    """Whole-library search for letterboxed copies: same aspect ratio, closest content."""
    aspect = small.width / small.height
    sig = images.near_signature(small)
    coarse = []
    for p in idx.photos:
        if p.uuid == lo.uuid or _area(p) <= _area(lo) * 2 or not (p.width and p.height):
            continue
        if abs((p.width / p.height) / aspect - 1) >= 0.04:
            continue
        im = images.render(p, 96)
        if im is not None:
            coarse.append((images.signature_distance(sig, images.near_signature(im)), p))
    coarse.sort(key=lambda t: t[0])
    return {p.uuid: p for _, p in coarse[:shortlist]}


def _same_frame(best, idx: LibraryIndex) -> list:
    """Other copies of exactly this frame: same camera, same timestamp (50 ms; 1 s if imprecise)."""
    t = _naive(best)
    if t is None:
        return [best]
    is_edit = "_edit" in _stem(best.original_filename)
    out = [best]
    for s in (-1, 0, 1):
        for c in idx.by_second.get(t.replace(microsecond=0) + timedelta(seconds=s), []):
            if c.uuid == best.uuid or _model(c) != _model(best):
                continue
            ct = _naive(c)
            tol = 1.0 if (ct.microsecond == 0 or t.microsecond == 0) else 0.05
            if abs((ct - t).total_seconds()) < tol and ("_edit" in _stem(c.original_filename)) == is_edit:
                out.append(c)
    return out


def match_album(db, album: str, max_mp: float = 1.0, deep: bool = False, log=print) -> list[dict]:
    """Find the original for every item in `album` under `max_mp` megapixels. Read-only."""
    idx = LibraryIndex(db)
    lows = [p for p in photos.album_photos(db, album) if _area(p) / 1e6 < max_mp]
    log(f"{album}: {len(lows)} items under {max_mp} MP")
    results = []
    for n, lo in enumerate(lows):
        small = images.render(lo, 380)
        if small is None:
            results.append({"id": f"L{n:03d}", "low_uuid": lo.uuid, "low_file": lo.original_filename,
                            "status": "unreadable"})
            continue
        small, letterboxed = images.unletterbox(small)
        cands, why = _candidates(lo, idx)
        if not cands and deep:
            cands = _content_search(small, lo, idx)
            why = {u: {"content"} for u in cands}
        scored = []
        for u, c in cands.items():
            big = images.render(c)
            if big is not None:
                scored.append((images.match_score(small, big), c))
        scored.sort(key=lambda t: t[0])
        r = {"id": f"L{n:03d}", "low_uuid": lo.uuid, "low_file": lo.original_filename,
             "low_size": f"{lo.width}x{lo.height}", "letterboxed": letterboxed}
        if not scored:
            r["status"] = "no candidate"
        else:
            best_score, best = scored[0]
            frames = _same_frame(best, idx)
            if len(frames) > 1:
                rendered = {c.uuid: images.render(c) for c in frames}
                fs = {u: images.match_score(small, im) for u, im in rendered.items() if im is not None}
                ok = [c for c in frames if fs.get(c.uuid, 1e9) <= best_score + UPGRADE_MARGIN] or [best]
                pick = max(ok, key=_area)
                if pick.uuid != best.uuid:
                    r["upgraded_from"] = f"{best.original_filename} {best.width}x{best.height}"
                    best, best_score = pick, fs[pick.uuid]
            r.update({"status": "matched", "score": round(best_score, 1), "hi_uuid": best.uuid,
                      "hi_file": best.original_filename, "hi_size": f"{best.width}x{best.height}",
                      "via": sorted(why.get(best.uuid, {"same-frame"})),
                      "runner_up": round(scored[1][0], 1) if len(scored) > 1 else None})
        results.append(r)
        if (n + 1) % 10 == 0:
            log(f"  {n + 1}/{len(lows)}")
    return results


def write_review(db, results: list[dict], workdir: str) -> list[str]:
    """Pair sheets (small | proposed original) so every match can be checked by eye."""
    os.makedirs(workdir, exist_ok=True)
    idx = {p.uuid: p for p in db.photos(uuid=[r["low_uuid"] for r in results] +
                                       [r["hi_uuid"] for r in results if r.get("hi_uuid")])}
    pairs = []
    for r in results:
        if r.get("status") != "matched":
            continue
        a, b = images.render(idx[r["low_uuid"]], 380), images.render(idx[r["hi_uuid"]], 480)
        if a is not None and b is not None:
            pairs.append((f"{r['id']} {r['score']} {'/'.join(r['via'])} -> {r['hi_size']}", a, b))
    return images.pair_sheet(pairs, os.path.join(workdir, "pairs_{:02d}.jpg"))


def save(results: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(results, f, indent=1)


def rebuild_album(db, album: str, results: list[dict], *, threshold: float, reject: set[str],
                  backup_name: str, log=print) -> tuple[int, int]:
    """Rename `album` to `backup_name`, then build a fresh `album` with the originals swapped in.

    Nothing is deleted: the old album survives under its new name, and no photo is ever removed
    from the library. Items are added oldest-first.
    """
    repl = {r["low_uuid"]: r["hi_uuid"] for r in results
            if r.get("status") == "matched" and r["score"] <= threshold and r["id"] not in reject}
    old = photos.album_photos(db, album)
    keep = [p.uuid for p in old if p.uuid not in repl]
    new_ids = list(dict.fromkeys(keep + list(repl.values())))
    dated = {p.uuid: p.date for p in db.photos(uuid=new_ids)}
    new_ids.sort(key=lambda u: dated[u].timestamp() if dated.get(u) else 0)
    log(f"replacing {len(repl)} low-res items with {len(set(repl.values()))} originals; "
        f"new '{album}' will hold {len(new_ids)} (old has {len(old)})")
    if albums.album_exists(backup_name):
        raise albums.AppleScriptError(f"an album called '{backup_name}' already exists; pick another --backup-name")
    albums.rename_album(album, backup_name)
    albums.create_album(album)
    n_new = albums.add_to_album(album, new_ids)
    return n_new, albums.album_count(backup_name)
