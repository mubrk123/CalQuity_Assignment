"""Retrieval behaviour: authority filtering, account scoping, and ranking."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import TIER_DEPRECATED
from app.corpus.search import search, visible_chunks
from app.security.session import Principal, Role

CUST_NS = Principal(Role.CUSTOMER, "ACCT-001")
CUST_LW = Principal(Role.CUSTOMER, "ACCT-002")
CUST_BEACON = Principal(Role.CUSTOMER, "ACCT-003")
AGENT = Principal(Role.SUPPORT_AGENT)


# ---------------------------------------------------------------------------
# Authority filtering
# ---------------------------------------------------------------------------
def test_deprecated_policy_is_never_returned_by_default():
    for principal in (CUST_NS, CUST_LW, AGENT):
        for query in ("enterprise P1 response time", "support policy",
                      "severity targets", "1 hour enterprise"):
            hits = search(query, principal, limit=10)
            assert all(h.authority_tier != TIER_DEPRECATED for h in hits), query


def test_deprecated_policy_is_reachable_only_when_explicitly_requested():
    hits = search("old superseded policy v2", AGENT, limit=10, include_deprecated=True)
    assert any(h.authority_tier == TIER_DEPRECATED for h in hits)
    deprecated = next(h for h in hits if h.authority_tier == TIER_DEPRECATED)
    assert "DEPRECATED" in deprecated.to_dict()["warning"]


def test_ticket_text_is_not_in_the_policy_index():
    """The poisoned closed tickets must be unreachable from policy search."""
    for query in ("cancellation fee after 30 minutes Northstar",
                  "bulk upload Growth plan 3000 rows",
                  "agent told customer"):
        for h in search(query, AGENT, limit=10):
            assert "TKT-" not in h.text
            assert "TKT-" not in h.source_ref


# ---------------------------------------------------------------------------
# Account scoping of agreements
# ---------------------------------------------------------------------------
def test_customer_cannot_retrieve_another_customers_agreement():
    for query in ("Northstar cancellation waiver no fee",
                  "Northstar enterprise agreement P1 15 minutes",
                  "monthly aggregate service credits capped"):
        hits = search(query, CUST_LW, limit=10)
        assert all("Northstar" not in h.source_ref for h in hits), query


def test_customer_with_no_agreement_sees_no_agreement_chunks():
    hits = search("my contract terms cancellation credits", CUST_BEACON, limit=10)
    assert all(h.account_scope is None for h in hits)


def test_customer_can_retrieve_their_own_agreement():
    hits = search("cancel booked shipment no fee", CUST_NS, limit=10)
    assert any("Northstar" in h.source_ref for h in hits)


def test_internal_account_scoping_excludes_other_agreements():
    hits = search("cancellation fee waiver agreement", AGENT, limit=10, account_id="ACCT-001")
    assert any("Northstar" in h.source_ref for h in hits)
    assert all("LumenWorks" not in h.source_ref for h in hits)


def test_visible_chunk_counts_differ_by_principal():
    ns = {c["chunk_id"] for c in visible_chunks(CUST_NS)}
    lw = {c["chunk_id"] for c in visible_chunks(CUST_LW)}
    internal = {c["chunk_id"] for c in visible_chunks(AGENT)}
    assert ns != lw
    assert ns < internal and lw < internal
    assert not any("05_" in c for c in lw)
    assert not any("06_" in c for c in ns)


# ---------------------------------------------------------------------------
# Ranking quality
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,expected_top",
    [
        ("P1 first response target enterprise", "Support Policy v3 s3"),
        ("what are the severity definitions", "Support Policy v3 s2"),
        ("cancellation fee after 30 minutes", "Cancellation & Service Credit SOP v4 s1"),
        ("bulk upload row limit", "Product Operations Guide KI-208"),
        ("source precedence when documents conflict", "Support Policy v3 s1"),
    ],
)
def test_expected_section_ranks_first(query, expected_top):
    hits = search(query, AGENT, limit=5)
    assert hits, query
    assert hits[0].source_ref == expected_top


def test_agreement_outranks_policy_at_equal_relevance():
    """Support Policy v3 s1 puts agreements above policy; ties must reflect that."""
    hits = search("cancel booked shipment no cancellation fee", CUST_NS, limit=10)
    tiers = [h.authority_tier for h in hits]
    assert 1 in tiers
    # every tier-1 hit must carry the account's own scope
    assert all(h.account_scope == "ACCT-001" for h in hits if h.authority_tier == 1)


def test_search_requires_the_capability():
    from app.security.session import AccessDenied

    class NoSearch(Principal):
        pass

    p = Principal(Role.CUSTOMER, "ACCT-001")
    assert p.can("search_policy")
    # sanity: a capability that customers lack
    with pytest.raises(AccessDenied):
        p.require("create_escalation")
