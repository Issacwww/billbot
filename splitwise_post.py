"""Post bill expenses to Splitwise as 1-on-1 expenses (no group needed)."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

LOGGER = logging.getLogger("billbot.splitwise")

PROVIDER_NAMES = {"pge": "PG&E", "city-service": "City Services"}


def _get_client():
    """Create Splitwise client using API key auth."""
    from splitwise import Splitwise

    consumer_key = os.environ.get("SPLITWISE_CONSUMER_KEY", "")
    consumer_secret = os.environ.get("SPLITWISE_CONSUMER_SECRET", "")
    api_key = os.environ.get("SPLITWISE_API_KEY", "")

    if not all([consumer_key, consumer_secret, api_key]):
        raise ValueError(
            "Missing Splitwise credentials. Set SPLITWISE_CONSUMER_KEY, "
            "SPLITWISE_CONSUMER_SECRET, and SPLITWISE_API_KEY in ~/.billbot/.env"
        )

    return Splitwise(consumer_key, consumer_secret, api_key=api_key)


def _get_friends_by_email(client) -> dict[str, int]:
    """Return {lowercase_email: splitwise_user_id} from friends list."""
    return {
        f.getEmail().lower(): f.getId()
        for f in client.getFriends()
        if f.getEmail()
    }


def _build_description(provider: str, period_start: str | None,
                        period_end: str | None, email_date: str | None) -> str:
    """Build description like 'PG&E Bill Mar 2026 (01/05/2026 - 02/03/2026)'."""
    name = PROVIDER_NAMES.get(provider, provider)
    month_label = ""
    if period_start and period_end:
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                month_label = datetime.strptime(period_end, fmt).strftime("%b %Y")
                break
            except ValueError:
                continue
    if not month_label and email_date:
        try:
            from email.utils import parsedate_to_datetime
            month_label = parsedate_to_datetime(email_date).strftime("%b %Y")
        except Exception:
            pass
    if period_start and period_end:
        return f"{name} Bill {month_label} ({period_start} - {period_end})"
    if month_label:
        return f"{name} Bill {month_label}"
    return f"{name} Bill"


def post_bill(
    provider: str,
    amount_due: float,
    bill_period_start: Optional[str],
    bill_period_end: Optional[str],
    tenant_shares: list[dict],
    currency_code: str = "USD",
    email_date: Optional[str] = None,
) -> str:
    """Create a 1-on-1 Splitwise expense for each included tenant.

    Returns comma-separated Splitwise expense IDs.
    """
    from splitwise import Expense
    from splitwise.user import ExpenseUser

    client = _get_client()
    current_user_id = client.getCurrentUser().getId()
    friends = _get_friends_by_email(client)
    description = _build_description(provider, bill_period_start, bill_period_end, email_date)

    expense_ids: list[str] = []

    for tenant in tenant_shares:
        if not tenant.get("included") or tenant["amount"] <= 0:
            continue

        email = (tenant.get("email") or "").lower()
        if not email:
            LOGGER.warning("Tenant %s has no email, skipping", tenant["name"])
            continue

        friend_id = friends.get(email)
        if friend_id is None:
            raise ValueError(
                f"Tenant {tenant['name']} ({email}) not found in Splitwise friends. "
                f"Your friends: {list(friends.keys())}"
            )

        owed = round(tenant["amount"], 2)

        payer = ExpenseUser()
        payer.setId(current_user_id)
        payer.setPaidShare(str(owed))
        payer.setOwedShare("0.00")

        debtor = ExpenseUser()
        debtor.setId(friend_id)
        debtor.setPaidShare("0.00")
        debtor.setOwedShare(str(owed))

        expense = Expense()
        expense.setCost(str(owed))
        expense.setDescription(description)
        expense.setCurrencyCode(currency_code)
        expense.setUsers([payer, debtor])

        created, errors = client.createExpense(expense)
        if errors:
            raise RuntimeError(f"Splitwise failed for {tenant['name']}: {errors}")

        eid = str(created.getId())
        expense_ids.append(eid)
        LOGGER.info("Created expense %s: %s owes $%.2f — %s", eid, tenant["name"], owed, description)

    return ",".join(expense_ids)
