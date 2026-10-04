"""Fetch iCloud-only originals so they can be reviewed at full quality.

Uses osxphotos' AppleScript download path. Its PhotoKit path (--use-photokit) needs the
separate Photos privacy permission, which Full Disk Access doesn't cover.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile


def download_missing(uuids: list[str], dest: str) -> str:
    """Ask Photos to download these originals (copies land in `dest`). Returns osxphotos' summary."""
    if not uuids:
        return "nothing to download"
    os.makedirs(dest, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(uuids))
        listfile = f.name
    try:
        r = subprocess.run([sys.executable, "-m", "osxphotos", "export", dest,
                            "--uuid-from-file", listfile, "--download-missing"],
                           capture_output=True, text=True)
    finally:
        os.unlink(listfile)
    summary = re.findall(r"Processed:.*", r.stdout + r.stderr)
    return summary[-1] if summary else (r.stderr.strip()[-400:] or "osxphotos finished")
