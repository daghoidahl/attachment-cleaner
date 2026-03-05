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

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .messages_db import Attachment, MessagesDB
from .photos_bridge import PhotosAsset, find_matching_assets


@dataclass
class MatchResult:
    attachment: Attachment
    photos_assets: list[PhotosAsset]
    match_method: str  # "hash" | "timestamp" | "none"

    @property
    def has_match(self) -> bool:
        return bool(self.photos_assets)

    @property
    def is_confirmed(self) -> bool:
        """True when verified by hash (not just timestamp)."""
        return self.match_method == "hash"


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

            matches = find_matching_assets(
                attachment_path=resolved,
                attachment_date=att.created_date,
                tolerance_seconds=self.timestamp_tolerance,
                verify_hash=self.verify_hash,
            )

            method = "none"
            if matches:
                method = "hash" if self.verify_hash else "timestamp"

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
                if self.delete_attachment_file(result.attachment):
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
