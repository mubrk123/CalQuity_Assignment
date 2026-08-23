"""SLA target resolution and first-response assessment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import TIER_CURRENT_POLICY, TIER_CUSTOMER_AGREEMENT
from app.data.store import Account, Ticket
from app.domain import precedence
from app.domain.calendar import ResponseTarget, TargetEvaluation, evaluate_target
from app.domain.results import Decision
from app.domain.severity import SeverityVerdict, classify
from app.sources import terms


def resolve_target(account: Account | None, severity: str, at: datetime
                   ) -> tuple[ResponseTarget, precedence.ClauseResolution]:
    """Return the governing first-response target for (account, severity)."""
    res = precedence.resolve("sla_targets", account, at)
    policy = terms.policy_defaults()

    if res.by_contract and res.clause:
        spec = res.clause["targets"][severity]
        return (
            ResponseTarget(
                amount=spec["amount"], unit=spec["unit"], coverage=spec["coverage"],
                source_ref=res.source_ref, quote=spec["quote"],
                interpretation=spec.get("interpretation"),
            ),
            res,
        )

    plan = (account.plan if account else None) or "Standard"
    rows = policy["sla_defaults"]["rows"]
    row = rows.get(plan) or rows["Standard"]
    spec = row[severity]
    return (
        ResponseTarget(
            amount=spec["amount"], unit=spec["unit"], coverage=spec["coverage"],
            source_ref=policy["sla_defaults"]["source_ref"], quote=row["quote"],
            interpretation=spec.get("interpretation"),
        ),
        res,
    )


def coverage_restriction(account: Account | None, at: datetime) -> dict | None:
    res = precedence.resolve("support_coverage_restriction", account, at)
    return res.clause if res.by_contract and res.clause else None


@dataclass
class SlaAssessment:
    ticket_id: str
    account_id: str
    account_name: str
    severity: SeverityVerdict
    evaluation: TargetEvaluation
    decision: Decision

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "severity": self.severity.to_dict(),
            "timing": self.evaluation.to_dict(),
            **self.decision.to_dict(),
        }


def assess_ticket(ticket: Ticket, account: Account | None, now: datetime,
                  severity: SeverityVerdict | None = None) -> SlaAssessment:
    """Assess first-response status for one ticket."""
    sev = severity or classify(ticket.subject, ticket.description)
    target, res = resolve_target(account, sev.severity, now)
    ev = evaluate_target(target, ticket.created_at or now, now)

    d = Decision(outcome="", summary="")
    policy = terms.policy_defaults()
    esc = policy["escalation"]

    d.reasoning.append(
        f"Severity {sev.severity} ({sev.method}): {sev.criterion}."
    )
    d.cite(sev.source_ref, TIER_CURRENT_POLICY, sev.quote)

    d.reasoning.append(f"Precedence: {res.reason}.")
    d.cite(
        target.source_ref,
        TIER_CUSTOMER_AGREEMENT if res.by_contract else TIER_CURRENT_POLICY,
        target.quote,
    )
    d.reasoning.append(
        f"Governing first-response target: {target.describe()} "
        f"(from {target.source_ref})."
    )

    if target.interpretation:
        d.assume(target.interpretation)
    if ev.depends_on_business_calendar:
        d.assume("business_hours")
        if target.unit == "business_days":
            d.assume("business_day")

    restriction = coverage_restriction(account, now)
    if restriction and ev.depends_on_business_calendar:
        d.reasoning.append(
            "This account's agreement excludes weekend and after-hours coverage, "
            "so the business-hours clock does not run outside those windows."
        )
        d.cite(restriction["source_ref"], TIER_CUSTOMER_AGREEMENT, restriction["quote"])

    d.assume("first_response_unobservable")
    d.reasoning.append(
        f"Ticket opened {ev.started_at.strftime('%a %d %b %Y %H:%M')}; "
        f"reference time {ev.now.strftime('%a %d %b %Y %H:%M')}. "
        f"{ev.elapsed_display.capitalize()}; target deadline "
        f"{ev.deadline.strftime('%a %d %b %Y %H:%M')}."
    )

    if not ev.clock_started:
        d.outcome = "within_target_clock_not_started"
        d.summary = (
            f"{ticket.ticket_id} is {sev.severity}. The {target.describe()} target is "
            "measured in business time and no business time has elapsed yet, so the "
            "first-response clock has not started."
        )
    elif ev.breached:
        d.outcome = "target_exceeded"
        d.summary = (
            f"{ticket.ticket_id} is {sev.severity} with a {target.describe()} "
            f"first-response target that expired at {ev.deadline.strftime('%H:%M on %d %b')}. "
            "No first response is recorded in the dataset."
        )
        d.escalate = True
        d.escalation_reason = "first-response target exceeded with no recorded response"
        d.cite(policy["escalation"]["source_ref"], TIER_CURRENT_POLICY, esc["breach_disclosure_quote"])
    else:
        d.outcome = "within_target"
        d.summary = (
            f"{ticket.ticket_id} is {sev.severity}; {ev.remaining_display} against the "
            f"{target.describe()} target."
        )

    if sev.severity == "P1":
        d.escalate = True
        d.escalation_reason = (
            (d.escalation_reason + "; " if d.escalation_reason else "")
            + "P1 incidents are escalated immediately"
        )
        d.cite(policy["escalation"]["source_ref"], TIER_CURRENT_POLICY, esc["p1_immediate_quote"])

    if ticket.customer_followed_up:
        d.reasoning.append(
            f"The customer sent a follow-up message at "
            f"{ticket.last_customer_message_at.strftime('%H:%M')}, which suggests they "  # type: ignore[union-attr]
            "were still waiting."
        )

    d.detail = {
        "severity": sev.severity,
        "target": target.describe(),
        "governed_by": res.governing,
        "deadline": ev.deadline.strftime("%Y-%m-%d %H:%M"),
        "breached": ev.breached,
        "clock_started": ev.clock_started,
    }

    return SlaAssessment(
        ticket_id=ticket.ticket_id,
        account_id=ticket.account_id,
        account_name=account.account_name if account else ticket.account_id,
        severity=sev,
        evaluation=ev,
        decision=d,
    )
