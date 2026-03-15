"""
Terminal UI built with the `rich` library.

Provides four main interactions:

1. scan_and_report()   — Show a summary of what can be cleaned up.
2. interactive_review() — Step through each matched attachment and decide
                           whether to delete it.
3. auto_clean()        — Non-interactive: delete everything in the plan.
4. browse_attachments() — Show every attachment with full metadata so you
                           can manually verify whether it is already in Photos.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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

from .cleaner import (
    AttachmentCleaner,
    CleanupPlan,
    MatchResult,
    load_cached_plan,
    save_cached_plan,
)
from .messages_db import Attachment, MessagesDB
from .photos_bridge import PhotosAsset, get_file_content_date

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


def _fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


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
        table.add_column("Size", justify="right", style="bold green")
        table.add_column("Contact", style="cyan", no_wrap=True)
        table.add_column("Date", style="dim")
        table.add_column("Match", style="yellow")
        table.add_column("Photos")

        sorted_matches = sorted(
            plan.matched, key=lambda r: r.attachment.total_bytes, reverse=True
        )[:20]

        for i, result in enumerate(sorted_matches, 1):
            att = result.attachment
            asset = result.photos_assets[0] if result.photos_assets else None
            if asset:
                photos_cell = f"[link=photos://asset?id={asset.local_id}]{asset.filename}[/link]"
            else:
                photos_cell = "?"
            match_label = _match_label(result.match_method)
            contact = att.chat_identifier or "Unknown"
            table.add_row(
                str(i),
                fmt_size(att.total_bytes),
                contact,
                fmt_date(att.created_date),
                match_label,
                photos_cell,
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

        console.rule(f"[{idx+1}/{total}]  {fmt_size(att.total_bytes)}  {fmt_date(att.created_date)}")
        console.print(f"  Contact: [cyan]{att.chat_identifier or 'Unknown'}[/cyan]")
        console.print(
            f"  Match method: [yellow]{_match_label(result.match_method)}[/yellow]"
        )

        if result.photos_assets:
            console.print("  Photos matches:")
            for asset in result.photos_assets:
                link = f"[link=photos://asset?id={asset.local_id}]{asset.filename}[/link]"
                t = Text.from_markup(f"    • {link}")
                t.append(f"  {fmt_date(asset.creation_date)}", style="dim")
                if asset.is_video:
                    t.append("  [VIDEO]", style="magenta")
                if asset.favorite:
                    t.append("  ★", style="red")
                console.print(t)

        console.print()
        choice = Prompt.ask(
            "  Delete this attachment?",
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
                success, method = cleaner.delete_attachment(att)
                if success and method == "message":
                    console.print(
                        f"  [green]Deleted from Messages[/green] — will sync to iPhone. "
                        f"Freed {fmt_size(att.total_bytes)}."
                    )
                    deleted_count += 1
                    deleted_bytes += att.total_bytes
                elif success and method == "file":
                    console.print(
                        f"  [green]Deleted file[/green] — [yellow]local only, won't sync[/yellow]. "
                        f"Freed {fmt_size(att.total_bytes)}."
                    )
                    deleted_count += 1
                    deleted_bytes += att.total_bytes
                else:
                    console.print("  [red]Delete failed.[/red]")
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
            success, _ = cleaner.delete_attachment(att)
            if success:
                deleted_count += 1
                deleted_bytes += att.total_bytes
            progress.advance(task)

    console.print(
        f"[bold green]Done.[/bold green]  "
        f"Deleted {deleted_count} file(s) · freed {fmt_size(deleted_bytes)}."
    )
    return deleted_count, deleted_bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_label(method: str) -> str:
    return {
        "hash":               "hash ✓",
        "filename":           "filename ✓",
        "metadata_timestamp": "exif ts ~",
        "timestamp":          "msg ts ~",
        "none":               "none",
    }.get(method, method)


# ---------------------------------------------------------------------------
# Browse mode
# ---------------------------------------------------------------------------

_BROWSE_PAGE = 20


def browse_attachments(
    min_bytes: int = 0,
    media_only: bool = False,
    page_size: int = _BROWSE_PAGE,
) -> None:
    """
    Display all attachments ordered by size descending with full metadata,
    including the EXIF content date from the file alongside the Messages
    received date.  Useful for manually verifying whether files are in Photos.
    """
    with MessagesDB() as db:
        attachments = sorted(
            db.iter_attachments(min_bytes=min_bytes, media_only=media_only),
            key=lambda a: a.total_bytes,
            reverse=True,
        )

    if not attachments:
        console.print("[yellow]No attachments found.[/yellow]")
        return

    console.print(
        f"\n[bold]Browse mode[/bold] — {len(attachments)} attachments, largest first.\n"
        "[dim]EXIF date = kMDItemContentCreationDate from Spotlight (actual capture time).[/dim]\n"
    )

    offset = 0
    while offset < len(attachments):
        page = attachments[offset : offset + page_size]

        table = Table(
            show_lines=True,
            expand=False,
            title=f"Attachments {offset + 1}–{offset + len(page)} of {len(attachments)}",
        )
        table.add_column("#", style="dim", width=5, justify="right")
        table.add_column("Filename", style="bold cyan", no_wrap=True)
        table.add_column("Size", justify="right", style="bold green")
        table.add_column("Type", style="dim", no_wrap=True)
        table.add_column("Received", style="dim", no_wrap=True)
        table.add_column("EXIF date", no_wrap=True)
        table.add_column("Contact", style="cyan", no_wrap=True)
        table.add_column("On disk", justify="center")

        for i, att in enumerate(page, offset + 1):
            name = att.transfer_name or (Path(att.filename).name if att.filename else "?")
            mime = att.mime_type or (
                Path(att.transfer_name).suffix.lstrip(".").upper()
                if att.transfer_name else "?"
            )
            resolved = att.resolved_path
            on_disk = "[green]✓[/green]" if resolved else "[red]✗[/red]"

            # Fetch EXIF date via Spotlight (fast for cached files)
            if resolved:
                content_dt = get_file_content_date(resolved)
                if content_dt is not None:
                    exif_cell = Text(fmt_date(content_dt))
                    # Flag when received date differs by more than 1 day from EXIF
                    delta = abs((att.created_date - content_dt).total_seconds())
                    if delta > 86400:
                        exif_cell.stylize("yellow")
                else:
                    exif_cell = Text("—", style="dim")
            else:
                exif_cell = Text("—", style="dim")

            table.add_row(
                str(i),
                name,
                fmt_size(att.total_bytes),
                mime,
                fmt_date(att.created_date),
                exif_cell,
                att.chat_identifier or "Unknown",
                on_disk,
            )

        console.print(table)
        offset += page_size

        if offset < len(attachments):
            choice = Prompt.ask(
                "\n[dim]Press Enter for next page[/dim]",
                choices=["", "q"],
                default="",
                show_choices=False,
                show_default=False,
            )
            if choice == "q":
                break
            console.print()


# ---------------------------------------------------------------------------
# Top-level scan runner (used by main.py)
# ---------------------------------------------------------------------------

def run_scan(
    min_bytes: int,
    media_only: bool,
    timestamp_tolerance: int,
    verify_hash: bool,
    refresh: bool = False,
) -> tuple[CleanupPlan, AttachmentCleaner]:
    """Run the scan with a live progress bar and return the plan."""
    cleaner = AttachmentCleaner(
        min_bytes=min_bytes,
        media_only=media_only,
        timestamp_tolerance=timestamp_tolerance,
        verify_hash=verify_hash,
    )
    scan_params = {
        "min_bytes": min_bytes,
        "media_only": media_only,
        "tolerance": timestamp_tolerance,
        "verify_hash": verify_hash,
    }

    with MessagesDB() as db:
        total_att = db.attachment_count()
        total_size = db.total_attachment_size()
        console.print(
            f"Messages DB: [bold]{total_att}[/bold] total attachments, "
            f"[bold]{fmt_size(total_size)}[/bold] on disk.\n"
        )

        db_mtime = db.db_mtime()

        if not refresh:
            cached = load_cached_plan(db_mtime, scan_params)
            if cached is not None:
                plan, cached_at_iso = cached
                cached_dt = datetime.fromisoformat(cached_at_iso)
                age = datetime.now(tz=cached_dt.tzinfo) - cached_dt
                console.print(
                    f"[dim]Loaded cached results from {_fmt_age(age)}. "
                    f"Use --refresh to re-scan.[/dim]\n"
                )
                return plan, cleaner

        with make_progress() as progress:
            task = progress.add_task("Scanning attachments…", total=None)
            scanned = [0]

            def update_progress(cur: int, tot: int, name: str) -> None:
                progress.update(task, total=tot, completed=cur, description=f"[cyan]{name[:40]}")
                scanned[0] = cur

            cleaner.progress_cb = update_progress
            plan = cleaner.scan(db)

    save_cached_plan(plan, db_mtime, scan_params)
    return plan, cleaner
