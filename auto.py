#!/usr/bin/env python3
"""BillBot automation — fetch bills from Gmail, parse, post to Splitwise."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from billbot import (
    load_env_file,
    load_tenants,
    parse_pdf,
    setup_logging,
    compute_tenant_shares,
)
from db import init_db, save_parsed, mark_posted, get_latest_date, get_unposted, get_all
from gmail_fetch import fetch_new_bills, send_notification_email

LOGGER = logging.getLogger("billbot.auto")

BILLBOT_DIR = Path.home() / ".billbot"
DEFAULT_TENANTS = Path(__file__).parent / "tenants.json"

# Manual PG&E bill period overrides for pro-rating.
# PG&E emails don't include billing period, so specify manually for any
# bill that partially overlaps a tenant's move-in/move-out date.
# Example: 340.59: ("01/05/2026", "02/03/2026")
PGE_PERIOD_OVERRIDES: dict[float, tuple[str, str]] = {}

PROVIDER_NAMES = {"pge": "PG&E", "city-service": "City Services"}


def _notify(message: str) -> None:
    """Send a macOS notification so you know cron ran."""
    try:
        safe = message.replace('"', '\\"')
        script = 'display notification "' + safe + '" with title "BillBot"'
        subprocess.run(["osascript", "-e", script],
                       check=False, capture_output=True, timeout=5)
    except Exception:
        pass


def _build_description(provider: str, period_start: str | None,
                       period_end: str | None, email_date: str | None = None) -> str:
    """Build a Splitwise-friendly description like 'PG&E Bill Mar 2026 (01/05 - 02/03)'."""
    provider_name = PROVIDER_NAMES.get(provider, provider)
    month_label = ""
    # Prefer bill period end date for the month label
    if period_start and period_end:
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                month_label = datetime.strptime(period_end, fmt).strftime("%b %Y")
                break
            except ValueError:
                continue
    # Fall back to email received date
    if not month_label and email_date:
        try:
            from email.utils import parsedate_to_datetime
            month_label = parsedate_to_datetime(email_date).strftime("%b %Y")
        except Exception:
            pass
    if period_start and period_end:
        return f"{provider_name} Bill {month_label} ({period_start} - {period_end})"
    if month_label:
        return f"{provider_name} Bill {month_label}"
    return f"{provider_name} Bill"


def _print_summary(bills: list[dict], show_splitwise: bool = False) -> None:
    """Print a human-readable summary of processed bills."""
    if not bills:
        print("\n  No new bills found.")
        return

    print("\n=== BillBot Summary ===\n")
    for i, bill in enumerate(bills, 1):
        name = PROVIDER_NAMES.get(bill["provider"], bill["provider"])
        ps, pe = bill.get("bill_period_start"), bill.get("bill_period_end")
        period = f"  |  {ps} - {pe}" if ps and pe else ""
        print(f"  #{i}  {name}  |  ${bill['amount_due']:.2f}{period}")

        for share in bill.get("tenant_shares", []):
            if not share.get("included"):
                if share.get("reason") != "inactive":
                    print(f"      {share['name']}:  EXCLUDED ({share['reason']})")
                continue
            calc = f"${bill['amount_due']:.2f} x {share['share_percent']}%"
            if share.get("prorate_factor") is not None:
                calc += f" x {share['prorate_factor']:.4f}"
            calc += f" = ${share['amount']:.2f}"
            if share.get("prorate_detail"):
                calc += f"  [PRORATED: {share['prorate_detail']}]"
            print(f"      {share['name']}:  {calc}")
        print()

    if show_splitwise:
        _print_splitwise_preview(bills)


def _print_splitwise_preview(bills: list[dict]) -> None:
    """Show exactly what Splitwise API calls would be made (1-on-1 expenses)."""
    print("=== Splitwise API Preview (1-on-1) ===\n")
    expense_num = 0
    for bill in bills:
        description = _build_description(
            bill["provider"], bill.get("bill_period_start"),
            bill.get("bill_period_end"), bill.get("email_date"),
        )
        for s in bill.get("tenant_shares", []):
            if not s.get("included"):
                continue
            expense_num += 1
            print(f"  Expense #{expense_num}:")
            print(f"    POST /api/v3.0/create_expense")
            print(f"    description: {description}")
            print(f"    cost:        {s['amount']:.2f}")
            print(f"    currency:    USD")
            print(f"    [You]   paid_share={s['amount']:.2f}  owed_share=0.00")
            print(f"    {s['name']} ({s.get('email', '?')})  paid_share=0.00  owed_share={s['amount']:.2f}")
            print()


def run_pipeline(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="BillBot automation pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse only, don't post to Splitwise")
    parser.add_argument("--backfill", action="store_true", help="Process all historical bills, don't post")
    parser.add_argument("--since-days", type=int, default=60, help="How far back to search Gmail (default: 60)")
    parser.add_argument("--tenants-file", default=str(DEFAULT_TENANTS), help="Path to tenants JSON")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    load_env_file(BILLBOT_DIR / ".env")

    # Init
    conn = init_db()
    tenants = load_tenants(Path(args.tenants_file).expanduser().resolve())

    # Determine which message IDs are already processed
    processed_ids: set[str] = set()
    for row in get_all(conn):
        processed_ids.add(row["email_message_id"])

    # Determine search start date
    since_date = None
    if not args.backfill:
        latest = get_latest_date(conn)
        if latest:
            since_date = datetime.fromisoformat(latest)

    # Fetch
    LOGGER.info("Fetching bills from Gmail...")
    fetched = fetch_new_bills(
        processed_message_ids=processed_ids,
        since_date=since_date,
        since_days=args.since_days,
    )

    if not fetched:
        print("No new bills found.")
        _notify("No new bills found")
        # No email for "no new bills" — avoid spamming inbox
        return 0

    # Parse and save
    new_bills: list[dict] = []
    for bill in fetched:
        LOGGER.info("Processing %s bill: %s", bill.provider, bill.email_subject)

        if bill.provider == "pge":
            # PG&E — amount from email body, no PDF
            if bill.amount_due is None:
                LOGGER.warning("Skipping PG&E bill with no amount: %s", bill.email_subject)
                continue

            # Check for manual period override (for bills needing pro-rating)
            period_start = bill.bill_period_start
            period_end = bill.bill_period_end
            if bill.amount_due in PGE_PERIOD_OVERRIDES:
                period_start, period_end = PGE_PERIOD_OVERRIDES[bill.amount_due]
                LOGGER.info("Using manual period override for PG&E $%.2f: %s - %s",
                            bill.amount_due, period_start, period_end)

            shares = compute_tenant_shares(
                amount_due=bill.amount_due,
                tenants=tenants,
                bill_period_start=period_start,
                bill_period_end=period_end,
            )
            shares_dicts = [asdict(s) for s in shares]

            bill_record = {
                "provider": bill.provider,
                "amount_due": bill.amount_due,
                "bill_period_start": period_start,
                "bill_period_end": period_end,
                "tenant_shares": shares_dicts,
                "email_message_id": bill.email_message_id,
                "email_date": bill.email_date,
            }

            save_parsed(
                conn,
                email_message_id=bill.email_message_id,
                provider=bill.provider,
                amount_due=bill.amount_due,
                bill_period_start=period_start,
                bill_period_end=period_end,
                pdf_path=None,
                tenant_shares=shares_dicts,
                parse_result=bill_record,
            )
            new_bills.append(bill_record)

        else:
            # City — parse PDF
            if bill.pdf_path is None:
                LOGGER.warning("Skipping city bill with no PDF: %s", bill.email_subject)
                continue

            try:
                result = parse_pdf(bill.pdf_path, tenants)
            except ValueError as exc:
                LOGGER.error("Failed to parse %s: %s", bill.pdf_path, exc)
                continue

            result_dict = asdict(result)
            shares_dicts = result_dict["tenant_shares"]

            bill_record = {
                "provider": result.provider or bill.provider,
                "amount_due": result.detected_amount_due,
                "bill_period_start": result.bill_period_start,
                "bill_period_end": result.bill_period_end,
                "tenant_shares": shares_dicts,
                "email_message_id": bill.email_message_id,
                "email_date": bill.email_date,
            }

            save_parsed(
                conn,
                email_message_id=bill.email_message_id,
                provider=result.provider or bill.provider,
                amount_due=result.detected_amount_due,
                bill_period_start=result.bill_period_start,
                bill_period_end=result.bill_period_end,
                pdf_path=str(bill.pdf_path),
                tenant_shares=shares_dicts,
                parse_result=result_dict,
            )
            new_bills.append(bill_record)

    _print_summary(new_bills, show_splitwise=(args.dry_run or args.backfill))

    # Dry run or backfill — stop before posting
    if args.dry_run or args.backfill:
        mode = "dry-run" if args.dry_run else "backfill"
        print(f"[{mode}] {len(new_bills)} bill(s) saved to database. No Splitwise posting.")
        return 0

    # Post to Splitwise
    from splitwise_post import post_bill

    # Retry any previously failed posts (fetch succeeded but Splitwise failed)
    unposted = get_unposted(conn)
    # Filter to only bills with included tenants (skip EXCLUDED-only bills)
    retry_bills = []
    for row in unposted:
        shares = json.loads(row["tenant_shares_json"])
        has_included = any(s.get("included") for s in shares)
        if has_included:
            retry_bills.append({
                "provider": row["provider"],
                "amount_due": row["amount_due"],
                "bill_period_start": row["bill_period_start"],
                "bill_period_end": row["bill_period_end"],
                "tenant_shares": shares,
                "email_message_id": row["email_message_id"],
                "email_date": json.loads(row["parse_result_json"]).get("email_date"),
            })

    if retry_bills:
        LOGGER.info("Retrying %d previously unposted bill(s)...", len(retry_bills))

    posted = 0
    for bill in retry_bills:
        try:
            expense_id = post_bill(
                provider=bill["provider"],
                amount_due=bill["amount_due"],
                bill_period_start=bill.get("bill_period_start"),
                bill_period_end=bill.get("bill_period_end"),
                tenant_shares=bill["tenant_shares"],
                email_date=bill.get("email_date"),
            )
            mark_posted(conn, bill["email_message_id"], expense_id)
            posted += 1
            print(f"  Posted to Splitwise: expense {expense_id}")
        except Exception as exc:
            LOGGER.error("Failed to post to Splitwise: %s", exc)

    print(f"\nDone. {posted} bill(s) posted to Splitwise.")
    _notify(f"BillBot: posted {posted} bill(s) to Splitwise")

    # Email summary to yourself
    if posted > 0:
        lines = [f"BillBot posted {posted} expense(s) to Splitwise:\n"]
        for bill in retry_bills:
            for s in bill.get("tenant_shares", []):
                if s.get("included"):
                    desc = _build_description(
                        bill["provider"], bill.get("bill_period_start"),
                        bill.get("bill_period_end"), bill.get("email_date"),
                    )
                    lines.append(f"  - {desc}: {s['name']} owes ${s['amount']:.2f}")
        try:
            send_notification_email(
                subject=f"BillBot: {posted} bill(s) posted to Splitwise",
                body="\n".join(lines),
            )
        except Exception as exc:
            LOGGER.error("Failed to send notification email: %s", exc)
    conn.close()
    return 0


def main() -> None:
    raise SystemExit(run_pipeline(sys.argv[1:]))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGGER.error("%s", exc)
        _notify(f"BillBot ERROR: {exc}")
        try:
            send_notification_email("BillBot ERROR", str(exc))
        except Exception:
            pass
        raise SystemExit(1)
