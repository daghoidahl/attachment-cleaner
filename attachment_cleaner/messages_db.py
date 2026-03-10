"""
Read and query the Messages SQLite database on macOS.

The database lives at ~/Library/Messages/chat.db.
Dates in the DB use Mac Absolute Time: seconds since 2001-01-01 00:00:00 UTC.
Since macOS Catalina (10.15) the timestamps are in nanoseconds.
"""

import sqlite3
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Mac Absolute Time epoch: 2001-01-01 00:00:00 UTC
MAC_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Mac epoch

# Attachments directory
MESSAGES_DIR = Path.home() / "Library" / "Messages"
ATTACHMENTS_DIR = MESSAGES_DIR / "Attachments"
CHAT_DB = MESSAGES_DIR / "chat.db"


def mac_time_to_datetime(mac_time: float) -> datetime:
    """Convert Mac Absolute Time (seconds or nanoseconds since 2001-01-01) to datetime."""
    # Heuristic: if value is huge it's nanoseconds (macOS Catalina+)
    if mac_time > 1e15:
        mac_time = mac_time / 1e9
    unix_time = mac_time + MAC_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_time, tz=timezone.utc)


@dataclass
class Attachment:
    rowid: int
    filename: str  # relative path starting with ~/...
    mime_type: str | None
    total_bytes: int
    created_date: datetime
    transfer_name: str | None  # original filename
    chat_identifier: str | None = None   # phone/email of the conversation partner
    message_guid: str | None = None      # for message deletion

    @property
    def resolved_path(self) -> Path | None:
        """Resolve the attachment path to an absolute path."""
        if not self.filename:
            return None
        path = Path(self.filename.replace("~", str(Path.home()), 1))
        return path if path.exists() else None

    @property
    def size_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def is_image(self) -> bool:
        if self.mime_type:
            return self.mime_type.startswith("image/")
        if self.transfer_name:
            return Path(self.transfer_name).suffix.lower() in {
                ".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".bmp", ".tiff",
            }
        return False

    @property
    def is_video(self) -> bool:
        if self.mime_type:
            return self.mime_type.startswith("video/")
        if self.transfer_name:
            return Path(self.transfer_name).suffix.lower() in {
                ".mov", ".mp4", ".m4v", ".avi", ".mkv",
            }
        return False

    @property
    def is_media(self) -> bool:
        return self.is_image or self.is_video

    def compute_hash(self) -> str | None:
        """SHA-256 hash of the attachment file, or None if file is missing."""
        path = self.resolved_path
        if path is None:
            return None
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None


@dataclass
class Conversation:
    rowid: int
    chat_identifier: str  # phone/email
    display_name: str | None
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def total_attachment_bytes(self) -> int:
        return sum(a.total_bytes for a in self.attachments)


class MessagesDB:
    def __init__(self, db_path: Path = CHAT_DB):
        if not db_path.exists():
            raise FileNotFoundError(
                f"Messages database not found at {db_path}.\n"
                "Make sure you are running this on macOS with Messages app installed."
            )
        # Open read-only to avoid corrupting the live DB
        uri = f"file:{db_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def db_mtime(self) -> float:
        """Return the modification time of the Messages database file."""
        return CHAT_DB.stat().st_mtime

    def iter_attachments(
        self,
        min_bytes: int = 0,
        media_only: bool = False,
    ) -> Iterator[Attachment]:
        """Yield all attachments, optionally filtered by size or type."""
        query = """
            SELECT
                a.ROWID,
                a.filename,
                a.mime_type,
                a.total_bytes,
                a.created_date,
                a.transfer_name,
                MIN(c.chat_identifier) AS chat_identifier,
                MIN(m.guid)            AS message_guid
            FROM attachment a
            LEFT JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            LEFT JOIN message m ON m.ROWID = maj.message_id
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            LEFT JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE a.total_bytes >= ?
            GROUP BY a.ROWID
            ORDER BY a.total_bytes DESC
        """
        cursor = self._conn.execute(query, (min_bytes,))
        for row in cursor:
            att = Attachment(
                rowid=row["ROWID"],
                filename=row["filename"] or "",
                mime_type=row["mime_type"],
                total_bytes=row["total_bytes"] or 0,
                created_date=mac_time_to_datetime(row["created_date"] or 0),
                transfer_name=row["transfer_name"],
                chat_identifier=row["chat_identifier"],
                message_guid=row["message_guid"],
            )
            if media_only and not att.is_media:
                continue
            yield att

    def iter_conversations_with_attachments(
        self,
        min_bytes: int = 0,
        media_only: bool = False,
    ) -> Iterator[Conversation]:
        """Yield conversations together with their attachments."""
        query = """
            SELECT
                c.ROWID        AS chat_rowid,
                c.chat_identifier,
                c.display_name,
                a.ROWID        AS att_rowid,
                a.filename,
                a.mime_type,
                a.total_bytes,
                a.created_date,
                a.transfer_name
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            JOIN message_attachment_join maj ON m.ROWID = maj.message_id
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE a.total_bytes >= ?
            ORDER BY c.ROWID, a.total_bytes DESC
        """
        cursor = self._conn.execute(query, (min_bytes,))

        current_conv: Conversation | None = None
        for row in cursor:
            att = Attachment(
                rowid=row["att_rowid"],
                filename=row["filename"] or "",
                mime_type=row["mime_type"],
                total_bytes=row["total_bytes"] or 0,
                created_date=mac_time_to_datetime(row["created_date"] or 0),
                transfer_name=row["transfer_name"],
            )
            if media_only and not att.is_media:
                continue

            if current_conv is None or current_conv.rowid != row["chat_rowid"]:
                if current_conv is not None:
                    yield current_conv
                current_conv = Conversation(
                    rowid=row["chat_rowid"],
                    chat_identifier=row["chat_identifier"] or "",
                    display_name=row["display_name"],
                )
            current_conv.attachments.append(att)

        if current_conv is not None:
            yield current_conv

    def total_attachment_size(self) -> int:
        """Total bytes of all attachments in the DB."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(total_bytes), 0) FROM attachment"
        ).fetchone()
        return row[0]

    def attachment_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM attachment").fetchone()
        return row[0]
