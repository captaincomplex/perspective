"""Writing to Photos albums via AppleScript.

Lessons baked in:
- Never ask Photos for `media items` across the whole library by AppleScript; on a large
  library it materialises everything and wedges Photos. Address items by id only.
- Media item ids are "<uuid>/L0/001".
- AppleScript won't accept bare line breaks inside a list, so each list goes on one line.
- AppleScript can't remove items from an album. Removing means rebuilding the album.
"""
from __future__ import annotations

import subprocess


class AppleScriptError(RuntimeError):
    pass


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def osa(script: str, timeout: int = 900) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise AppleScriptError(r.stderr.strip())
    return r.stdout.strip()


def item_ref(uuid: str) -> str:
    return f'media item id "{uuid}/L0/001"'


def album_exists(name: str) -> bool:
    return osa(f'tell application "Photos" to return exists album "{_q(name)}"') == "true"


def album_count(name: str) -> int:
    return int(osa(f'tell application "Photos" to return (count of media items of album "{_q(name)}") as text'))


def album_filenames(name: str) -> list[str]:
    out = osa('with timeout of 600 seconds\n'
              f'tell application "Photos" to set f to filename of media items of album "{_q(name)}"\n'
              "set AppleScript's text item delimiters to linefeed\nreturn f as text\nend timeout")
    return out.splitlines()


def create_album(name: str) -> None:
    osa(f'tell application "Photos" to make new album named "{_q(name)}"')


def rename_album(old: str, new: str) -> None:
    osa(f'tell application "Photos" to set name of album "{_q(old)}" to "{_q(new)}"')


def add_to_album(name: str, uuids: list[str], chunk: int = 60, create: bool = False) -> int:
    """Add photos by uuid, in order, a chunk at a time. Returns the album's new count."""
    if create and not album_exists(name):
        create_album(name)
    for i in range(0, len(uuids), chunk):
        refs = ", ".join(item_ref(u) for u in uuids[i:i + chunk])
        osa(f'with timeout of 600 seconds\ntell application "Photos" to add {{{refs}}} '
            f'to album "{_q(name)}"\nend timeout')
    return album_count(name)
