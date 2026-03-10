"""
Bridge to macOS Photos library.

Two strategies are used to find Photos that match a Messages attachment:

1. Hash match  — read the original file from the attachment folder and compute
   its SHA-256.  Then export a temporary copy of each Photos asset whose
   creation date is within a time window and compare hashes.  This is the
   most reliable method but is slow for large libraries.

2. Timestamp match — compare the attachment's created_date with the asset's
   creation date (within a configurable tolerance).  Fast, but may produce
   false positives.

The Photos library is queried via the `osascript` AppleScript bridge, which
works without any special entitlements as long as the user grants Photos access
when prompted.  For large libraries we fall back to the Photos SQLite database
(Photos Library.photoslibrary/database/Photos.sqlite) when direct osascript
calls would time-out.
"""

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

PHOTOS_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"
PHOTOS_DB = PHOTOS_LIBRARY / "database" / "Photos.sqlite"

# How close two timestamps must be (in seconds) to be considered a match
DEFAULT_TIMESTAMP_TOLERANCE = 10


@dataclass
class PhotosAsset:
    local_id: str          # Photos internal UUID
    filename: str
    creation_date: datetime
    width: int
    height: int
    media_type: int        # 1 = image, 2 = video
    favorite: bool

    @property
    def is_video(self) -> bool:
        return self.media_type == 2


# ---------------------------------------------------------------------------
# osascript helpers
# ---------------------------------------------------------------------------

def _run_applescript(script: str, timeout: int = 30) -> str:
    """Run an AppleScript and return stdout. Raises on failure."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    return result.stdout.strip()


def photos_is_available() -> bool:
    """Return True if the Photos app is accessible via osascript."""
    try:
        _run_applescript('tell application "Photos" to get name', timeout=5)
        return True
    except Exception:
        return False


def get_asset_count() -> int:
    """Return the total number of assets in the Photos library."""
    script = 'tell application "Photos" to return count of media items'
    return int(_run_applescript(script))


# ---------------------------------------------------------------------------
# Date-range query via osascript
# ---------------------------------------------------------------------------

_FETCH_ASSETS_IN_RANGE_SCRIPT = """\
on run argv
    set startSecs to (item 1 of argv) as integer
    set endSecs to (item 2 of argv) as integer
    set results to {}
    tell application "Photos"
        set allItems to every media item
        repeat with anItem in allItems
            set d to (date of anItem)
            -- AppleScript date: seconds since 1904-01-01
            -- Convert to Unix seconds: subtract 2082844800
            set unixSecs to (d as integer) - 2082844800
            if unixSecs >= startSecs and unixSecs <= endSecs then
                set localID to local identifier of anItem
                set fname to filename of anItem
                set mtype to (media type of anItem) as integer
                set isFav to (favorite of anItem) as integer
                set results to results & {localID & "|" & fname & "|" & (unixSecs as text) & "|" & (mtype as text) & "|" & (isFav as text)}
            end if
        end repeat
    end tell
    return results
end run
"""

# For large libraries the above loop is too slow.
# We use an alternative that fetches only recently-searched items via SQL.

def fetch_assets_in_time_range(
    start: datetime,
    end: datetime,
    timeout: int = 60,
) -> list[PhotosAsset]:
    """
    Query Photos for assets whose creation date falls in [start, end].

    Uses the Photos SQLite database directly (fast) when available,
    otherwise falls back to osascript (slow for large libraries).
    """
    if PHOTOS_DB.exists():
        return _fetch_assets_sql(start, end)
    return _fetch_assets_applescript(start, end, timeout)


def _fetch_assets_sql(start: datetime, end: datetime) -> list[PhotosAsset]:
    """
    Read Photos.sqlite directly.

    Table: ZASSET
    Relevant columns:
      ZUUID, ZFILENAME, ZDATECREATED (Mac absolute time, float),
      ZKIND (0=image, 1=video), ZFAVORITE (0/1),
      ZWIDTH, ZHEIGHT
    """
    import sqlite3

    # Mac absolute time: seconds since 2001-01-01 00:00:00 UTC
    MAC_EPOCH = 978307200
    start_mac = start.timestamp() - MAC_EPOCH
    end_mac = end.timestamp() - MAC_EPOCH

    # Open read-only
    uri = f"file:{PHOTOS_DB}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        raise RuntimeError(
            f"Cannot open Photos database at {PHOTOS_DB}: {exc}\n"
            "Try running: tccutil reset Photos"
        ) from exc

    query = """
        SELECT
            ZUUID,
            ZFILENAME,
            ZDATECREATED,
            ZKIND,
            ZFAVORITE,
            ZWIDTH,
            ZHEIGHT
        FROM ZASSET
        WHERE ZDATECREATED >= ? AND ZDATECREATED <= ?
          AND ZTRASHEDSTATE = 0
    """
    assets = []
    try:
        for row in conn.execute(query, (start_mac, end_mac)):
            unix_ts = (row["ZDATECREATED"] or 0) + MAC_EPOCH
            dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
            assets.append(
                PhotosAsset(
                    local_id=row["ZUUID"] or "",
                    filename=row["ZFILENAME"] or "",
                    creation_date=dt,
                    width=row["ZWIDTH"] or 0,
                    height=row["ZHEIGHT"] or 0,
                    media_type=(row["ZKIND"] or 0) + 1,  # ZKIND: 0=image,1=video → remap to 1=image,2=video
                    favorite=bool(row["ZFAVORITE"]),
                )
            )
    finally:
        conn.close()
    return assets


def _fetch_assets_applescript(
    start: datetime,
    end: datetime,
    timeout: int = 60,
) -> list[PhotosAsset]:
    start_unix = int(start.timestamp())
    end_unix = int(end.timestamp())
    script = _FETCH_ASSETS_IN_RANGE_SCRIPT
    result = subprocess.run(
        ["osascript", "-", str(start_unix), str(end_unix)],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")

    assets = []
    for line in result.stdout.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        local_id, fname, unix_str, mtype_str, fav_str = parts[:5]
        dt = datetime.fromtimestamp(int(unix_str), tz=timezone.utc)
        assets.append(
            PhotosAsset(
                local_id=local_id.strip(),
                filename=fname.strip(),
                creation_date=dt,
                width=0,
                height=0,
                media_type=int(mtype_str.strip()),
                favorite=fav_str.strip() == "1",
            )
        )
    return assets


# ---------------------------------------------------------------------------
# Export a Photos asset to a temp file for hash comparison
# ---------------------------------------------------------------------------

_EXPORT_ASSET_SCRIPT = """\
on run argv
    set localID to item 1 of argv
    set destFolder to item 2 of argv
    tell application "Photos"
        set theItem to media item id localID
        export {theItem} to (POSIX file destFolder) with using originals
    end tell
end run
"""


def export_asset_for_hashing(local_id: str) -> Path | None:
    """
    Export a Photos asset to a temp directory and return the path.
    Returns None if export fails.
    """
    tmp_dir = tempfile.mkdtemp(prefix="att_cleaner_")
    result = subprocess.run(
        ["osascript", "-", local_id, tmp_dir],
        input=_EXPORT_ASSET_SCRIPT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    # Find whatever was exported
    exported = list(Path(tmp_dir).iterdir())
    return exported[0] if exported else None


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# High-level matching
# ---------------------------------------------------------------------------

def find_matching_assets(
    attachment_path: Path,
    attachment_date: datetime,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE,
    verify_hash: bool = False,
) -> list[PhotosAsset]:
    """
    Return Photos assets that likely correspond to the given attachment.

    First narrows by timestamp (±tolerance_seconds), then optionally
    verifies by hash.
    """
    window_start = attachment_date - timedelta(seconds=tolerance_seconds)
    window_end = attachment_date + timedelta(seconds=tolerance_seconds)

    candidates = fetch_assets_in_time_range(window_start, window_end)

    if not candidates:
        return []

    if not verify_hash:
        return candidates

    # Hash-based verification
    att_hash = _hash_file_safe(attachment_path)
    if att_hash is None:
        return candidates  # Can't hash, return timestamp matches

    confirmed = []
    for asset in candidates:
        exported = export_asset_for_hashing(asset.local_id)
        if exported is None:
            # Export failed — keep as candidate since timestamps matched
            confirmed.append(asset)
            continue
        try:
            if hash_file(exported) == att_hash:
                confirmed.append(asset)
        finally:
            try:
                exported.unlink()
                exported.parent.rmdir()
            except OSError:
                pass

    return confirmed


def _hash_file_safe(path: Path) -> str | None:
    try:
        return hash_file(path)
    except OSError:
        return None
