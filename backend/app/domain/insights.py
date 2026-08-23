"""Rule-based proactive issue detection over the supplied records."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.data.store import Store
from app.domain import credit, known_issues, precedence, sla
from app.sources import terms

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Insight:
    signal: str
    level: str  # critical | high | medium | low
    title: str
    detail: str
    records: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    action_kind: str | None = None
    action_target: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "records": self.records,
            "accounts": self.accounts,
            "recommended_action": self.recommended_action,
            "action_kind": self.action_kind,
            "action_target": self.action_target,
            "evidence": self.evidence,
            "metrics": self.metrics,
        }


def _name(store: Store, account_id: str) -> str:
    acc = store.accounts.get(account_id)
    return acc.account_name if acc else account_id


# SLA and P1
def _sla_signals(store: Store, now: datetime) -> list[Insight]:
    out: list[Insight] = []
    for t in store.tickets.values():
        if not t.is_open:
            continue
        a = sla.assess_ticket(t, store.accounts.get(t.account_id), now)
        sev = a.severity.severity
        target = a.decision.detail["target"]

        if a.evaluation.breached:
            over = a.evaluation.now - a.evaluation.deadline
            mins = int(over.total_seconds() // 60)
            out.append(Insight(
                signal="sla_breach",
                level="critical" if sev == "P1" else "high",
                title=f"{t.ticket_id} past its first-response target by {mins} min",
                detail=(
                    f"{_name(store, t.account_id)} - {sev}, target {target}, due "
                    f"{a.evaluation.deadline:%H:%M}. No first response is recorded."
                ),
                records=[t.ticket_id],
                accounts=[t.account_id],
                recommended_action=f"Escalate {t.ticket_id} and respond now",
                action_kind="escalation",
                action_target={"ticket_id": t.ticket_id},
                evidence=[c.source_ref for c in a.decision.citations],
                metrics={"severity": sev, "target": target, "minutes_over": mins},
            ))
        elif sev == "P1":
            out.append(Insight(
                signal="open_p1",
                level="critical",
                title=f"{t.ticket_id} is an open P1",
                detail=(
                    f"{_name(store, t.account_id)} - {t.subject}. Target {target}, "
                    f"due {a.evaluation.deadline:%H:%M}."
                ),
                records=[t.ticket_id],
                accounts=[t.account_id],
                recommended_action=f"Escalate {t.ticket_id} immediately",
                action_kind="escalation",
                action_target={"ticket_id": t.ticket_id},
                evidence=["Support Policy v3 s4"],
                metrics={"severity": sev, "target": target},
            ))
        elif a.evaluation.clock_started:
            remaining = (a.evaluation.deadline - now).total_seconds() / 60
            if 0 < remaining <= 60:
                out.append(Insight(
                    signal="sla_at_risk",
                    level="medium",
                    title=f"{t.ticket_id} due in {int(remaining)} min",
                    detail=f"{_name(store, t.account_id)} - {sev}, target {target}.",
                    records=[t.ticket_id],
                    accounts=[t.account_id],
                    metrics={"minutes_remaining": int(remaining)},
                ))
    return out


def _known_issue_signals(store: Store, now: datetime) -> list[Insight]:
    """Cluster open tickets by the known issue that explains them."""
    grouped: dict[str, list] = {}
    detail: dict[str, object] = {}
    for ticket in store.tickets.values():
        for match in known_issues.for_ticket(ticket, store.accounts.get(ticket.account_id)):
            grouped.setdefault(match.issue_id, []).append(ticket)
            detail.setdefault(match.issue_id, match)

    out: list[Insight] = []
    for issue_id, tickets in grouped.items():
        open_ones = [t for t in tickets if t.is_open]
        if not open_ones:
            continue
        match = detail[issue_id]
        accounts = sorted({t.account_id for t in tickets})
        multi = len(accounts) > 1
        out.append(Insight(
            signal="known_issue_cluster",
            level="high" if multi or len(open_ones) > 1 else "medium",
            title=(
                f"{len(open_ones)} open ticket{'s' if len(open_ones) != 1 else ''} "
                f"tracing to {issue_id}"
                + (f", across {len(accounts)} accounts" if multi else "")
            ),
            detail=f"{issue_id} is {match.status}. " + match.guidance.split(". ")[0] + ".",
            records=[t.ticket_id for t in tickets],
            accounts=accounts,
            recommended_action=(
                f"Confirm {issue_id} is the cause, reply with the documented workaround, "
                "and link these tickets to the known issue"
            ),
            action_kind="followup_task",
            evidence=[match.source_ref],
            metrics={"open": len(open_ones), "total": len(tickets),
                     "accounts": len(accounts), "known_issue": issue_id},
        ))
    return out


# Repeat contact
_STOP = {"the", "a", "for", "is", "of", "to", "and", "in", "on", "we", "our",
         "how", "do", "does", "with", "after", "still", "all"}


def _topic_key(text: str) -> frozenset[str]:
    words = {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP}
    return frozenset(words)


def _repeat_contact_signals(store: Store, now: datetime) -> list[Insight]:
    out: list[Insight] = []
    by_account: dict[str, list] = {}
    for t in store.tickets.values():
        by_account.setdefault(t.account_id, []).append(t)

    for account_id, tickets in by_account.items():
        for i, a in enumerate(tickets):
            for b in tickets[i + 1:]:
                if not (a.is_open or b.is_open):
                    continue
                overlap = _topic_key(a.subject) & _topic_key(b.subject)
                if len(overlap) < 2:
                    continue
                out.append(Insight(
                    signal="repeat_contact",
                    level="medium",
                    title=f"{_name(store, account_id)} has raised this topic before",
                    detail=(
                        f"{a.ticket_id} ({a.status}) and {b.ticket_id} ({b.status}) "
                        f"share the topic: {', '.join(sorted(overlap))}."
                    ),
                    records=[a.ticket_id, b.ticket_id],
                    accounts=[account_id],
                    recommended_action=(
                        "Check whether the earlier ticket was resolved correctly before "
                        "answering again"
                    ),
                    metrics={"shared_terms": sorted(overlap)},
                ))
    return out


# Operational signals nobody has raised
def _order_signals(store: Store, now: datetime) -> list[Insight]:
    out: list[Insight] = []
    for o in store.orders.values():
        if o.status != "BOOKED" or o.pickup_actual_at or not o.pickup_window_end:
            continue
        overdue_h = (now - o.pickup_window_end).total_seconds() / 3600
        if overdue_h <= 2:
            continue

        related = [
            t for t in store.tickets.values()
            if t.account_id == o.account_id and t.is_open
            and (o.order_id in f"{t.subject} {t.description}"
                 or re.search(r"pickup|collect", f"{t.subject} {t.description}", re.I))
        ]
        out.append(Insight(
            signal="stale_pickup",
            level="high" if overdue_h > 4 else "medium",
            title=f"{o.order_id} pickup overdue by {overdue_h:.1f}h with no ticket raised"
                  if not related else
                  f"{o.order_id} pickup overdue by {overdue_h:.1f}h",
            detail=(
                f"{_name(store, o.account_id)} - {o.carrier}, window ended "
                f"{o.pickup_window_end:%H:%M}, still BOOKED. "
                + ("Carrier fault is recorded. " if o.carrier_fault else "")
                + ("No open ticket references it." if not related else
                   f"Related: {', '.join(t.ticket_id for t in related)}.")
            ),
            records=[o.order_id] + [t.ticket_id for t in related],
            accounts=[o.account_id],
            recommended_action="Chase the carrier and contact the customer proactively",
            action_kind="followup_task",
            action_target={"order_id": o.order_id},
            metrics={"hours_overdue": round(overdue_h, 2), "carrier": o.carrier},
        ))

        # Is a credit owed that nobody has claimed?
        d = credit.assess(o, store.accounts.get(o.account_id), now)
        if d.outcome == "eligible":
            out.append(Insight(
                signal="unclaimed_credit",
                level="high",
                title=(
                    f"{_name(store, o.account_id)} is owed INR "
                    f"{d.detail['credit_inr']:.0f} on {o.order_id} and has not asked"
                ),
                detail=d.summary,
                records=[o.order_id],
                accounts=[o.account_id],
                recommended_action=(
                    f"Offer the INR {d.detail['credit_inr']:.0f} credit proactively"
                ),
                action_kind="service_credit",
                action_target={"order_id": o.order_id},
                evidence=[c.source_ref for c in d.citations],
                metrics={"credit_inr": d.detail["credit_inr"],
                         "delay_hours": d.detail.get("delay_hours")},
            ))
    return out


def _wrong_guidance_signals(store: Store, now: datetime) -> list[Insight]:
    """Compare closed-ticket resolutions against what the rules say today."""
    policy = terms.policy_defaults()
    out: list[Insight] = []

    sop_fee = policy["cancellation"]["by_status"]["BOOKED"]["fee_after_window_inr"]
    supported_rows = policy["product_facts"]["bulk_upload_supported_rows"]
    ki208_rows = policy["known_issues"]["KI-208"]["affects_above_rows"]

    for t in store.tickets.values():
        res = t.historical_resolution
        if not res:
            continue
        acc = store.accounts.get(t.account_id)

        # (a) a fee was quoted to an account whose agreement waives it
        if re.search(rf"\b{int(sop_fee)}\b", res) and re.search(r"fee", res, re.I):
            pr = precedence.resolve("cancellation_fee", acc, now)
            if pr.by_contract and pr.clause and pr.clause.get("waived_before_pickup"):
                out.append(Insight(
                    signal="wrong_past_guidance",
                    level="high",
                    title=f"{t.ticket_id} told {_name(store, t.account_id)} a fee applied that their agreement waives",
                    detail=(
                        f"Recorded resolution: \"{res}\" But {pr.source_ref} waives the "
                        "cancellation fee before pickup regardless of elapsed time."
                    ),
                    records=[t.ticket_id],
                    accounts=[t.account_id],
                    recommended_action=(
                        "Review whether this customer was wrongly charged, and correct "
                        "the record"
                    ),
                    action_kind="followup_task",
                    action_target={"ticket_id": t.ticket_id},
                    evidence=[pr.source_ref, policy["cancellation"]["source_ref"]],
                    metrics={"quoted_fee_inr": sop_fee},
                ))

        # (b) a row limit was quoted below the documented product limit
        m = re.search(r"([\d,]{3,7})\s*rows", res, re.I)
        if m:
            quoted = int(m.group(1).replace(",", ""))
            if quoted < supported_rows and re.search(r"plan|support", res, re.I):
                out.append(Insight(
                    signal="wrong_past_guidance",
                    level="high",
                    title=f"{t.ticket_id} understated the bulk-upload limit as a plan restriction",
                    detail=(
                        f"Recorded resolution: \"{res}\" The documented limit is "
                        f"{supported_rows:,} rows; failures above ~{ki208_rows:,} are "
                        "KI-208, a defect with a workaround, not a plan cap."
                    ),
                    records=[t.ticket_id],
                    accounts=[t.account_id],
                    recommended_action=(
                        "Correct the customer's understanding and link the ticket to KI-208"
                    ),
                    action_kind="followup_task",
                    action_target={"ticket_id": t.ticket_id},
                    evidence=[policy["product_facts"]["source_ref"],
                              policy["known_issues"]["KI-208"]["source_ref"]],
                    metrics={"quoted_rows": quoted, "documented_rows": supported_rows},
                ))
    return out


# Account concentration
def _concentration_signals(store: Store, now: datetime) -> list[Insight]:
    open_tickets = [t for t in store.tickets.values() if t.is_open]
    if len(open_tickets) < 3:
        return []
    counts: dict[str, int] = {}
    for t in open_tickets:
        counts[t.account_id] = counts.get(t.account_id, 0) + 1
    out = []
    for account_id, n in counts.items():
        share = n / len(open_tickets)
        if n >= 2 and share >= 0.4:
            recent = [
                t.ticket_id for t in open_tickets
                if t.account_id == account_id and t.created_at
                and (now - t.created_at) <= timedelta(hours=6)
            ]
            out.append(Insight(
                signal="account_concentration",
                level="medium",
                title=(
                    f"{_name(store, account_id)} accounts for {n} of "
                    f"{len(open_tickets)} open tickets"
                ),
                detail=(
                    f"{share:.0%} of current open volume from one account"
                    + (f", {len(recent)} in the last 6 hours." if recent else ".")
                ),
                records=[t.ticket_id for t in open_tickets if t.account_id == account_id],
                accounts=[account_id],
                recommended_action="Check for a shared root cause before treating these separately",
                metrics={"open_tickets": n, "share": round(share, 2)},
            ))
    return out


def detect(store: Store, now: datetime | None = None) -> dict:
    now = now or store.now
    insights: list[Insight] = []
    for fn in (
        _sla_signals,
        _known_issue_signals,
        _order_signals,
        _wrong_guidance_signals,
        _repeat_contact_signals,
        _concentration_signals,
    ):
        insights.extend(fn(store, now))

    # Dedupe repeated sources, keeping first-occurrence order.
    for i in insights:
        i.evidence = list(dict.fromkeys(i.evidence))

    insights.sort(key=lambda i: (SEVERITY_RANK.get(i.level, 9), i.signal))

    open_tickets = [t for t in store.tickets.values() if t.is_open]
    return {
        "reference_time": now.strftime("%A %d %B %Y, %H:%M"),
        "summary": {
            "open_tickets": len(open_tickets),
            "accounts": len(store.accounts),
            "critical": sum(1 for i in insights if i.level == "critical"),
            "high": sum(1 for i in insights if i.level == "high"),
            "medium": sum(1 for i in insights if i.level == "medium"),
            "signals": len(insights),
        },
        "method": (
            "All signals are computed deterministically from the supplied records. "
            "Nothing here is inferred by a language model."
        ),
        "insights": [i.to_dict() for i in insights],
    }
