"""The agent loop, emitting typed events for tool visibility."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from app.agent.compact import serialise_for_model
from app.agent.llm import (
    AssistantTurn, LLMClient, LLMConfig, LLMUnavailable, ToolCallRejected,
)
from app.agent.prompts import continuation_prompt, system_prompt
from app.agent.tools import Toolbox
from app.data.store import load_store
from app.security.session import Principal

MAX_ITERATIONS = 8

# Cap per tool NAME per turn, regardless of arguments.
TOOL_BUDGET = {
    "search_policy_documents": 4,
    "search_ticket_history": 3,
    "get_order": 4,
    "lookup_tickets": 4,
    "list_orders": 3,
    "get_account_context": 2,
}
DEFAULT_TOOL_BUDGET = 3


# Tools whose result is a finished Decision.
_DECIDING_TOOLS = (
    "evaluate_cancellation", "evaluate_service_credit", "evaluate_sla",
)


def salvage_answer(tool_calls: list[dict]) -> str | None:
    """Build an answer from engine output when the model cannot write one."""
    lines: list[str] = []
    for call in tool_calls:
        result = call.get("result") or {}
        if call.get("name") in _DECIDING_TOOLS:
            summary = result.get("summary")
            if summary and summary not in lines:
                lines.append(summary)
        elif call.get("name") == "prepare_action" and result.get("prepared"):
            title = (result.get("preview") or {}).get("title") or "An action"
            note = f"Drafted for you to confirm below: **{title}**."
            if result.get("blocked_reason"):
                note += f" {result['blocked_reason']}"
            lines.append(note)

    if not lines:
        return None
    lines.append(
        "_(The service that writes these replies was briefly unavailable, so "
        "this is the raw finding from the policy engines. The verdicts and "
        "citations above are unaffected.)_"
    )
    return "\n\n".join(lines)


@dataclass
class Event:
    type: str  # status | tool_call | tool_result | message | error | done
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}


@dataclass
class TurnRecord:
    """What happened in one user turn."""
    tool_calls: list[dict] = field(default_factory=list)
    iterations: int = 0
    elapsed_ms: int = 0
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    pending_action_ids: list[str] = field(default_factory=list)
    # Provider's reason, set when the answer came from the engines instead.
    degraded: str | None = None


class Agent:
    def __init__(self, principal: Principal, config: LLMConfig | None = None) -> None:
        self.principal = principal
        self.store = load_store()
        self.toolbox = Toolbox(principal)
        self.client = LLMClient(config)

    # -- public ------------------------------------------------------------
    def system_message(self) -> dict:
        return {"role": "system", "content": system_prompt(self.principal, self.store)}

    def continuation_message(self) -> dict:
        """Lean system message for iterations that already have tool results."""
        return {"role": "system", "content": continuation_prompt(self.store)}

    def run(self, user_message: str, history: list[dict] | None = None) -> Iterator[Event]:
        """Run one user turn, yielding events as they happen."""
        started = time.monotonic()
        record = TurnRecord(model=self.client.describe)

        messages: list[dict] = [self.system_message()]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        schemas = self.toolbox.schemas()
        used: dict[str, int] = {}
        nudged = False
        model_called = False

        # Resolve the governing sources before the model runs and hand them over
        # as an ordinary tool result.
        primed = self.toolbox.prime(user_message)
        if primed and primed.get("found"):
            yield Event("tool_call", {
                "id": "primed", "name": "get_account_context",
                "arguments": {"account_ref": primed.get("account_name")},
            })
            yield Event("tool_result", {
                "id": "primed", "name": "get_account_context", "result": primed,
            })
            record.tool_calls.append({
                "name": "get_account_context",
                "arguments": {"account_ref": primed.get("account_name")},
                "result": primed,
            })
            messages.append({
                "role": "user",
                "content": (
                    "Context resolved for this question, already verified -- do "
                    "not look it up again:\n" + serialise_for_model(primed)
                ),
            })

        # Records the message names by id, fetched up front for the same reason.
        records = self.toolbox.prime_records(user_message)
        for tool, ref, result in records:
            args = {"order_id": ref} if tool == "get_order" else {"ticket_id": ref}
            yield Event("tool_call", {"id": f"primed:{ref}", "name": tool,
                                      "arguments": args})
            yield Event("tool_result", {"id": f"primed:{ref}", "name": tool,
                                        "result": result})
            record.tool_calls.append({"name": tool, "arguments": args,
                                      "result": result})
        if records:
            messages.append({
                "role": "user",
                "content": (
                    "Records you named, already fetched -- do not look them up "
                    "again:\n" + serialise_for_model(
                        {ref: result for _, ref, result in records})
                ),
            })

        for iteration in range(1, MAX_ITERATIONS + 1):
            record.iterations = iteration
            yield Event("status", {"stage": "thinking", "iteration": iteration})

            # Swap the full ruleset for the lean one only once the MODEL itself
            # has called a tool; primed context does not count.
            if model_called:
                messages[0] = self.continuation_message()

            try:
                turn: AssistantTurn = self.client.chat(messages, schemas)
            except ToolCallRejected:
                # Nudge once, then give up gracefully.
                if nudged:
                    yield Event("message", {"content": (
                        "I couldn't complete that reliably. Rather than guess, I'd "
                        "suggest passing it to the support team - would you like me "
                        "to draft that?"
                    )})
                    yield Event("done", {"record": asdict(record), "messages": messages[1:]})
                    return
                nudged = True
                messages.append({"role": "system", "content": (
                    "Your last tool call was rejected as invalid. Supply only the "
                    "parameters you actually need, as strings, and omit any you do "
                    "not use rather than sending null."
                )})
                continue
            except LLMUnavailable as exc:
                # The engines have already decided; fall back to their sentences.
                salvaged = salvage_answer(record.tool_calls)
                if salvaged:
                    messages.append({"role": "assistant", "content": salvaged})
                    record.elapsed_ms = int((time.monotonic() - started) * 1000)
                    record.degraded = str(exc)
                    yield Event("message", {"content": salvaged, "degraded": True})
                    yield Event("done", {"record": asdict(record), "messages": messages[1:]})
                    return
                yield Event("error", {"kind": "llm_unavailable", "detail": str(exc)})
                return

            if turn.usage:
                for k, v in turn.usage.items():
                    if isinstance(v, int):
                        record.usage[k] = record.usage.get(k, 0) + v

            if not turn.wants_tools:
                text = (turn.content or "").strip()
                if not text:
                    text = (
                        "I wasn't able to produce an answer for that. Could you "
                        "rephrase, or would you like me to raise it with the support "
                        "team?"
                    )
                messages.append({"role": "assistant", "content": text})
                record.elapsed_ms = int((time.monotonic() - started) * 1000)
                yield Event("message", {"content": text})
                yield Event("done", {"record": asdict(record), "messages": messages[1:]})
                return

            # The tool-requesting turn must be echoed back verbatim, including
            # provider-specific fields, so the client supplies the message.
            messages.append(turn.replay_message())

            model_called = True
            for tc in turn.tool_calls:
                used[tc.name] = used.get(tc.name, 0) + 1
                budget = TOOL_BUDGET.get(tc.name, DEFAULT_TOOL_BUDGET)

                yield Event("tool_call", {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                })

                if used[tc.name] > budget:
                    result: dict = {
                        "error": "tool_budget_exhausted",
                        "detail": (
                            f"You have already called {tc.name} {budget} times this "
                            "turn. Rewording the query will not return anything new. "
                            "Answer with what you have, or say what is missing and "
                            "offer to escalate."
                        ),
                    }
                else:
                    result = self.toolbox.call(tc.name, tc.arguments)

                if result.get("prepared") and result.get("action_id"):
                    record.pending_action_ids.append(result["action_id"])

                record.tool_calls.append({
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "result": result,
                })
                yield Event("tool_result", {
                    "id": tc.id,
                    "name": tc.name,
                    "result": result,
                })
                # The UI got the full result above; the model gets a lean view.
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": serialise_for_model(result),
                })

        # Iteration cap reached.
        record.elapsed_ms = int((time.monotonic() - started) * 1000)
        text = salvage_answer(record.tool_calls) or (
            "I've gathered a lot of information but wasn't able to settle this "
            "cleanly. Rather than guess, I'd recommend passing it to the support "
            "team -- would you like me to draft that escalation?"
        )
        messages.append({"role": "assistant", "content": text})
        yield Event("message", {"content": text})
        yield Event("done", {
            "record": asdict(record),
            "messages": messages[1:],
            "note": "iteration cap reached",
        })

    def run_sync(self, user_message: str, history: list[dict] | None = None) -> dict:
        """Convenience wrapper: run to completion and return the outcome."""
        answer, events = "", []
        record: dict = {}
        out_messages: list[dict] = []
        for ev in self.run(user_message, history):
            events.append(ev.to_dict())
            if ev.type == "message":
                answer = ev.data["content"]
            elif ev.type == "done":
                record = ev.data.get("record", {})
                out_messages = ev.data.get("messages", [])
            elif ev.type == "error":
                answer = f"[{ev.data.get('kind')}] {ev.data.get('detail')}"
        return {
            "answer": answer,
            "events": events,
            "record": record,
            "messages": out_messages,
        }
