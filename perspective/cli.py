"""perspective: command-line entry point.

    perspective scan     rank photos by Photos' composition scores        (read-only)
    perspective fetch    download iCloud-only originals of the shortlist
    perspective sheets   thumbnails + labelled contact sheets              (read-only)
    perspective verify   bigger sheet of chosen IDs for a second look      (read-only)
    perspective check    near-duplicates vs the album, and provenance      (read-only)
    perspective add      add chosen IDs to an album
    perspective lowres   swap low-res copies in an album for their originals
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import __version__, albums, icloud, images, lowres, photos


def _load(work: str) -> list[dict]:
    path = os.path.join(work, "candidates.json")
    if not os.path.exists(path):
        sys.exit(f"no {path} — run `perspective scan` first")
    with open(path) as f:
        return json.load(f)


def _save(work: str, rows: list[dict], name: str = "candidates.json") -> None:
    with open(os.path.join(work, name), "w") as f:
        json.dump(rows, f, indent=1)


def _pick(rows: list[dict], ids: list[str]) -> list[dict]:
    by_id = {r["id"].upper(): r for r in rows}
    missing = [i for i in ids if i.upper() not in by_id]
    if missing:
        sys.exit(f"unknown id(s): {' '.join(missing)}")
    return [by_id[i.upper()] for i in ids]


def _thumb(work: str, r: dict) -> str:
    return os.path.join(work, "thumbs", f"{r['id']}.jpg")


# ---------------- commands ----------------

def cmd_scan(a) -> None:
    os.makedirs(a.work, exist_ok=True)
    db = photos.open_library(a.library)
    rows, stats = photos.select_candidates(
        db, since=a.since, until=a.until, favourites_only=a.favourites_only,
        exclude_dates=frozenset(a.exclude_date or ()), skip_album=a.album, min_side=a.min_side)
    selected = rows[:a.top]
    if a.include_favourites:
        selected += [r for r in rows[a.top:] if r["favourite"]]
    for i, r in enumerate(selected):
        r["id"] = f"P{i:03d}"
    _save(a.work, rows, "ranked.json")
    _save(a.work, selected)
    for k, v in stats.items():
        print(f"  {k:<28}{v}")
    print(f"selected {len(selected)} for review (top {a.top}"
          f"{' + favourites' if a.include_favourites else ''}); "
          f"iCloud-only: {sum(not r['local'] for r in selected)} (run `perspective fetch`)")


def cmd_fetch(a) -> None:
    rows = _load(a.work)
    missing = [r["uuid"] for r in rows if not r["local"]]
    print(f"downloading {len(missing)} iCloud-only originals…")
    print(" ", icloud.download_missing(missing, os.path.join(a.work, "icloud-copies")))
    n = photos.refresh_paths(photos.open_library(a.library), rows)
    _save(a.work, rows)
    print(f"{n}/{len(rows)} now local")


def cmd_sheets(a) -> None:
    rows = _load(a.work)
    os.makedirs(os.path.join(a.work, "thumbs"), exist_ok=True)
    os.makedirs(os.path.join(a.work, "sheets"), exist_ok=True)
    ok = [r for r in rows if r.get("path") and images.export_thumb(r["path"], _thumb(a.work, r))]
    label = lambda r: r["id"] + (" *" if r["favourite"] else "") + ("" if r["likely_own"] else " ?")
    sheets = images.contact_sheet([(label(r), _thumb(a.work, r)) for r in ok],
                                  os.path.join(a.work, "sheets", "sheet_{:02d}.jpg"))
    print(f"{len(ok)}/{len(rows)} thumbnailed -> {len(sheets)} sheets in {a.work}/sheets")
    print("  * = favourite   ? = probably not your photo (messaging import or no camera data)")


def cmd_verify(a) -> None:
    rows = _pick(_load(a.work), a.ids)
    out = images.contact_sheet([(r["id"], _thumb(a.work, r)) for r in rows],
                               os.path.join(a.work, "verify_{:02d}.jpg"), cols=3, rows=3, cell=560)
    print("\n".join(out))


def cmd_check(a) -> None:
    rows = _pick(_load(a.work), a.ids)
    db = photos.open_library(a.library)
    album = [(im, p.original_filename) for p in photos.album_photos(db, a.album)
             if (im := images.render(p)) is not None]
    print(f"compared against {len(album)} photos in '{a.album}'  "
          f"(≤{a.dup} = almost exactly the same frame, ≤{a.similar} = similar)")

    def nearest(im, pool):
        scored = [(d, name) for other, name in pool if (d := images.same_frame_distance(im, other)) is not None]
        return min(scored, default=(999.0, "-"))

    seen = []
    for r in rows:
        im = images.open_rgb(_thumb(a.work, r))
        flags = []
        for (d, name), where in ((nearest(im, album), "album"), (nearest(im, seen), "this shortlist")):
            if d <= a.dup:
                flags.append(f"DUPLICATE of {name} in {where} ({d})")
            elif d <= a.similar:
                flags.append(f"similar to {name} in {where} ({d})")
        if not r["likely_own"]:
            flags.append(f"NOT YOURS? camera={r['camera']} via={r['imported_by']}")
        print(f"  {r['id']} {r['file']:<28} {' | '.join(flags) or 'ok'}")
        seen.append((im, r["id"]))


def cmd_add(a) -> None:
    rows = _pick(_load(a.work), a.ids)
    others = [r["id"] for r in rows if not r["likely_own"]]
    if others and not a.include_others:
        sys.exit(f"{' '.join(others)} look like other people's photos; pass --include-others to add anyway")
    print(f"adding {len(rows)} to '{a.album}': " + ", ".join(r["file"] for r in rows))
    if a.dry_run:
        print("dry run — nothing changed")
        return
    print(f"'{a.album}' now holds {albums.add_to_album(a.album, [r['uuid'] for r in rows], create=True)}")


def cmd_lowres(a) -> None:
    work = os.path.join(a.work, "lowres")
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "matches.json")
    db = photos.open_library(a.library)
    if not a.go:
        results = lowres.match_album(db, a.album, max_mp=a.max_mp, deep=a.deep)
        lowres.save(results, path)
        sheets = lowres.write_review(db, results, work)
        matched = [r for r in results if r.get("status") == "matched"]
        strong = [r for r in matched if r["score"] <= a.threshold]
        print(f"matched {len(matched)}/{len(results)}, {len(strong)} at score ≤ {a.threshold}; "
              f"{sum('upgraded_from' in r for r in matched)} upgraded to a larger copy of the same frame")
        for r in results:
            if r.get("status") != "matched" or r["score"] > a.threshold:
                print(f"  {r['id']} {r['low_file']}: {r.get('status')} {r.get('score', '')}")
        print(f"check every pair by eye: {', '.join(sheets) or '(none)'}")
        print("then: perspective lowres --album ... --go [--reject L003 L017 …]")
        return
    if not os.path.exists(path):
        sys.exit("run without --go first, and check the pair sheets")
    with open(path) as f:
        results = json.load(f)
    backup = a.backup_name or f"{a.album} (old low-res)"
    n_new, n_old = lowres.rebuild_album(db, a.album, results, threshold=a.threshold,
                                        reject={x.upper() for x in a.reject or ()}, backup_name=backup)
    print(f"done: '{a.album}' = {n_new}; previous version kept as '{backup}' = {n_old}")


# ---------------- argument parsing ----------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="perspective", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"perspective {__version__}")
    ap.add_argument("--library", help="path to a .photoslibrary (default: the last one opened)")
    ap.add_argument("--work", default="work", help="working folder for shortlists and sheets (default: ./work)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    date = dt.date.fromisoformat

    s = sub.add_parser("scan", help="rank photos by Photos' own composition scores")
    s.add_argument("--since", type=date, help="taken on or after YYYY-MM-DD")
    s.add_argument("--until", type=date, help="taken on or before YYYY-MM-DD")
    s.add_argument("--favourites-only", action="store_true")
    s.add_argument("--exclude-date", type=date, action="append", help="skip a whole day (repeatable)")
    s.add_argument("--album", default="Perspective", help="skip photos already in this album")
    s.add_argument("--top", type=int, default=180, help="how many top-ranked photos to review")
    s.add_argument("--include-favourites", action=argparse.BooleanOptionalAction, default=True,
                   help="also review every favourite outside the top N (default: yes)")
    s.add_argument("--min-side", type=int, default=1500, help="ignore images smaller than this (derivatives)")
    s.set_defaults(fn=cmd_scan)

    sub.add_parser("fetch", help="download iCloud-only originals of the shortlist").set_defaults(fn=cmd_fetch)
    sub.add_parser("sheets", help="thumbnails and labelled contact sheets").set_defaults(fn=cmd_sheets)

    v = sub.add_parser("verify", help="bigger sheet of chosen IDs")
    v.add_argument("ids", nargs="+")
    v.set_defaults(fn=cmd_verify)

    c = sub.add_parser("check", help="near-duplicates vs the album, and provenance")
    c.add_argument("ids", nargs="+")
    c.add_argument("--album", default="Perspective")
    c.add_argument("--dup", type=float, default=12, help="frame distance counting as a duplicate (default 12)")
    c.add_argument("--similar", type=float, default=25, help="frame distance worth mentioning (default 25)")
    c.set_defaults(fn=cmd_check)

    d = sub.add_parser("add", help="add chosen IDs to an album (created if missing)")
    d.add_argument("ids", nargs="+")
    d.add_argument("--album", default="Perspective")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--include-others", action="store_true", help="allow photos that look like someone else's")
    d.set_defaults(fn=cmd_add)

    l = sub.add_parser("lowres", help="swap low-res copies in an album for their originals")
    l.add_argument("--album", required=True)
    l.add_argument("--max-mp", type=float, default=1.0, help="treat items under this many megapixels as low-res")
    l.add_argument("--deep", action="store_true", help="whole-library content search for unmatched items (slow)")
    l.add_argument("--threshold", type=float, default=30.0, help="max match score to accept (lower = closer)")
    l.add_argument("--go", action="store_true", help="apply the saved matches (default: dry run)")
    l.add_argument("--reject", nargs="*", help="match IDs (L###) to leave alone")
    l.add_argument("--backup-name", help="name for the preserved old album")
    l.set_defaults(fn=cmd_lowres)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except albums.AppleScriptError as e:
        print(f"Photos/AppleScript error: {e}", file=sys.stderr)
        return 1
    return 0
