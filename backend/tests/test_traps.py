"""
The planted traps.

The source pack is deliberately imperfect. Each test here names one trap and
asserts the system gets it RIGHT -- not merely that it produces an answer, but
that its answer contradicts the misleading source, and that the misleading
source cannot reach an answer as authority.

Trap inventory:
  1. Deprecated Support Policy v2 competing with v3
  2. TKT-450: a closed ticket asserting a cancellation fee the contract waives
  3. TKT-451: a closed ticket asserting a plan row-limit the product docs deny
  4. KI-211: BOOKED status is not proof a parcel is uncollected
  5. A contract clause that REPLACES a threshold, making a customer worse off
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools import Toolbox
from app.config import AUTHORITATIVE_TIERS, TIER_DEPRECATED
from app.corpus.search import search
from app.data.store import load_store
from app.domain import cancellation, credit, sla
from app.security.session import DEMO_PRINCIPALS, Principal, Role


@pytest.fixture(scope="module")
def store():
    return load_store()


@pytest.fixture(scope="module")
def now(store):
    return store.now


# ---------------------------------------------------------------------------
# Trap 1 -- the deprecated policy
# ---------------------------------------------------------------------------
def test_trap1_v2_targets_never_surface_as_an_answer(store, now):
    """v2: Enterprise P1 = 1 hour. v3: 30 min. Northstar's contract: 15 min."""
    a501 = sla.assess_ticket(store.tickets["TKT-501"], store.accounts["ACCT-001"], now)
    assert a501.decision.detail["target"] == "15 minutes, 24x7"
    assert all(c.tier in AUTHORITATIVE_TIERS for c in a501.decision.citations)
    assert not any("v2" in c.source_ref for c in a501.decision.citations)


def test_trap1_deprecated_doc_is_filtered_from_every_default_search():
    for principal in DEMO_PRINCIPALS.values():
        hits = search("enterprise P1 one hour response target", principal, limit=8)
        assert all(h.authority_tier != TIER_DEPRECATED for h in hits)


# ---------------------------------------------------------------------------
# Trap 2 -- TKT-450, the wrong cancellation-fee answer
# ---------------------------------------------------------------------------
def test_trap2_the_poisoned_ticket_says_the_opposite_of_the_truth(store):
    """Confirm the trap really is a trap before asserting we avoid it."""
    tkt = store.tickets["TKT-450"]
    assert tkt.account_id == "ACCT-001"
    assert "250" in tkt.historical_resolution
    assert "fee applied" in tkt.historical_resolution


def test_trap2_system_returns_no_fee_despite_the_poisoned_ticket(store, now):
    d = cancellation.assess(store.orders["ORD-1001"], store.accounts["ACCT-001"], now)
    assert d.detail["fee_inr"] == 0
    assert d.detail["minutes_since_booking"] == 120  # well past the 30-min window
    assert "TKT-450" not in json.dumps(d.to_dict())


def test_trap2_ticket_history_is_unreachable_from_policy_search():
    for principal in DEMO_PRINCIPALS.values():
        for query in ("cancellation fee after 30 minutes",
                      "Northstar cancellation fee 90 minutes",
                      "INR 250 fee applied"):
            for hit in search(query, principal, limit=8):
                assert "TKT-" not in hit.text
                assert "Agent told customer" not in hit.text


def test_trap2_ticket_history_tool_labels_results_as_unverified():
    tb = Toolbox(DEMO_PRINCIPALS["agent_maya"])
    res = tb.call("search_ticket_history", {"query": "cancellation fee 30 minutes"})
    assert "may contain incorrect past guidance" in res["reliability_warning"]
    hit = next(t for t in res["tickets"] if t["ticket_id"] == "TKT-450")
    assert "UNVERIFIED CONTEXT" in hit["historical_resolution_reliability"]


def test_trap2_customers_cannot_reach_ticket_history_at_all():
    for key in ("customer_northstar", "customer_lumenworks", "customer_beacon", "customer_axis"):
        tb = Toolbox(DEMO_PRINCIPALS[key])
        assert "search_ticket_history" not in tb.names()
        assert tb.call("search_ticket_history", {"query": "fee"})["error"].startswith("unknown tool")


# ---------------------------------------------------------------------------
# Trap 3 -- TKT-451, the wrong bulk-upload limit
# ---------------------------------------------------------------------------
def test_trap3_the_poisoned_ticket_understates_the_limit(store):
    assert "3,000 rows" in store.tickets["TKT-451"].historical_resolution


def test_trap3_authoritative_search_gives_5000_and_the_known_issue():
    tb = Toolbox(DEMO_PRINCIPALS["agent_maya"])
    res = tb.call("search_policy_documents", {"query": "bulk upload CSV row limit failing"})
    blob = " ".join(r["text"] for r in res["results"])
    assert "5,000 rows" in blob
    assert "KI-208" in blob
    assert "Workaround" in blob
    # the plan-limit claim must not appear in any authoritative passage
    assert "only supports 3,000" not in blob


def test_trap3_product_doc_is_tier_3_not_ticket_tier():
    hits = search("bulk upload row limit", DEMO_PRINCIPALS["agent_maya"], limit=5)
    assert hits[0].source_ref == "Product Operations Guide KI-208"
    assert hits[0].authority_tier in AUTHORITATIVE_TIERS


# ---------------------------------------------------------------------------
# Trap 4 -- BOOKED is not proof of no pickup
# ---------------------------------------------------------------------------
def test_trap4_swiftship_booked_order_demands_verification(store, now):
    d = cancellation.assess(store.orders["ORD-1001"], store.accounts["ACCT-001"], now)
    assert d.detail["pickup_confirmation_uncertain"] is True
    assert d.outcome.endswith("pending_verification")
    assert any("KI-211" in c.source_ref for c in d.citations)
    assert any("has not been collected" in v for v in d.verify_before_acting)


def test_trap4_no_false_alarm_when_the_window_has_not_opened(store, now):
    """The caveat must be earned. ORD-2001's window opens at the reference time."""
    d = cancellation.assess(store.orders["ORD-2001"], store.accounts["ACCT-002"], now)
    assert d.detail["pickup_confirmation_uncertain"] is False
    assert not d.verify_before_acting


def test_trap4_non_lagging_carrier_gets_no_caveat(store, now):
    """KI-211 names SwiftShip only; RoadRunner must not inherit the caveat."""
    o = store.orders["ORD-3001"]
    assert "swiftship" not in o.carrier.lower()
    d = cancellation.assess(o, store.accounts["ACCT-003"], now)
    assert d.detail["pickup_confirmation_uncertain"] is False


# ---------------------------------------------------------------------------
# Trap 5 -- a replacing clause that harms the customer
# ---------------------------------------------------------------------------
def test_trap5_contract_threshold_can_be_worse_and_is_stated_plainly(store, now):
    d = credit.assess(store.orders["ORD-2002"], store.accounts["ACCT-002"], now,
                      delay_hours_override=3.0)
    assert d.outcome == "not_eligible"
    assert d.conflicts, "the system must surface the differing thresholds"
    joined = " ".join(d.reasoning)
    assert "would qualify" in joined  # says the default would have been kinder
    refs = {c.source_ref for c in d.citations}
    assert any("LumenWorks" in r for r in refs)
    assert any("SOP v4" in r for r in refs)


def test_trap5_the_same_facts_favour_an_account_with_no_contract(store, now):
    o = store.orders["ORD-3001"]
    object.__setattr__(o, "carrier_fault", True)
    d = credit.assess(o, store.accounts["ACCT-003"], now, delay_hours_override=3.0)
    object.__setattr__(o, "carrier_fault", False)
    assert d.outcome == "eligible"


def test_trap5_above_threshold_lumenworks_gets_the_fixed_amount(store, now):
    d = credit.assess(store.orders["ORD-2002"], store.accounts["ACCT-002"], now)
    assert d.detail["delay_hours"] == 4.5
    assert d.detail["credit_inr"] == 300  # not the SOP's 240


# ---------------------------------------------------------------------------
# Cross-cutting: every decision is fully sourced
# ---------------------------------------------------------------------------
def test_every_decision_cites_only_authoritative_sources(store, now):
    decisions = []
    for o in store.orders.values():
        acc = store.accounts.get(o.account_id)
        decisions.append(cancellation.assess(o, acc, now))
        decisions.append(credit.assess(o, acc, now))
    for t in store.tickets.values():
        decisions.append(sla.assess_ticket(t, store.accounts.get(t.account_id), now).decision)

    assert decisions
    for d in decisions:
        for c in d.citations:
            assert c.tier in AUTHORITATIVE_TIERS, f"{d.outcome}: {c.source_ref}"


def test_no_decision_is_ever_unsourced_unless_it_escalates(store, now):
    for o in store.orders.values():
        acc = store.accounts.get(o.account_id)
        for d in (cancellation.assess(o, acc, now), credit.assess(o, acc, now)):
            assert d.citations or d.escalate, f"{o.order_id}: {d.outcome} had no sources"
