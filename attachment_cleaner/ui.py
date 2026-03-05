"""
Terminal UI built with the `rich` library.

Provides three main interactions:

1. scan_and_report()  — Show a summary of what can be cleaned up.
2. interactive_review() — Step through each matched attachment and decide
                           whether to delete it.
3. auto_clean()       — Non-interactive: delete everything in the plan.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .cleaner import AttachmentCleaner, CleanupPlan, MatchResult
from .messages_db import Attachment, MessagesDB
from .photos_bridge import PhotosAsset

console = Console()


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_size(n_bytes: int) -> str:
    if n_bytes >= 1_073_741_824:
        return f"{n_bytes / 1_073_741_824:.1f} GB"
    if n_bytes >= 1_048_576:
        return f"{n_bytes / 1_048_576:.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{n_bytes} B"


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _attachment_row_text(att: Attachment, include_size: bool = True) -> Text:
    name = att.transfer_name or Path(att.filename).name if att.filename else "unknown"
    t = Text()
    t.append(name, style="bold cyan")
    t.append(f"  {fmt_date(att.created_date)}", style="dim")
    if include_size:
        t.append(f"  {fmt_size(att.total_bytes)}", style="green")
    return t


def _photos_asset_text(asset: PhotosAsset) -> Text:
    t = Text()
    t.append(asset.filename, style="bold yellow")
    t.append(f"  {fmt_date(asset.creation_date)}", style="dim")
    if asset.is_video:
        t.append("  [VIDEO]", style="magenta")
    if asset.favorite:
        t.append("  ★", style="red")
    return t


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(plan: CleanupPlan) -> None:
    console.print()
    console.rule("[bold]Scan Summary")

    # Stats panel
    stats = Table.grid(padding=(0, 2))
    stats.add_column(style="dim")
    stats.add_column(style="bold")
    stats.add_row("Attachments scanned:", str(plan.total_scanned))
    stats.add_row("Matched (in Photos):", f"[green]{len(plan.matched)}")
    stats.add_row("Not matched:", f"[yellow]{len(plan.unmatched)}")
    stats.add_row("File already missing:", str(len(plan.missing_file)))
    stats.add_row("Reclaimable space:", f"[bold green]{fmt_size(plan.reclaimable_bytes)}")
    console.print(Panel(stats, title="Results", expand=False))

    # Top 20 reclaimable
    if plan.matched:
        table = Table(title="Top reclaimable attachments", show_lines=False, expand=False)
        table.add_column("#", style="dim", width=4)
        table.add_column("Attachment", no_wrap=True)
        table.add_column("Date", style="dim")
        table.add_column("Size", justify="right", style="green")
        table.add_column("Match", style="yellow")
        table.add_column("Photos filename")

        sorted_matches = sorted(
            plan.matched, key=lambda r: r.attachment.total_bytes, reverse=True
        )[:20]

        for i, result in enumerate(sorted_matches, 1):
            att = result.attachment
            photos_name = (
                result.photos_assets[0].filename if result.photos_assets else "?"
            )
            match_label = "hash ✓" if result.is_confirmed else "timestamp ~"
            name = att.transfer_name or Path(att.filename).name if att.filename else "?"
            table.add_row(
                str(i),
                name,
                fmt_date(att.created_date),
                fmt_size(att.total_bytes),
                match_label,
                photos_name,
            )
        console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def interactive_review(
    plan: CleanupPlan,
    cleaner: AttachmentCleaner,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Walk through each matched attachment and ask the user what to do.
    Returns (deleted_count, deleted_bytes).
    """
    if not plan.matched:
        console.print("[yellow]No matched attachments to review.[/yellow]")
        return 0, 0

    sorted_matches = sorted(
        plan.matched, key=lambda r: r.attachment.total_bytes, reverse=True
    )

    deleted_count = 0
    deleted_bytes = 0
    total = len(sorted_matches)

    console.print(
        f"\n[bold]Interactive review[/bold] — {total} attachments matched in Photos.\n"
        "For each one, press [bold]y[/bold] to delete, "
        "[bold]n[/bold] to keep, or [bold]q[/bold] to quit.\n"
    )

    for idx, result in enumerate(sorted_matches):
        att = result.attachment
        name = att.transfer_name or Path(att.filename).name if att.filename else "?"

        console.rule(f"[{idx+1}/{total}]  {name}")
        console.print(_attachment_row_text(att))
        console.print(
            f"  Path: [dim]{att.filename or 'N/A'}[/dim]"
        )
        console.print(
            f"  Match method: [yellow]{'hash ✓' if result.is_confirmed else 'timestamp ~'}[/yellow]"
        )

        if result.photos_assets:
            console.print("  Photos matches:")
            for asset in result.photos_assets:
                console.print(f"    • {_photos_asset_text(asset)}")

        console.print()
        choice = Prompt.ask(
            "  Delete this attachment file?",
            choices=["y", "n", "q"],
            default="n",
        )

        if choice == "q":
            console.print("[dim]Stopping review.[/dim]")
            break
        elif choice == "y":
            if dry_run:
                console.print(f"  [dim][dry-run] Would delete {fmt_size(att.total_bytes)}[/dim]")
                deleted_count += 1
                deleted_bytes += att.total_bytes
            else:
                if cleaner.delete_attachment_file(att):
                    console.print(f"  [green]Deleted.[/green] Freed {fmt_size(att.total_bytes)}.")
                    deleted_count += 1
                    deleted_bytes += att.total_bytes
                else:
                    console.print("  [red]Delete failed.[/red] File may already be gone.")
        else:
            console.print("  [dim]Skipped.[/dim]")

    console.print()
    console.rule()
    console.print(
        f"[bold green]Session complete.[/bold green]  "
        f"Deleted {deleted_count} file(s) · freed {fmt_size(deleted_bytes)}."
        + (" [dim](dry-run)[/dim]" if dry_run else "")
    )
    return deleted_count, deleted_bytes


# ---------------------------------------------------------------------------
# Auto-clean (non-interactive)
# ---------------------------------------------------------------------------

def auto_clean(
    plan: CleanupPlan,
    cleaner: AttachmentCleaner,
    dry_run: bool = False,
    confirmed_only: bool = False,
) -> tuple[int, int]:
    """Delete all matched attachments without prompting."""
    if not plan.matched:
        console.print("[yellow]Nothing to clean.[/yellow]")
        return 0, 0

    mode_label = "hash-verified matches only" if confirmed_only else "all timestamp matches"
    if dry_run:
        console.print(
            f"[dim][dry-run] Would delete {len(plan.matched)} file(s) "
            f"({mode_label}) — {fmt_size(plan.reclaimable_bytes)} total.[/dim]"
        )
        return len(plan.matched), plan.reclaimable_bytes

    with make_progress() as progress:
        task = progress.add_task("Deleting…", total=len(plan.matched))
        deleted_count = 0
        deleted_bytes = 0
        for result in plan.matched:
            if confirmed_only and not result.is_confirmed:
                progress.advance(task)
                continue
            att = result.attachment
            if cleaner.delete_attachment_file(att):
                deleted_count += 1
                deleted_bytes += att.total_bytes
            progress.advance(task)

    console.print(
        f"[bold green]Done.[/bold green]  "
        f"Deleted {deleted_count} file(s) · freed {fmt_size(deleted_bytes)}."
    )
    return deleted_count, deleted_bytes


# ---------------------------------------------------------------------------
# Top-level scan runner (used by main.py)
# ---------------------------------------------------------------------------

def run_scan(
    min_bytes: int,
    media_only: bool,
    timestamp_tolerance: int,
    verify_hash: bool,
) -> tuple[CleanupPlan, AttachmentCleaner]:
    """Run the scan with a live progress bar and return the plan."""
    cleaner = AttachmentCleaner(
        min_bytes=min_bytes,
        media_only=media_only,
        timestamp_tolerance=timestamp_tolerance,
        verify_hash=verify_hash,
    )

    with MessagesDB() as db:
        total_att = db.attachment_count()
        total_size = db.total_attachment_size()
        console.print(
            f"Messages DB: [bold]{total_att}[/bold] total attachments, "
            f"[bold]{fmt_size(total_size)}[/bold] on disk.\n"
        )

        with make_progress() as progress:
            task = progress.add_task("Scanning attachments…", total=None)
            scanned = [0]

            def update_progress(cur: int, tot: int, name: str) -> None:
                progress.update(task, total=tot, completed=cur, description=f"[cyan]{name[:40]}")
                scanned[0] = cur

            cleaner.progress_cb = update_progress
            plan = cleaner.scan(db)

    return plan, cleaner
