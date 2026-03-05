#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13.0"]
# ///
"""
Messages Attachment Cleaner
===========================

Finds large attachments in your iMessage history that are already stored
in your Photos library and helps you delete them to free up disk space.

Usage
-----
    python main.py [OPTIONS]

Options
-------
  --min-size MB         Minimum attachment size to consider (default: 0.1 MB)
  --no-media-only       Include non-image/video attachments
  --tolerance SECS      Timestamp match window in seconds (default: 10)
  --verify-hash         Export each Photos asset and compare SHA-256 (slower,
                        more reliable)
  --auto                Delete all matched attachments without prompting
  --confirmed-only      With --auto: only delete hash-verified matches
  --dry-run             Show what would be deleted without actually deleting

Examples
--------
  # Show a summary of reclaimable space
  python main.py

  # Interactively decide for each matched attachment
  python main.py --review

  # Auto-delete everything matched by timestamp, confirm first
  python main.py --auto --dry-run

  # Auto-delete only hash-verified matches (safest)
  python main.py --auto --verify-hash --confirmed-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="attachment-cleaner",
        description="Free up disk space by removing Messages attachments already in Photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--min-size",
        type=float,
        default=0.1,
        metavar="MB",
        help="Minimum attachment size in MB to include (default: 0.1)",
    )
    p.add_argument(
        "--no-media-only",
        action="store_true",
        default=False,
        help="Include non-image/video attachments (PDFs, etc.)",
    )
    p.add_argument(
        "--tolerance",
        type=int,
        default=10,
        metavar="SECS",
        help="Seconds of tolerance for timestamp matching (default: 10)",
    )
    p.add_argument(
        "--verify-hash",
        action="store_true",
        default=False,
        help="Export Photos assets and compare SHA-256 (slower but more reliable)",
    )
    p.add_argument(
        "--review",
        action="store_true",
        default=False,
        help="Interactively review and decide for each matched attachment",
    )
    p.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Delete all matched attachments without prompting",
    )
    p.add_argument(
        "--confirmed-only",
        action="store_true",
        default=False,
        help="With --auto: only delete hash-verified matches (requires --verify-hash)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be deleted without actually deleting",
    )
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Lazy imports so the help text is fast
    from attachment_cleaner.ui import (
        auto_clean,
        interactive_review,
        print_summary,
        run_scan,
    )

    console.print(
        Panel.fit(
            "[bold cyan]Messages Attachment Cleaner[/bold cyan]\n"
            "[dim]Scanning your Messages library for attachments already in Photos…[/dim]",
            border_style="cyan",
        )
    )

    if args.dry_run:
        console.print("[bold yellow]DRY-RUN mode — nothing will be deleted.[/bold yellow]\n")

    # Validate flag combinations
    if args.confirmed_only and not args.verify_hash:
        console.print(
            "[yellow]Warning:[/yellow] --confirmed-only has no effect without --verify-hash. "
            "Adding --verify-hash automatically.\n"
        )
        args.verify_hash = True

    try:
        plan, cleaner = run_scan(
            min_bytes=int(args.min_size * 1_048_576),
            media_only=not args.no_media_only,
            timestamp_tolerance=args.tolerance,
            verify_hash=args.verify_hash,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    except PermissionError:
        console.print(
            "[red]Permission denied.[/red]\n"
            "Messages requires Full Disk Access.\n"
            "Go to: System Settings → Privacy & Security → Full Disk Access\n"
            "and add your terminal application (Terminal or iTerm2)."
        )
        return 1

    print_summary(plan)

    if not plan.matched:
        console.print("[green]Nothing to clean up — your Messages attachments are not in Photos.[/green]")
        return 0

    if args.auto:
        auto_clean(plan, cleaner, dry_run=args.dry_run, confirmed_only=args.confirmed_only)
    elif args.review:
        interactive_review(plan, cleaner, dry_run=args.dry_run)
    else:
        # Default: just show the summary and hint at next steps
        console.print(
            "Run with [bold]--review[/bold] to decide file-by-file, or "
            "[bold]--auto[/bold] to delete all matches.\n"
            "Add [bold]--dry-run[/bold] to either mode to preview without deleting."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
