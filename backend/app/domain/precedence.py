"""Source precedence per Support Policy v3 s1, resolved clause by clause."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from app.config import TIER_CURRENT_POLICY, TIER_CUSTOMER_AGREEMENT
from app.data.store import Account
from app.sources import terms

Topic = Literal[
    "sla_targets",
    "support_coverage_restriction",
    "cancellation_fee",
    "failed_pickup_credit",
    "credit_monthly_cap",
]

Governing = Literal["customer_agreement", "current_policy", "none"]


@dataclass
class ClauseResolution:
    topic: str
    governing: Governing
    tier: int
    source_ref: str
    quote: str
    clause: dict | None
    reason: str
    contract_exists: bool
    contract_addresses_topic: bool

    @property
    def by_contract(self) -> bool:
        return self.governing == "customer_agreement"


def _term_active(contract: dict, at: datetime) -> tuple[bool, str]:
    start = contract.get("term_start")
    end = contract.get("term_end")
    d: date = at.date()
    if start and d < date.fromisoformat(start):
        return False, f"agreement term has not begun (starts {start})"
    if end and d > date.fromisoformat(end):
        return False, f"agreement term ended {end}"
    if (contract.get("status") or "ACTIVE").upper() != "ACTIVE":
        return False, f"agreement status is {contract.get('status')}"
    return True, f"agreement active ({start} to {end})"


def resolve(topic: Topic, account: Account | None, at: datetime) -> ClauseResolution:
    """Decide which source governs `topic` for `account` at time `at`."""
    policy = terms.policy_defaults()
    default_ref = {
        "sla_targets": policy["sla_defaults"]["source_ref"],
        "support_coverage_restriction": policy["sla_defaults"]["source_ref"],
        "cancellation_fee": policy["cancellation"]["source_ref"],
        "failed_pickup_credit": policy["failed_pickup_credit"]["source_ref"],
        "credit_monthly_cap": policy["failed_pickup_credit"]["source_ref"],
    }[topic]

    def fall_through(reason: str, contract_exists: bool, addresses: bool) -> ClauseResolution:
        return ClauseResolution(
            topic=topic,
            governing="current_policy",
            tier=TIER_CURRENT_POLICY,
            source_ref=default_ref,
            quote=policy["sla_defaults"]["precedence_quote"],
            clause=None,
            reason=reason,
            contract_exists=contract_exists,
            contract_addresses_topic=addresses,
        )

    if account is None:
        return fall_through("no account resolved; general policy applies", False, False)

    contract = terms.contract_for(account.account_id)
    if contract is None:
        note = "no signed agreement for this account in the supplied pack"
        if account.contract_file:
            note = (
                f"the accounts sheet references {account.contract_file} but no "
                "structured terms are available for it"
            )
        return fall_through(note, False, False)

    active, term_note = _term_active(contract, at)
    if not active:
        return fall_through(f"{term_note}; general policy applies", True, False)

    clause = (contract.get("clauses") or {}).get(topic)
    if not clause or not clause.get("addressed"):
        reason = f"{contract['source_ref']} does not address {topic.replace('_', ' ')}"
        if clause and clause.get("quote"):
            reason += f" and defers to the current policy: \"{clause['quote']}\""
        return fall_through(reason, True, False)

    # The agreement addresses this topic but may still explicitly defer.
    if clause.get("defers_to_sop") and not clause.get("replaces_default"):
        res = fall_through(
            f"{clause.get('source_ref', contract['source_ref'])} addresses this topic "
            f"but defers to the current SOP: \"{clause.get('quote', '')}\"",
            True,
            True,
        )
        res.clause = clause
        return res

    return ClauseResolution(
        topic=topic,
        governing="customer_agreement",
        tier=TIER_CUSTOMER_AGREEMENT,
        source_ref=clause.get("source_ref", contract["source_ref"]),
        quote=clause.get("quote") or clause.get("replaces_default_quote") or "",
        clause=clause,
        reason=(
            f"{clause.get('source_ref', contract['source_ref'])} addresses "
            f"{topic.replace('_', ' ')} and takes precedence over the general policy "
            "(Support Policy v3 s1)"
        ),
        contract_exists=True,
        contract_addresses_topic=True,
    )


def explain_for_account(account: Account | None, at: datetime) -> dict:
    """Full precedence picture for an account."""
    topics: list[Topic] = [
        "sla_targets", "support_coverage_restriction", "cancellation_fee",
        "failed_pickup_credit", "credit_monthly_cap",
    ]
    return {
        "account_id": account.account_id if account else None,
        "account_name": account.account_name if account else None,
        "plan": account.plan if account else None,
        "topics": {
            t: {
                "governed_by": (r := resolve(t, account, at)).governing,
                "source": r.source_ref,
                "reason": r.reason,
                "quote": r.quote,
            }
            for t in topics
        },
    }
