"""Read-only access to the Apple Photos library, via osxphotos.

Nothing in this module changes the library. Writes (adding to albums) go through
AppleScript in albums.py, because osxphotos is read-only.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

import osxphotos

# Photos computes per-image aesthetic scores. These are the ones that track composition;
# perspective counts double because that's what the project is about.
SCORE_WEIGHTS = {
    "pleasant_perspective": 2.0,
    "pleasant_symmetry": 1.5,
    "pleasant_pattern": 1.5,
    "pleasant_composition": 1.5,
    "well_framed_subject": 1.0,
    "pleasant_camera_tilt": 0.5,
    "interesting_subject": 0.5,
    "overall": 0.5,
}

# Imports from these apps are usually other people's photos, not the owner's own work.
MESSAGING_BUNDLES = ("whatsapp", "mobilesms", "telegram", "signal", "messenger")


def open_library(path: str | None = None) -> osxphotos.PhotosDB:
    """Open the Photos library (the last-opened one by default)."""
    return osxphotos.PhotosDB(dbfile=path) if path else osxphotos.PhotosDB()


def composite_score(photo) -> float | None:
    """Weighted sum of Photos' composition-related scores, or None if unscored."""
    s = photo.score
    if s is None:
        return None
    return round(sum(w * (getattr(s, k, 0) or 0) for k, w in SCORE_WEIGHTS.items()), 4)


def provenance(photo) -> dict:
    """Camera and importing app, and a best guess at whether the owner took it."""
    ex = photo.exif_info
    camera = (ex.camera_model if ex else None) or None
    importer = getattr(photo, "imported_by", None) or ()
    bundle = importer[1] if len(importer) > 1 else None
    messaging = any(m in (bundle or "").lower() for m in MESSAGING_BUNDLES)
    return {"camera": camera, "imported_by": bundle, "likely_own": bool(camera) and not messaging}


def album_photos(db, name: str) -> list:
    """Photos in live albums called `name`.

    db.photos(albums=[name]) also matches deleted albums that still linger in the database,
    which makes removed items look present. album_info only lists live albums.
    """
    seen, out = set(), []
    for a in db.album_info:
        if a.title == name:
            for p in a.photos:
                if p.uuid not in seen and not p.intrash:
                    seen.add(p.uuid)
                    out.append(p)
    return out


def local_path(photo) -> str | None:
    return photo.path_edited or photo.path


def select_candidates(db, *, since: dt.date | None = None, until: dt.date | None = None,
                      favourites_only: bool = False, exclude_dates: frozenset = frozenset(),
                      skip_album: str | None = None, min_side: int = 1500) -> tuple[list[dict], Counter]:
    """Still photos matching the filters, ranked by composite score (best first)."""
    stats: Counter = Counter()
    rows = []
    in_album = {x.uuid for x in album_photos(db, skip_album)} if skip_album else set()
    for p in db.photos(images=True, movies=False):
        if p.intrash or not p.date:
            continue
        day = p.date.date()
        if (since and day < since) or (until and day > until):
            continue
        stats["in_range"] += 1
        if favourites_only and not p.favorite:
            stats["skipped_not_favourite"] += 1
        elif day in exclude_dates:
            stats["skipped_excluded_date"] += 1
        elif p.uuid in in_album:
            stats["skipped_already_in_album"] += 1
        elif getattr(p, "screenshot", False):
            stats["skipped_screenshot"] += 1
        elif max(p.width or 0, p.height or 0) < min_side:
            stats["skipped_small_derivative"] += 1
        else:
            rows.append({
                "uuid": p.uuid, "file": p.original_filename, "date": p.date.isoformat()[:10],
                "favourite": bool(p.favorite), "local": bool(local_path(p)), "path": local_path(p),
                "score": composite_score(p), **provenance(p),
            })
    rows.sort(key=lambda r: -(r["score"] if r["score"] is not None else -99))
    stats["candidates"] = len(rows)
    return rows, stats


def refresh_paths(db, rows: list[dict]) -> int:
    """Re-read local paths, e.g. after downloading iCloud originals. Returns how many are local."""
    by_uuid = {p.uuid: p for p in db.photos(uuid=[r["uuid"] for r in rows])}
    for r in rows:
        p = by_uuid.get(r["uuid"])
        if p is not None:
            r["path"] = local_path(p)
            r["local"] = bool(r["path"])
    return sum(r["local"] for r in rows)
