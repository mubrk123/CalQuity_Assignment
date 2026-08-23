"""Which documented known issue applies to a record."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.data.store import Account, Order, Ticket
from app.sources import terms

RESOLVED_STATUSES = {"resolved", "closed", "fixed"}


@dataclass
class IssueMatch:
    issue_id: str
    status: str
    source_ref: str
    guidance: str
    why: str

    def to_dict(self) -> dict:
        return {
            "issue": self.issue_id,
            "status": self.status,
            "source": self.source_ref,
            "why_it_matches": self.why,
            "guidance": self.guidance,
        }


def _issues() -> dict[str, dict]:
    """Known issues eligible to match: unresolved, with an `applies_when`."""
    raw = terms.policy_defaults()["known_issues"]
    return {
        key: meta
        for key, meta in raw.items()
        if isinstance(meta, dict)
        and meta.get("applies_when")
        and str(meta.get("status", "")).lower() not in RESOLVED_STATUSES
    }


def _rows_mentioned(text: str) -> int | None:
    """Largest row count referenced in free text, e.g. '4,200-row CSV'."""
    counts = [
        int(m.replace(",", ""))
        for m in re.findall(r"\b([\d][\d,]{2,8})\s*(?:-?\s*row|rows)\b", text, re.I)
    ]
    return max(counts) if counts else None


def _symptom_hit(cond: dict, text: str) -> tuple[bool, str | None]:
    """Does the free text mention something this issue is about?"""
    wanted = cond.get("symptom_any")
    if not wanted:
        return True, None
    low = (text or "").lower()
    for phrase in wanted:
        if phrase.lower() in low:
            return True, phrase
    return False, None


def for_order(order: Order, now: datetime, symptom_text: str = "") -> list[IssueMatch]:
    """Known issues that apply to a specific shipment."""
    out: list[IssueMatch] = []
    for issue_id, meta in _issues().items():
        cond = meta["applies_when"]
        reasons: list[str] = []

        if "carrier" in cond:
            if cond["carrier"].lower() not in (order.carrier or "").lower():
                continue
            reasons.append(f"carrier is {order.carrier}")

        if "order_status" in cond:
            if (order.status or "").upper() != cond["order_status"].upper():
                continue
            reasons.append(f"status is {order.status}")

        if cond.get("no_pickup_confirmation"):
            if order.pickup_actual_at is not None:
                continue
            reasons.append("no pickup confirmation recorded")

        if cond.get("pickup_window_opened"):
            # Before the window opens there is no webhook-lag ambiguity yet.
            if not (order.pickup_window_start and now > order.pickup_window_start):
                continue
            reasons.append(
                f"pickup window opened at {order.pickup_window_start:%H:%M}"
            )

        # Row-count conditions are ticket-shaped, so skip those here.
        if cond.get("min_rows_mentioned") and not symptom_text:
            continue
        if not reasons:
            continue

        out.append(IssueMatch(
            issue_id=issue_id,
            status=meta.get("status", "unknown"),
            source_ref=meta["source_ref"],
            guidance=meta["quote"],
            why=", ".join(reasons),
        ))
    return out


def for_ticket(ticket: Ticket, account: Account | None) -> list[IssueMatch]:
    """Known issues that plausibly explain a ticket."""
    text = f"{ticket.subject} {ticket.description}"
    out: list[IssueMatch] = []

    for issue_id, meta in _issues().items():
        cond = meta["applies_when"]
        reasons: list[str] = []

        hit, phrase = _symptom_hit(cond, text)
        if not hit:
            continue
        if phrase:
            reasons.append(f"ticket mentions {phrase!r}")

        if cond.get("plans_any"):
            plan = (account.plan if account else None) or ""
            if plan not in cond["plans_any"]:
                continue
            reasons.append(f"{plan} plan is affected")

        if cond.get("min_rows_mentioned"):
            rows = _rows_mentioned(text)
            if rows is None or rows < int(cond["min_rows_mentioned"]):
                continue
            reasons.append(f"{rows:,} rows exceeds the ~{int(cond['min_rows_mentioned']):,} threshold")

        if cond.get("carrier"):
            if cond["carrier"].lower() not in text.lower():
                continue
            reasons.append(f"{cond['carrier']} referenced")

        if not reasons:
            continue

        out.append(IssueMatch(
            issue_id=issue_id,
            status=meta.get("status", "unknown"),
            source_ref=meta["source_ref"],
            guidance=meta["quote"],
            why=", ".join(reasons),
        ))
    return out


def for_text(text: str, account: Account | None = None) -> list[IssueMatch]:
    """Known issues plausibly explaining a free-text complaint."""
    class _T:  # minimal duck-type for for_ticket
        subject = text
        description = ""
    return for_ticket(_T(), account)  # type: ignore[arg-type]


def workaround_note(matches: list[IssueMatch]) -> str | None:
    """One line for a human deciding whether an action is still warranted."""
    if not matches:
        return None
    ids = ", ".join(m.issue_id for m in matches)
    return (
        f"{ids} may already explain this and has documented guidance — check it "
        "before treating this as a new incident."
    )
