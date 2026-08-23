"""
Agent-loop mechanics, exercised with a scripted model so no API key is needed.

These tests pin the parts that break silently in production: message shapes fed
back to the API, tool dispatch, the repeat-call guard, the iteration cap, and
the guarantee that a prepared action is never reported as executed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.actions.store import ACTIONS
from app.agent.agent import MAX_ITERATIONS, TOOL_BUDGET, Agent
from app.agent.llm import AssistantTurn, LLMUnavailable, ToolCall
from app.security.session import DEMO_PRINCIPALS


class ScriptedClient:
    """Replays a fixed list of AssistantTurns, recording what it was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []
        self.describe = "scripted:test"

    def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        if not self.turns:
            return AssistantTurn(content="(script exhausted)")
        return self.turns.pop(0)


def call(name, args, cid="c1"):
    return ToolCall(id=cid, name=name, arguments=args, raw_arguments=json.dumps(args))


def model_calls(out):
    """Tool calls the MODEL chose, excluding the context primed before it ran.

    The loop resolves which sources govern the account before the first model
    request and records it so the interface can show it. That entry is not a
    routing decision, so these tests look past it.
    """
    return [c for c in out["record"]["tool_calls"]
            if c["name"] != "get_account_context" or "primed" not in str(c)]


def agent_with(principal_key, turns):
    a = Agent(DEMO_PRINCIPALS[principal_key])
    a.client = ScriptedClient(turns)
    return a


# ---------------------------------------------------------------------------
# Basic loop
# ---------------------------------------------------------------------------
def test_single_turn_no_tools():
    a = agent_with("customer_northstar", [AssistantTurn(content="Hello.")])
    out = a.run_sync("hi")
    assert out["answer"] == "Hello."
    assert out["record"]["iterations"] == 1
    assert [c["name"] for c in out["record"]["tool_calls"]] in ([], ["get_account_context"])


def test_tool_call_then_answer():
    turns = [
        AssistantTurn(content=None, tool_calls=[call("get_order", {"order_id": "ORD-1001"})]),
        AssistantTurn(content="ORD-1001 is BOOKED with SwiftShip."),
    ]
    a = agent_with("customer_northstar", turns)
    out = a.run_sync("what's the status of ORD-1001?")
    assert out["answer"].startswith("ORD-1001 is BOOKED")
    names = [c["name"] for c in out["record"]["tool_calls"]]
    assert names[-1] == "get_order"
    assert out["record"]["tool_calls"][-1]["result"]["found"] is True


def test_assistant_tool_call_message_shape_is_echoed_back():
    """The API requires the tool_calls message replayed verbatim before results."""
    turns = [
        AssistantTurn(content=None, tool_calls=[call("get_order", {"order_id": "ORD-1001"})]),
        AssistantTurn(content="done"),
    ]
    a = agent_with("agent_maya", turns)
    a.run_sync("how do you rank sources?")
    second_request = a.client.seen[1]
    assistant_msg = next(m for m in second_request if m["role"] == "assistant")
    assert "tool_calls" in assistant_msg
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "get_order"
    tool_msg = next(m for m in second_request if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert "ORD-1001" in tool_msg["content"]


def test_multi_step_chain_is_recorded_in_order():
    turns = [
        AssistantTurn(tool_calls=[call("get_order", {"order_id": "ORD-1001"}, "a")]),
        AssistantTurn(tool_calls=[call("get_account_context", {"account_ref": "ACCT-001"}, "b")]),
        AssistantTurn(tool_calls=[call("evaluate_cancellation", {"order_id": "ORD-1001"}, "c")]),
        AssistantTurn(content="No fee, per your agreement."),
    ]
    a = agent_with("customer_northstar", turns)
    out = a.run_sync("can I cancel ORD-1001 free?")
    assert [c["name"] for c in out["record"]["tool_calls"]][-3:] == [
        "get_order", "get_account_context", "evaluate_cancellation",
    ]
    assert out["record"]["iterations"] == 4
    final = out["record"]["tool_calls"][-1]["result"]
    assert final["detail"]["fee_inr"] == 0


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_tool_budget_is_per_tool_not_per_exact_call():
    """Rewording a query must not dodge the budget.

    The original guard keyed on exact arguments, which a model defeats simply by
    rephrasing -- observed live as five search_policy_documents calls returning
    the same passage, each paying the full context cost again.
    """
    # One call past the budget, whatever the budget currently is -- the point is
    # that rewording cannot buy an extra call, not that the number is 3.
    budget = TOOL_BUDGET["search_policy_documents"]
    wordings = ["cancellation fee", "cancel fee rules", "fee on cancelling",
                "charge for cancel", "cancel charge", "fee to cancel"]
    turns = [
        AssistantTurn(tool_calls=[call("search_policy_documents",
                                       {"query": wordings[i % len(wordings)]}, f"c{i}")])
        for i in range(budget + 1)
    ] + [AssistantTurn(content="ok")]
    a = agent_with("customer_northstar", turns)
    out = a.run_sync("what is the fee?")
    results = [c["result"] for c in out["record"]["tool_calls"]
               if c["name"] == "search_policy_documents"]
    # Earlier calls either return results or are short-circuited as already
    # retrieved; either way they are served. The call past the budget is not.
    assert all(r.get("error") != "tool_budget_exhausted" for r in results[:budget])
    assert all("results" in r or "already_retrieved" in r for r in results[:budget])
    assert results[budget].get("error") == "tool_budget_exhausted"


def test_iteration_cap_ends_with_an_escalation_offer():
    turns = [
        AssistantTurn(tool_calls=[call("get_order", {"order_id": f"ORD-100{i%2+1}"}, f"c{i}")])
        for i in range(MAX_ITERATIONS + 3)
    ]
    a = agent_with("agent_maya", turns)
    out = a.run_sync("loop forever")
    assert out["record"]["iterations"] == MAX_ITERATIONS
    assert "escalation" in out["answer"].lower()


def test_unknown_tool_returns_available_tools():
    turns = [
        AssistantTurn(tool_calls=[call("delete_everything", {})]),
        AssistantTurn(content="ok"),
    ]
    a = agent_with("agent_maya", turns)
    out = a.run_sync("hi")
    res = out["record"]["tool_calls"][0]["result"]
    assert res["error"].startswith("unknown tool")
    assert "search_policy_documents" in res["available_tools"]


def test_llm_unavailable_is_surfaced_not_crashed():
    class Broken:
        describe = "broken:none"

        def chat(self, messages, tools=None):
            raise LLMUnavailable("no key configured")

    a = Agent(DEMO_PRINCIPALS["customer_northstar"])
    a.client = Broken()
    out = a.run_sync("hello")
    assert "llm_unavailable" in out["answer"]


# ---------------------------------------------------------------------------
# Confirmation guarantee
# ---------------------------------------------------------------------------
def test_prepared_action_is_pending_and_has_no_token_in_model_context():
    turns = [
        AssistantTurn(tool_calls=[call("prepare_action", {
            "kind": "escalation",
            "summary": "Escalate TKT-501",
            "reason": "P1 outage, target exceeded",
            "ticket_id": "TKT-501",
        })]),
        AssistantTurn(content="I've drafted the escalation - confirm to create it."),
    ]
    a = agent_with("agent_maya", turns)
    out = a.run_sync("escalate TKT-501")

    result = next(c["result"] for c in out["record"]["tool_calls"]
                  if c["name"] == "prepare_action")
    assert result["prepared"] is True
    assert result["state"] == "pending_confirmation"
    assert "confirmation_token" not in result
    assert result["reference"] is None  # nothing created

    action = ACTIONS.get(result["action_id"])
    assert action.state.value == "pending_confirmation"

    # the token must not appear anywhere in what the model was sent
    blob = json.dumps(a.client.seen[-1], default=str)
    assert ACTIONS.confirmation_token(result["action_id"]) not in blob


def test_no_tool_can_commit_an_action():
    """There must be no model-callable tool that executes a prepared action."""
    a = Agent(DEMO_PRINCIPALS["manager_priya"])
    names = " ".join(a.toolbox.names())
    for forbidden in ("commit", "confirm", "execute", "apply_action"):
        assert forbidden not in names


def test_customer_cannot_prepare_an_internal_action():
    turns = [
        AssistantTurn(tool_calls=[call("prepare_action", {
            "kind": "ticket_update",
            "summary": "close it",
            "reason": "because",
            "ticket_id": "TKT-501",
        })]),
        AssistantTurn(content="ok"),
    ]
    a = agent_with("customer_lumenworks", turns)
    out = a.run_sync("close ticket TKT-501")
    res = next(c["result"] for c in out["record"]["tool_calls"]
               if c["name"] == "prepare_action")
    # either the ticket is invisible, or the capability is refused -- never success
    assert res.get("found") is False or res.get("error") == "access_denied"


# ---------------------------------------------------------------------------
# Prompt wiring
# ---------------------------------------------------------------------------
def test_system_prompt_carries_snapshot_time_and_role_scope():
    cust = Agent(DEMO_PRINCIPALS["customer_lumenworks"]).system_message()["content"]
    assert "16 August 2026" in cust and "11:00" in cust
    assert "LumenWorks" in cust
    assert "You see only their data" in cust

    staff = Agent(DEMO_PRINCIPALS["agent_rohit"]).system_message()["content"]
    assert "PARCELPILOT STAFF" in staff
    assert "Not a manager" in staff

    mgr = Agent(DEMO_PRINCIPALS["manager_priya"]).system_message()["content"]
    assert "authorise credits above the SOP" in mgr


def test_customer_toolbox_excludes_ticket_history():
    assert "search_ticket_history" not in Agent(DEMO_PRINCIPALS["customer_axis"]).toolbox.names()
    assert "search_ticket_history" in Agent(DEMO_PRINCIPALS["agent_maya"]).toolbox.names()
