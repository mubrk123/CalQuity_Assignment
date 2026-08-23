"""Cancellation eligibility and fee, per SOP v4 s1 and any governing agreement."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config import (
    TIER_CURRENT_POLICY,
    TIER_CUSTOMER_AGREEMENT,
    TIER_PRODUCT_DOC,
)
from app.data.store import Account, Order
from app.domain import known_issues, precedence
from app.domain.results import Decision
from app.sources import terms


def assess(order: Order, account: Account | None, now: datetime) -> Decision:
    policy = terms.policy_defaults()
    canc = policy["cancellation"]
    by_status = canc["by_status"]

    d = Decision(outcome="", summary="")
    status = (order.status or "").upper()

    d.reasoning.append(
        f"{order.order_id} belongs to {account.account_name if account else order.account_id} "
        f"and is currently {status} with carrier {order.carrier}."
    )

    rule = by_status.get(status)
    if rule is None:
        d.outcome = "unknown_status"
        d.summary = (
            f"{order.order_id} has status {status!r}, which the current SOP does not "
            "cover. This needs a human to look at."
        )
        d.escalate = True
        d.escalation_reason = f"order status {status!r} is not addressed by SOP v4 s1"
        return d

    d.cite(canc["source_ref"], TIER_CURRENT_POLICY, rule["quote"])

    if not rule["cancellable"]:
        d.outcome = "not_cancellable"
        alt = rule.get("alternative")
        d.summary = (
            f"{order.order_id} is {status} and cannot be cancelled."
            + (f" The {alt} should be used instead if the customer wants the parcel back." if alt else "")
        )
        d.reasoning.append(f"SOP v4 s1 is explicit for {status} shipments.")
        if status == "PICKED_UP":
            res = precedence.resolve("cancellation_fee", account, now)
            if res.by_contract and res.clause and res.clause.get("after_pickup_quote"):
                d.cite(res.source_ref, TIER_CUSTOMER_AGREEMENT, res.clause["after_pickup_quote"])
                d.reasoning.append(
                    "The customer agreement points to the same return-to-origin route "
                    "after pickup, so there is no conflict here."
                )
        return d

    if status == "DRAFT":
        d.outcome = "cancellable_no_fee"
        d.summary = f"{order.order_id} is a DRAFT and can be cancelled with no fee."
        d.requires_confirmation = True
        return d

    free_window = int(rule["free_window_minutes"])
    default_fee = float(rule["fee_after_window_inr"])
    requested_at = order.cancellation_requested_at or now
    booked_at = order.booked_at

    if booked_at is None:
        d.outcome = "insufficient_data"
        d.summary = (
            f"{order.order_id} has no recorded booking time, so the {free_window}-minute "
            "free-cancellation window cannot be evaluated."
        )
        d.escalate = True
        d.escalation_reason = "booked_at missing; cannot compute the free-cancellation window"
        return d

    elapsed_min = (requested_at - booked_at).total_seconds() / 60.0
    within_free = elapsed_min <= free_window
    d.reasoning.append(
        f"Booked at {booked_at.strftime('%H:%M')}, cancellation requested at "
        f"{requested_at.strftime('%H:%M')} - {elapsed_min:.0f} minutes later "
        f"({'inside' if within_free else 'outside'} the {free_window}-minute free window)."
    )

    res = precedence.resolve("cancellation_fee", account, now)
    d.reasoning.append(f"Precedence: {res.reason}.")

    waived = bool(res.by_contract and res.clause and res.clause.get("waived_before_pickup"))

    if within_free:
        fee = 0.0
        basis = f"inside the {free_window}-minute free-cancellation window in SOP v4 s1"
    elif waived:
        fee = 0.0
        basis = "the customer agreement waives the cancellation fee before pickup"
        d.cite(res.source_ref, TIER_CUSTOMER_AGREEMENT, res.clause["quote"])  # type: ignore[index]
        if res.clause.get("waiver_ignores_elapsed_time"):  # type: ignore[union-attr]
            d.reasoning.append(
                "The agreement waives the fee regardless of how long ago the shipment "
                f"was booked, so the {elapsed_min:.0f}-minute elapsed time does not "
                "create a fee for this account."
            )
    else:
        fee = default_fee
        basis = "outside the free window and no agreement waiver applies"
        if res.clause and res.clause.get("quote"):
            # Cite the agreement's own reference, not the policy that governed.
            d.cite(
                res.clause.get("source_ref") or res.source_ref,
                TIER_CUSTOMER_AGREEMENT,
                res.clause["quote"],
            )
            d.reasoning.append(
                "This account's agreement explicitly declines a cancellation-fee "
                "waiver and defers to the SOP."
            )

    # A BOOKED status is not proof of no pickup when a known issue lags webhooks.
    pickup_uncertain = False
    for match in known_issues.for_order(order, now):
        pickup_uncertain = True
        d.cite(match.source_ref, TIER_PRODUCT_DOC, match.guidance)
        d.reasoning.append(
            f"{match.source_ref} applies here ({match.why}), so a {status} status "
            "is not proof that the parcel is still uncollected."
        )
        d.verify_before_acting.append(
            f"Confirm with {order.carrier} that the parcel has not been collected "
            f"before executing the cancellation ({match.issue_id})."
        )

    d.detail = {
        "order_id": order.order_id,
        "status": status,
        "minutes_since_booking": round(elapsed_min),
        "free_window_minutes": free_window,
        "fee_inr": fee,
        "fee_waived_by_agreement": waived and not within_free,
        "governed_by": res.governing,
        "pickup_confirmation_uncertain": pickup_uncertain,
    }

    if fee == 0.0:
        d.outcome = "cancellable_no_fee_pending_verification" if pickup_uncertain else "cancellable_no_fee"
        d.summary = (
            f"{order.order_id} can be cancelled with no cancellation fee, because {basis}."
        )
        if pickup_uncertain:
            d.summary += (
                " One caveat: the no-fee route applies only before pickup, and this "
                f"shipment's {order.carrier} status may be lagging, so carrier pickup "
                "should be confirmed before the cancellation is executed."
            )
    else:
        d.outcome = "cancellable_with_fee"
        d.summary = (
            f"{order.order_id} can be cancelled, but an INR {fee:.0f} cancellation fee "
            f"applies because it is {basis}."
        )

    d.requires_confirmation = True
    return d
