# Messages Attachment Cleaner

A macOS command-line tool that finds large attachments in your iMessage history
that are **already saved in your Photos library** and helps you delete the local
copies to free up disk space — on both your Mac and (after sync) your iPhone.

## How it works

1. Reads `~/Library/Messages/chat.db` (read-only) to list all attachments with
   their filenames, sizes, and timestamps.
2. Queries your Photos library — via the fast Photos SQLite database or
   AppleScript — to find assets with matching creation timestamps.
3. Optionally exports each Photos asset and compares its SHA-256 hash to the
   attachment file for a confirmed match.
4. Shows you a summary of reclaimable space and lets you delete the attachment
   files interactively or automatically.

> **What gets deleted?**  Only the local file inside
> `~/Library/Messages/Attachments/`.  The Messages chat record is kept intact
> (you'll see a "tap to download" placeholder).  Your Photos library is never
> touched.

## Requirements

- macOS 12 Monterey or later (tested on 13 Ventura / 14 Sonoma)
- Python 3.10+
- Terminal app must have **Full Disk Access** (see below)
- `rich` Python library (`pip install rich`)

### Full Disk Access

Messages database access requires Full Disk Access for your terminal:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Add your terminal app (Terminal, iTerm2, Ghostty, …)
3. Restart the terminal

## Installation

```bash
git clone <this-repo>
cd attachment-cleaner
pip install rich          # only dependency
```

## Usage

### 1. Scan and show a summary (safe — read-only)

```bash
python main.py
```

Scans all media attachments ≥ 100 KB and prints a table of the top 20
reclaimable files along with their Photos matches.

### 2. Interactive review (recommended first run)

```bash
python main.py --review
```

Steps through each matched attachment from largest to smallest.  For each one,
shows the filename, size, date, and the matching Photos asset, then asks:

```
Delete this attachment file? [y/n/q]:
```

### 3. Preview auto-clean without deleting

```bash
python main.py --auto --dry-run
```

### 4. Auto-delete (hash-verified matches only — safest)

```bash
python main.py --auto --verify-hash --confirmed-only
```

Exports each candidate Photos asset, computes SHA-256, and only deletes the
attachment if the hashes match exactly.  Slower but zero false positives.

### 5. Auto-delete all timestamp-matched files

```bash
python main.py --auto
```

Uses timestamp proximity (±10 seconds) to match.  Fast but may rarely
match unrelated files taken at the exact same second.

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--min-size MB` | `0.1` | Minimum attachment size in MB |
| `--no-media-only` | off | Include PDFs and other non-media files |
| `--tolerance SECS` | `10` | Timestamp window for matching (seconds) |
| `--verify-hash` | off | Export Photos assets and compare SHA-256 |
| `--review` | off | Interactive per-file review |
| `--auto` | off | Delete all matches automatically |
| `--confirmed-only` | off | With `--auto`: only hash-verified matches |
| `--dry-run` | off | Preview without deleting |

## Matching strategies

### Timestamp matching (default, fast)

The attachment's `created_date` in `chat.db` is compared with the Photos
asset's creation date.  A `--tolerance` of 10 seconds covers any timezone
rounding that may occur when a photo is sent/received.

For shared photos (sent to you by someone else), the timestamp in Messages
is when you *received* the photo, which may differ from when it was taken.
If you find many false negatives, try increasing `--tolerance`.

### Hash matching (`--verify-hash`, slow but reliable)

Each candidate Photos asset is exported via AppleScript and its SHA-256 is
compared to the attachment file.  Only exact byte-for-byte matches are
confirmed.  This is the most reliable strategy but can be slow for large
libraries because it calls the Photos app for each candidate.

## Troubleshooting

**"Messages database not found"**
Confirm you are on macOS and that `~/Library/Messages/chat.db` exists.

**"Permission denied"**
Add your terminal to Full Disk Access (see above).

**"osascript failed"**
Photos is not accessible.  Make sure Photos app is installed and try running
`osascript -e 'tell application "Photos" to get name'` to verify.

**Photos library not found at default path**
If your library is in a non-standard location, temporarily symlink it:
```bash
ln -s "/Volumes/External/Photos Library.photoslibrary" \
      ~/Pictures/Photos\ Library.photoslibrary
```

## FAQ

**Will this affect my iCloud sync?**
Deleting the local file from `~/Library/Messages/Attachments/` removes it from
the Mac's local storage.  On the iPhone, the attachment may show as "tap to
download" if iCloud Messages is enabled and you delete the cloud copy too.
This tool only deletes the Mac-local file; it does not interact with iCloud.

**Can I undo a deletion?**
No — the files are permanently deleted, not moved to Trash.  Use `--dry-run`
and `--review` first until you are confident.

**What about attachments I received from others?**
If you saved them to Photos, they will be matched and offered for deletion.
If you didn't save them, they will appear in the "not matched" list and will
not be deleted.

## Architecture

```
attachment_cleaner/
├── __init__.py
├── messages_db.py    # Read-only SQLite access to chat.db
├── photos_bridge.py  # Query Photos via SQL or osascript; export for hashing
├── cleaner.py        # Matching logic and deletion
└── ui.py             # rich-based terminal UI
main.py               # CLI entry point
```
