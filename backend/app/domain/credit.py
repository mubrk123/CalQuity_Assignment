"""Failed-pickup service credit, per SOP v4 s2 plus any governing agreement."""
from __future__ import annotations

from datetime import datetime

from app.config import TIER_CURRENT_POLICY, TIER_CUSTOMER_AGREEMENT
from app.data.store import Account, Order
from app.domain import precedence
from app.domain.results import Decision
from app.sources import terms


def assess(order: Order, account: Account | None, now: datetime,
           delay_hours_override: float | None = None) -> Decision:
    policy = terms.policy_defaults()
    default = policy["failed_pickup_credit"]
    approval = policy["approval_and_uncertainty"]

    d = Decision(outcome="", summary="")
    res = precedence.resolve("failed_pickup_credit", account, now)

    if res.by_contract and res.clause:
        c = res.clause
        threshold = float(c["threshold_hours"])
        rule_ref, rule_quote, rule_tier = res.source_ref, c["quote"], TIER_CUSTOMER_AGREEMENT
        fixed = float(c["fixed_credit_inr"]) if c.get("credit_rule") == "fixed" else None
        cap, pct = None, None
        replaces = bool(c.get("replaces_default"))
    else:
        threshold = float(default["threshold_hours"])
        rule_ref, rule_quote, rule_tier = default["source_ref"], default["quote"], TIER_CURRENT_POLICY
        fixed = None
        cap = float(default["credit_cap_inr"])
        pct = float(default["credit_pct_of_fee"])
        replaces = False

    d.cite(rule_ref, rule_tier, rule_quote)
    d.reasoning.append(f"Precedence: {res.reason}.")

    if delay_hours_override is not None:
        delay = float(delay_hours_override)
        d.reasoning.append(
            f"Using the stated delay of {delay:g} hours past the end of the scheduled "
            "pickup window."
        )
    else:
        if order.pickup_window_end is None:
            d.outcome = "insufficient_data"
            d.summary = (
                f"{order.order_id} has no scheduled pickup window end recorded, so the "
                "delay cannot be measured."
            )
            d.escalate = True
            d.escalation_reason = "pickup_window_end missing; cannot compute delay"
            d.cite(approval["source_ref"], TIER_CURRENT_POLICY, approval["no_promise_when_unknown_quote"])
            return d
        reference = order.pickup_actual_at or now
        delay = (reference - order.pickup_window_end).total_seconds() / 3600.0
        picked = order.pickup_actual_at is not None
        d.reasoning.append(
            f"Pickup window ended {order.pickup_window_end.strftime('%H:%M')}; "
            + (
                f"pickup was confirmed at {order.pickup_actual_at.strftime('%H:%M')}"  # type: ignore[union-attr]
                if picked
                else f"no pickup is confirmed as of the reference time {now.strftime('%H:%M')}"
            )
            + f" - a delay of {delay:.2f} hours."
        )

    unknown: list[str] = []
    if order.carrier_fault is None:
        unknown.append("carrier fault")
    if order.customer_fault is None:
        unknown.append("customer fault")
    if unknown:
        d.outcome = "cannot_determine"
        d.summary = (
            f"Eligibility for {order.order_id} cannot be confirmed because "
            f"{' and '.join(unknown)} is not recorded. The SOP forbids promising a "
            "credit on unknown fault."
        )
        d.cite(approval["source_ref"], TIER_CURRENT_POLICY, approval["no_promise_when_unknown_quote"])
        d.escalate = True
        d.escalation_reason = f"{' and '.join(unknown)} unknown for {order.order_id}"
        return d

    d.detail["delay_hours"] = round(delay, 2)
    d.detail["threshold_hours"] = threshold
    d.detail["carrier_fault"] = order.carrier_fault
    d.detail["customer_fault"] = order.customer_fault
    d.detail["governed_by"] = res.governing

    if order.customer_fault:
        d.outcome = "not_eligible"
        d.summary = (
            f"No service credit is due on {order.order_id}: the record shows a "
            "customer-caused issue, which the policy excludes."
        )
        return d

    if not order.carrier_fault:
        d.outcome = "not_eligible"
        d.summary = (
            f"No service credit is due on {order.order_id}: carrier fault is not "
            "recorded, and carrier fault is a condition of the credit."
        )
        return d

    if delay <= threshold:
        d.outcome = "not_eligible"
        d.summary = (
            f"No service credit is due on {order.order_id}: the delay of {delay:.2f} "
            f"hours does not exceed the {threshold:g}-hour threshold that applies to "
            "this account."
        )
        if replaces:
            d.reasoning.append(
                f"Note: this account's agreement replaces the SOP default "
                f"{default['threshold_hours']}-hour threshold with {threshold:g} hours. "
                f"On these same facts an account without that clause would qualify. "
                "The agreement governs because it is the higher-authority source."
            )
            d.cite(default["source_ref"], TIER_CURRENT_POLICY, default["quote"])
            d.conflicts.append({
                "topic": "failed_pickup_credit_threshold",
                "resolved_to": rule_ref,
                "detail": (
                    f"{rule_ref} sets a {threshold:g}-hour threshold; "
                    f"{default['source_ref']} sets {default['threshold_hours']}-hour. "
                    "The signed agreement takes precedence (Support Policy v3 s1)."
                ),
            })
        return d

    if fixed is not None:
        amount = fixed
        d.reasoning.append(
            f"The agreement specifies a fixed INR {fixed:.0f} credit, replacing the "
            "SOP's percentage-based calculation."
        )
        d.cite(default["source_ref"], TIER_CURRENT_POLICY, default["override_quote"])
    else:
        if order.shipment_fee_inr is None:
            d.outcome = "insufficient_data"
            d.summary = (
                f"{order.order_id} qualifies on timing and fault, but the shipment fee "
                "is missing so the credit amount cannot be calculated."
            )
            d.escalate = True
            d.escalation_reason = "shipment_fee_inr missing; cannot compute credit amount"
            return d
        pct_amount = order.shipment_fee_inr * (pct / 100.0)  # type: ignore[operator]
        amount = min(cap, pct_amount)  # type: ignore[arg-type]
        d.reasoning.append(
            f"Default credit is the lower of INR {cap:.0f} and {pct:g}% of the "
            f"INR {order.shipment_fee_inr:.0f} shipment fee "
            f"(INR {pct_amount:.0f}) = INR {amount:.0f}."
        )

    # Monthly aggregate cap, if the agreement sets one.
    cap_res = precedence.resolve("credit_monthly_cap", account, now)
    monthly_cap = None
    if cap_res.by_contract and cap_res.clause:
        monthly_cap = float(cap_res.clause["monthly_cap_inr"])
        d.cite(cap_res.source_ref, TIER_CUSTOMER_AGREEMENT, cap_res.clause["quote"])
        d.reasoning.append(
            f"This account's agreement caps monthly aggregate credits at INR "
            f"{monthly_cap:.0f}. Credits already issued this month are not present in "
            "the supplied dataset, so remaining headroom cannot be verified here."
        )
        d.verify_before_acting.append(
            f"Check credits already issued this month against the INR {monthly_cap:.0f} "
            "monthly aggregate cap before issuing."
        )

    threshold_inr = float(approval["manager_approval_above_inr"])
    needs_manager = amount > threshold_inr
    if needs_manager:
        d.requires_manager_approval = True
        d.cite(approval["source_ref"], TIER_CURRENT_POLICY, approval["manager_approval_quote"])
        d.reasoning.append(
            f"INR {amount:.0f} exceeds the INR {threshold_inr:.0f} threshold, so manager "
            "approval is required before the credit is issued."
        )

    d.outcome = "eligible"
    d.summary = (
        f"{order.order_id} qualifies for a service credit of INR {amount:.0f}. The "
        f"delay of {delay:.2f} hours exceeds the {threshold:g}-hour threshold, carrier "
        "fault is recorded, and no customer fault is recorded."
    )
    d.detail["credit_inr"] = amount
    d.detail["monthly_cap_inr"] = monthly_cap
    d.requires_confirmation = True
    return d
