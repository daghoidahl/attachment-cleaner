"""
Core logic: scan Messages attachments, find matches in Photos, and delete.

Deletion model
--------------
We never delete a Photos asset — only the local copy stored inside
~/Library/Messages/Attachments/.  Removing the file from that folder
frees disk space on both Mac and (after sync) iPhone without touching
your Photos library.

Matching strategies (in order of reliability):
  1. Hash match  — export the Photos asset and compare SHA-256.
  2. Timestamp   — attachment created_date ≈ Photos asset creation date.

When running in "dry-run" mode nothing is deleted; a report is printed.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .messages_db import Attachment, MessagesDB
from .photos_bridge import PhotosAsset, find_matching_assets

CACHE_DIR = Path.home() / ".cache" / "attachment-cleaner"
CACHE_FILE = CACHE_DIR / "scan_cache.json"


@dataclass
class MatchResult:
    attachment: Attachment
    photos_assets: list[PhotosAsset]
    match_method: str  # "hash" | "filename" | "metadata_timestamp" | "timestamp" | "none"

    @property
    def has_match(self) -> bool:
        return bool(self.photos_assets)

    @property
    def is_confirmed(self) -> bool:
        """True when verified by hash or exact filename (not just timestamp)."""
        return self.match_method in ("hash", "filename")


@dataclass
class CleanupPlan:
    """The result of a scan: what can be deleted and what was skipped."""

    matched: list[MatchResult] = field(default_factory=list)
    unmatched: list[Attachment] = field(default_factory=list)
    missing_file: list[Attachment] = field(default_factory=list)  # file already gone

    @property
    def reclaimable_bytes(self) -> int:
        return sum(r.attachment.total_bytes for r in self.matched)

    @property
    def reclaimable_mb(self) -> float:
        return self.reclaimable_bytes / (1024 * 1024)

    @property
    def total_scanned(self) -> int:
        return len(self.matched) + len(self.unmatched) + len(self.missing_file)


# ------------------------------------------------------------------
# Cache helpers
# ------------------------------------------------------------------

def _datetime_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def plan_to_dict(plan: CleanupPlan) -> dict:
    return dataclasses.asdict(plan)


def _attachment_from_dict(d: dict) -> Attachment:
    known = {f.name for f in dataclasses.fields(Attachment)}
    filtered = {k: v for k, v in d.items() if k in known}
    if "created_date" in filtered and isinstance(filtered["created_date"], str):
        filtered["created_date"] = datetime.fromisoformat(filtered["created_date"])
    return Attachment(**filtered)


def _match_result_from_dict(d: dict) -> MatchResult:
    att = _attachment_from_dict(d["attachment"])
    parsed_assets = []
    for a in d.get("photos_assets", []):
        if isinstance(a, dict):
            a = dict(a)
            if "creation_date" in a and isinstance(a["creation_date"], str):
                a["creation_date"] = datetime.fromisoformat(a["creation_date"])
            known_fields = {f.name for f in dataclasses.fields(PhotosAsset)}
            filtered_a = {k: v for k, v in a.items() if k in known_fields}
            parsed_assets.append(PhotosAsset(**filtered_a))
        else:
            parsed_assets.append(a)
    return MatchResult(
        attachment=att,
        photos_assets=parsed_assets,
        match_method=d.get("match_method", "none"),
    )


def plan_from_dict(d: dict) -> CleanupPlan:
    return CleanupPlan(
        matched=[_match_result_from_dict(r) for r in d.get("matched", [])],
        unmatched=[_attachment_from_dict(a) for a in d.get("unmatched", [])],
        missing_file=[_attachment_from_dict(a) for a in d.get("missing_file", [])],
    )


def load_cached_plan(db_mtime: float, scan_params: dict) -> tuple[CleanupPlan, str] | None:
    """Load cached plan if db_mtime and scan_params match. Returns (plan, cached_at_iso) or None."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if data.get("db_mtime") != db_mtime:
            return None
        if data.get("scan_params") != scan_params:
            return None
        plan = plan_from_dict(data["plan"])
        return plan, data["cached_at"]
    except Exception:
        return None


def save_cached_plan(plan: CleanupPlan, db_mtime: float, scan_params: dict) -> None:
    """Write plan to cache file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "db_mtime": db_mtime,
        "cached_at": datetime.now(tz=timezone.utc).isoformat(),
        "scan_params": scan_params,
        "plan": plan_to_dict(plan),
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, default=_datetime_serializer)


class AttachmentCleaner:
    def __init__(
        self,
        min_bytes: int = 100_000,          # 100 KB minimum to bother with
        media_only: bool = True,
        timestamp_tolerance: int = 10,     # seconds
        verify_hash: bool = False,         # slow but reliable
        progress_cb: Callable[[int, int, str], None] | None = None,
    ):
        self.min_bytes = min_bytes
        self.media_only = media_only
        self.timestamp_tolerance = timestamp_tolerance
        self.verify_hash = verify_hash
        self.progress_cb = progress_cb or (lambda cur, tot, name: None)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(self, db: MessagesDB) -> CleanupPlan:
        """
        Scan all attachments in the Messages DB and match them against Photos.
        Returns a CleanupPlan describing what can be cleaned up.
        """
        attachments = list(
            db.iter_attachments(
                min_bytes=self.min_bytes,
                media_only=self.media_only,
            )
        )
        total = len(attachments)
        plan = CleanupPlan()

        for idx, att in enumerate(attachments):
            name = att.transfer_name or Path(att.filename).name if att.filename else "?"
            self.progress_cb(idx + 1, total, name)

            resolved = att.resolved_path
            if resolved is None:
                # File is already gone from disk (iCloud offloaded or manually removed)
                plan.missing_file.append(att)
                continue

            matches, method = find_matching_assets(
                attachment_path=resolved,
                attachment_date=att.created_date,
                transfer_name=att.transfer_name,
                tolerance_seconds=self.timestamp_tolerance,
                verify_hash=self.verify_hash,
            )

            result = MatchResult(
                attachment=att,
                photos_assets=matches,
                match_method=method,
            )
            if matches:
                plan.matched.append(result)
            else:
                plan.unmatched.append(att)

        return plan

    # ------------------------------------------------------------------
    # Execute deletions
    # ------------------------------------------------------------------

    def delete_attachment_file(self, attachment: Attachment) -> bool:
        """
        Delete the on-disk file for this attachment.
        Returns True on success.

        The Messages DB record is intentionally left intact — iMessage
        still shows the message with a placeholder.  Only the local file
        is removed, reclaiming disk space.
        """
        path = attachment.resolved_path
        if path is None:
            return False
        try:
            path.unlink()
            # Try to clean up empty parent directories (Messages nests files in
            # hash-named subdirectories)
            _remove_empty_parents(path.parent, stop_at=Path.home() / "Library" / "Messages")
            return True
        except OSError:
            return False

    def delete_message_via_applescript(self, attachment: Attachment) -> bool:
        """Delete the message from Messages.app. Returns True on success."""
        import subprocess
        guid = attachment.message_guid
        chat = attachment.chat_identifier
        if not guid:
            return False
        script = f'''
tell application "Messages"
    try
        set targetChat to (first chat whose id = "{chat}")
        repeat with aMsg in (every message of targetChat)
            if guid of aMsg = "{guid}" then
                delete aMsg
                return "ok"
            end if
        end repeat
    end try
    return "not found"
end tell'''
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
            return r.returncode == 0 and "ok" in r.stdout
        except Exception:
            return False

    def delete_attachment(self, attachment: Attachment) -> tuple[bool, str]:
        """Try message deletion first, fall back to file deletion."""
        if attachment.message_guid and self.delete_message_via_applescript(attachment):
            return True, "message"
        if self.delete_attachment_file(attachment):
            return True, "file"
        return False, "failed"

    def delete_from_plan(
        self,
        plan: CleanupPlan,
        dry_run: bool = True,
        confirmed_only: bool = False,
    ) -> tuple[int, int]:
        """
        Delete attachment files from matched results in the plan.

        Args:
            dry_run:        If True, only report what would be deleted.
            confirmed_only: If True, skip timestamp-only matches (hash not verified).

        Returns:
            (deleted_count, deleted_bytes)
        """
        deleted_count = 0
        deleted_bytes = 0

        for result in plan.matched:
            if confirmed_only and not result.is_confirmed:
                continue
            if dry_run:
                deleted_count += 1
                deleted_bytes += result.attachment.total_bytes
            else:
                success, _ = self.delete_attachment(result.attachment)
                if success:
                    deleted_count += 1
                    deleted_bytes += result.attachment.total_bytes

        return deleted_count, deleted_bytes


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _remove_empty_parents(directory: Path, stop_at: Path) -> None:
    """Remove empty parent directories up to (but not including) stop_at."""
    current = directory
    while current != stop_at and current != current.parent:
        try:
            current.rmdir()  # only removes if empty
            current = current.parent
        except OSError:
            break
