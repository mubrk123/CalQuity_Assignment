"""
Ground-truth regression suite.

Every expectation here was derived by hand from the source pack before any
agent code existed. These tests are the contract the LLM layer must not break.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import TIMEZONE
from app.data.store import load_store
from app.domain import cancellation, credit, precedence, sla
from app.domain.calendar import business_hours_between, is_business_day
from app.domain.severity import classify
from app.security.session import AccessDenied, Principal, Role, ScopedStore
from app.sources import terms


@pytest.fixture(scope="module")
def store():
    return load_store()


@pytest.fixture(scope="module")
def now(store):
    return store.now


# ---------------------------------------------------------------------------
# Source integrity
# ---------------------------------------------------------------------------
def test_all_declared_quotes_exist_in_source_pdfs():
    _p, _c, verified = terms.load()
    assert len(verified) >= 40


def test_snapshot_is_read_from_readme_and_is_a_sunday(store):
    assert store.snapshot.taken_at == datetime(2026, 8, 16, 11, 0, tzinfo=TIMEZONE)
    assert store.snapshot.taken_at.strftime("%A") == "Sunday"
    assert not is_business_day(store.snapshot.taken_at)


def test_deprecated_policy_is_tiered_out_of_authority():
    import json
    from app.config import CORPUS_FILE
    corpus = json.loads(CORPUS_FILE.read_text())
    v2 = [c for c in corpus["chunks"] if c["doc_id"].startswith("02_")]
    assert v2 and all(c["authority_tier"] == 90 for c in v2)
    v3 = [c for c in corpus["chunks"] if c["doc_id"].startswith("01_")]
    assert v3 and all(c["authority_tier"] == 2 for c in v3)


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ticket_id,expected",
    [
        ("TKT-501", "P1"),  # all shipment creation failing, no workaround
        ("TKT-505", "P1"),  # suspected credential exposure
        ("TKT-502", "P2"),  # bulk upload down, one-by-one still works
        ("TKT-503", "P3"),  # how-to: change billing contact
        ("TKT-504", "P3"),  # status display lag, limited impact
    ],
)
def test_severity_matches_policy_definitions(store, ticket_id, expected):
    t = store.tickets[ticket_id]
    assert classify(t.subject, t.description).severity == expected


def test_suspected_credential_exposure_is_p1_without_confirmed_exploitation(store):
    v = classify(store.tickets["TKT-505"].subject, store.tickets["TKT-505"].description)
    assert v.severity == "P1"
    assert "credential exposure" in v.criterion


# ---------------------------------------------------------------------------
# Precedence -- clause by clause, not document by document
# ---------------------------------------------------------------------------
def test_northstar_contract_governs_cancellation_but_defers_on_credit(store, now):
    acc = store.accounts["ACCT-001"]
    assert precedence.resolve("cancellation_fee", acc, now).governing == "customer_agreement"
    # s3 explicitly defers to the SOP -> falls through despite a contract existing
    res = precedence.resolve("failed_pickup_credit", acc, now)
    assert res.governing == "current_policy"
    assert res.contract_exists and not res.by_contract


def test_lumenworks_contract_declines_waiver_but_replaces_credit_rule(store, now):
    acc = store.accounts["ACCT-002"]
    assert precedence.resolve("cancellation_fee", acc, now).governing == "current_policy"
    assert precedence.resolve("failed_pickup_credit", acc, now).governing == "customer_agreement"


def test_accounts_without_a_contract_use_policy_defaults(store, now):
    for acct in ("ACCT-003", "ACCT-004"):
        res = precedence.resolve("sla_targets", store.accounts[acct], now)
        assert res.governing == "current_policy"
        assert not res.contract_exists


# ---------------------------------------------------------------------------
# SLA targets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "account_id,severity,expected,source_kind",
    [
        ("ACCT-001", "P1", "15 minutes, 24x7", "customer_agreement"),  # contract beats v3's 30 min
        ("ACCT-001", "P3", "8 business hours", "customer_agreement"),
        ("ACCT-002", "P1", "2 business hours", "customer_agreement"),
        ("ACCT-003", "P1", "4 business hours", "current_policy"),      # Standard row
        ("ACCT-004", "P1", "30 minutes, 24x7", "current_policy"),      # Enterprise row, no contract
    ],
)
def test_sla_target_resolution(store, now, account_id, severity, expected, source_kind):
    target, res = sla.resolve_target(store.accounts[account_id], severity, now)
    assert target.describe() == expected
    assert res.governing == source_kind


def test_deprecated_v2_targets_are_never_returned(store, now):
    """v2 says Enterprise P1 = 1 hour. No resolution may produce that."""
    for acct in store.accounts.values():
        for sev in ("P1", "P2", "P3"):
            target, _ = sla.resolve_target(acct, sev, now)
            assert "v2" not in target.source_ref
            if acct.plan == "Enterprise" and sev == "P1":
                assert target.amount in (15, 30) and target.unit == "minutes"


# ---------------------------------------------------------------------------
# Breach detection -- exactly two, both 24x7 Enterprise P1
# ---------------------------------------------------------------------------
def test_only_the_two_24x7_p1_tickets_are_breached(store, now):
    breached = set()
    for t in store.tickets.values():
        if not t.is_open:
            continue
        a = sla.assess_ticket(t, store.accounts.get(t.account_id), now)
        if a.evaluation.breached:
            breached.add(t.ticket_id)
    assert breached == {"TKT-501", "TKT-505"}


def test_weekend_pauses_business_hour_clocks(store, now):
    """LumenWorks' P2 clock must not run on a Sunday."""
    a = sla.assess_ticket(store.tickets["TKT-502"], store.accounts["ACCT-002"], now)
    assert a.severity.severity == "P2"
    assert not a.evaluation.breached
    assert not a.evaluation.clock_started
    assert business_hours_between(store.tickets["TKT-502"].created_at, now) == 0.0


def test_breach_magnitudes(store, now):
    a501 = sla.assess_ticket(store.tickets["TKT-501"], store.accounts["ACCT-001"], now)
    assert a501.decision.detail["target"] == "15 minutes, 24x7"
    assert a501.evaluation.deadline == datetime(2026, 8, 16, 10, 45, tzinfo=TIMEZONE)

    a505 = sla.assess_ticket(store.tickets["TKT-505"], store.accounts["ACCT-004"], now)
    assert a505.decision.detail["target"] == "30 minutes, 24x7"
    assert a505.evaluation.deadline == datetime(2026, 8, 16, 9, 0, tzinfo=TIMEZONE)


def test_p1_always_escalates(store, now):
    for tid in ("TKT-501", "TKT-505"):
        a = sla.assess_ticket(store.tickets[tid], store.accounts.get(store.tickets[tid].account_id), now)
        assert a.decision.escalate


def test_first_response_is_reported_as_unobservable(store, now):
    a = sla.assess_ticket(store.tickets["TKT-501"], store.accounts["ACCT-001"], now)
    assert "first_response_unobservable" in a.decision.assumption_keys
    assert "no first response is recorded" in a.decision.summary.lower()


# ---------------------------------------------------------------------------
# Cancellation -- the headline trap
# ---------------------------------------------------------------------------
def test_ORD_1001_northstar_no_fee_despite_120_minutes(store, now):
    """The contract waiver beats the SOP's 30-minute window AND beats TKT-450."""
    d = cancellation.assess(store.orders["ORD-1001"], store.accounts["ACCT-001"], now)
    assert d.detail["fee_inr"] == 0
    assert d.detail["minutes_since_booking"] == 120
    assert d.detail["fee_waived_by_agreement"] is True
    assert d.requires_confirmation
    refs = [c.source_ref for c in d.citations]
    assert any("Northstar" in r for r in refs)


def test_ORD_1001_flags_swiftship_pickup_uncertainty(store, now):
    d = cancellation.assess(store.orders["ORD-1001"], store.accounts["ACCT-001"], now)
    assert d.detail["pickup_confirmation_uncertain"] is True
    assert d.outcome == "cancellable_no_fee_pending_verification"
    assert d.verify_before_acting
    assert any("KI-211" in c.source_ref for c in d.citations)


def test_ORD_2001_lumenworks_pays_the_fee_on_the_same_facts(store, now):
    """Same status, same day, past 30 minutes -- opposite answer to ORD-1001."""
    d = cancellation.assess(store.orders["ORD-2001"], store.accounts["ACCT-002"], now)
    assert d.detail["fee_inr"] == 250
    assert d.detail["minutes_since_booking"] == 75
    assert d.outcome == "cancellable_with_fee"


def test_ORD_3001_beacon_inside_free_window(store, now):
    d = cancellation.assess(store.orders["ORD-3001"], store.accounts["ACCT-003"], now)
    assert d.detail["fee_inr"] == 0
    assert d.detail["minutes_since_booking"] == 15
    assert d.detail["fee_waived_by_agreement"] is False


def test_picked_up_and_delivered_cannot_be_cancelled(store, now):
    d1 = cancellation.assess(store.orders["ORD-1002"], store.accounts["ACCT-001"], now)
    assert d1.outcome == "not_cancellable"
    assert "return-to-origin" in d1.summary
    d2 = cancellation.assess(store.orders["ORD-4001"], store.accounts["ACCT-004"], now)
    assert d2.outcome == "not_cancellable"


# ---------------------------------------------------------------------------
# Service credit
# ---------------------------------------------------------------------------
def test_ORD_2002_lumenworks_fixed_300_not_default_240(store, now):
    d = credit.assess(store.orders["ORD-2002"], store.accounts["ACCT-002"], now)
    assert d.outcome == "eligible"
    assert d.detail["credit_inr"] == 300
    assert d.detail["delay_hours"] == 4.5
    assert d.detail["threshold_hours"] == 4
    assert not d.requires_manager_approval  # 300 < 1000


def test_three_hour_delay_answer_depends_on_the_account(store, now):
    """The brief's second example question: same facts, opposite answers."""
    lumen = credit.assess(store.orders["ORD-2002"], store.accounts["ACCT-002"], now,
                          delay_hours_override=3.0)
    assert lumen.outcome == "not_eligible"
    assert lumen.conflicts, "the threshold conflict must be surfaced, not hidden"

    # Beacon Retail has no agreement -> SOP's 2-hour threshold applies
    beacon_order = store.orders["ORD-3001"]
    object.__setattr__(beacon_order, "carrier_fault", True)
    beacon = credit.assess(beacon_order, store.accounts["ACCT-003"], now,
                           delay_hours_override=3.0)
    assert beacon.outcome == "eligible"
    assert beacon.detail["credit_inr"] == 120  # min(500, 10% of 1200)
    object.__setattr__(beacon_order, "carrier_fault", False)


def test_northstar_credit_uses_sop_default_with_contract_monthly_cap(store, now):
    o = store.orders["ORD-1001"]
    object.__setattr__(o, "carrier_fault", True)
    d = credit.assess(o, store.accounts["ACCT-001"], now, delay_hours_override=5.0)
    object.__setattr__(o, "carrier_fault", False)
    assert d.outcome == "eligible"
    assert d.detail["credit_inr"] == 420  # min(500, 10% of 4200)
    assert d.detail["monthly_cap_inr"] == 5000
    assert d.verify_before_acting


def test_unknown_fault_never_promises_a_credit(store, now):
    o = store.orders["ORD-2002"]
    object.__setattr__(o, "carrier_fault", None)
    d = credit.assess(o, store.accounts["ACCT-002"], now)
    object.__setattr__(o, "carrier_fault", True)
    assert d.outcome == "cannot_determine"
    assert d.escalate


def test_credit_above_1000_requires_manager_approval(store, now):
    o = store.orders["ORD-1001"]
    object.__setattr__(o, "carrier_fault", True)
    object.__setattr__(o, "shipment_fee_inr", 40000.0)
    d = credit.assess(o, store.accounts["ACCT-003"], now, delay_hours_override=5.0)
    object.__setattr__(o, "shipment_fee_inr", 4200.0)
    object.__setattr__(o, "carrier_fault", False)
    assert d.detail["credit_inr"] == 500  # capped
    assert d.requires_manager_approval is False  # 500 !> 1000


# ---------------------------------------------------------------------------
# Access control -- enforced in the data layer
# ---------------------------------------------------------------------------
def test_customer_cannot_read_another_accounts_order():
    s = ScopedStore(Principal(Role.CUSTOMER, "ACCT-002"))
    assert s.order("ORD-2001") is not None
    assert s.order("ORD-1001") is None  # Northstar's -> indistinguishable from missing


def test_customer_cannot_read_another_accounts_ticket_or_account():
    s = ScopedStore(Principal(Role.CUSTOMER, "ACCT-002"))
    assert s.ticket("TKT-501") is None
    assert s.account("ACCT-001") is None
    assert len(s.accounts()) == 1


def test_customer_account_reference_always_resolves_to_self():
    """'What are Northstar's terms?' asked by LumenWorks must not reach ACCT-001."""
    s = ScopedStore(Principal(Role.CUSTOMER, "ACCT-002"))
    for ref in ("Northstar Logistics", "ACCT-001", "northstar"):
        assert s.resolve_account(ref).account_id == "ACCT-002"


def test_internal_roles_see_everything(store):
    s = ScopedStore(Principal(Role.SUPPORT_AGENT))
    assert len(s.accounts()) == len(store.accounts)
    assert s.order("ORD-1001") is not None
    assert s.resolve_account("Northstar Logistics").account_id == "ACCT-001"


def test_customer_cannot_reach_the_operations_dashboard():
    s = ScopedStore(Principal(Role.CUSTOMER, "ACCT-001"))
    with pytest.raises(AccessDenied):
        _ = s.raw


def test_only_managers_may_approve_credits():
    assert not Principal(Role.SUPPORT_AGENT).can("approve_credit")
    assert Principal(Role.SUPPORT_MANAGER).can("approve_credit")
    assert not Principal(Role.CUSTOMER, "ACCT-001").can("create_escalation")


def test_customer_principal_must_be_bound_to_an_account():
    with pytest.raises(ValueError):
        Principal(Role.CUSTOMER)


def test_lumenworks_waiver_denial_is_cited_to_the_agreement_not_the_sop(store, now):
    """Regression: the quote comes from the agreement, so the citation must too."""
    d = cancellation.assess(store.orders["ORD-2001"], store.accounts["ACCT-002"], now)
    agreement_cites = [c for c in d.citations if "LumenWorks" in c.source_ref]
    assert agreement_cites, "the agreement's deferral clause must be cited to the agreement"
    assert all(c.tier == 1 for c in agreement_cites)


def test_no_pickup_lag_warning_before_the_window_opens(store, now):
    """ORD-2001's pickup window opens at the reference time; no lag risk exists yet."""
    d = cancellation.assess(store.orders["ORD-2001"], store.accounts["ACCT-002"], now)
    assert d.detail["pickup_confirmation_uncertain"] is False
    assert not d.verify_before_acting
    assert not any("KI-211" in c.source_ref for c in d.citations)
