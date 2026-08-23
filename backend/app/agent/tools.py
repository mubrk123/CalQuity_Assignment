"""The agent's tool surface: thin fact lookups and thick deterministic engines."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from app.actions.store import ACTIONS, REQUIRED_CAPABILITY, ActionKind
from app.config import (ASSUMPTION_NOTES, TIER_CURRENT_POLICY, TIER_LABELS,
                        TIER_TICKET_HISTORY)
from app.corpus.search import search, superseded_pairs
from app.data.store import Order, Ticket
from app.domain import cancellation, credit, known_issues, precedence, sla
from app.security.session import (AccessDenied, CAPABILITIES, Principal,
                                  ScopedStore)
from app.sources import terms

NOT_FOUND = {
    "found": False,
    "note": (
        "No such record is visible to you. If you believe it exists, it belongs "
        "to another account and cannot be accessed here."
    ),
    "scope_of_this_result": (
        "This concerns only the record requested. It does NOT mean the account "
        "has no orders or tickets. To list what an account has, call list_orders "
        "or lookup_tickets -- never infer emptiness from a single miss."
    ),
}

# Stems for derived draft titles.
ACTION_LABELS = {
    ActionKind.ESCALATION: "Escalation",
    ActionKind.TICKET_UPDATE: "Ticket update",
    ActionKind.FOLLOWUP_TASK: "Follow-up",
    ActionKind.SERVICE_CREDIT: "Service credit",
}

# What to ask for, in the caller's language, when a required argument is absent.
MISSING_ARGUMENT_PROMPTS = {
    "reason": "Ask the user why this is being raised, then call the tool again.",
    "query": "Decide what to search for, then call the tool again.",
    "kind": "Choose one of: escalation, ticket_update, followup_task, service_credit.",
}


def _nullable_optionals(parameters: dict) -> dict:
    """Allow null for every non-required parameter, since models emit explicit nulls."""
    props = parameters.get("properties") or {}
    if not props:
        return parameters
    required = set(parameters.get("required") or ())
    patched = {}
    for name, spec in props.items():
        t = spec.get("type")
        if name not in required and isinstance(t, str) and t != "null":
            spec = {**spec, "type": [t, "null"]}
        patched[name] = spec
    return {**parameters, "properties": patched}

def _allowed_kinds(principal: Principal) -> list[str]:
    """Action kinds this principal may prepare."""
    kinds = [k for k in ActionKind
             if principal.can(REQUIRED_CAPABILITY[k])]
    return [k.value for k in kinds] or ["escalation"]

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., dict]
    internal_only: bool = False

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _nullable_optionals(self.parameters),
            },
        }

def _order_view(o: Order, include_notes: bool) -> dict:
    d = {
        "order_id": o.order_id,
        "account_id": o.account_id,
        "carrier": o.carrier,
        "status": o.status,
        "booked_at": o.booked_at.strftime("%Y-%m-%d %H:%M") if o.booked_at else None,
        "pickup_window": (
            f"{o.pickup_window_start:%Y-%m-%d %H:%M} to {o.pickup_window_end:%H:%M}"
            if o.pickup_window_start and o.pickup_window_end else None
        ),
        "pickup_actual_at": o.pickup_actual_at.strftime("%Y-%m-%d %H:%M") if o.pickup_actual_at else None,
        "shipment_fee_inr": o.shipment_fee_inr,
        "carrier_fault": o.carrier_fault,
        "customer_fault": o.customer_fault,
        "cancellation_requested_at": (
            o.cancellation_requested_at.strftime("%Y-%m-%d %H:%M")
            if o.cancellation_requested_at else None
        ),
    }
    if include_notes:
        # Internal annotations on the dataset ("Customer asks to cancel"), not
        # customer-facing record data. Shown to a customer they read as the
        # topic of the question and steered the agent into answering something
        # that was never asked.
        d["notes"] = o.notes
    return d

def _ticket_view(t: Ticket, include_history: bool) -> dict:
    d = {
        "ticket_id": t.ticket_id,
        "account_id": t.account_id,
        "status": t.status,
        "subject": t.subject,
        "description": t.description,
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else None,
        "channel": t.channel,
        "assigned_to": t.assigned_to,
        "last_customer_message_at": (
            t.last_customer_message_at.strftime("%Y-%m-%d %H:%M")
            if t.last_customer_message_at else None
        ),
        "customer_followed_up": t.customer_followed_up,
    }
    if include_history and t.historical_resolution:
        d["historical_resolution"] = t.historical_resolution
        d["historical_resolution_reliability"] = (
            "UNVERIFIED CONTEXT. This is what an agent said at the time and may be "
            "wrong. It is not policy. Verify against the current policy or the "
            "customer's agreement before repeating it."
        )
    return d

class Toolbox:
    """Bound to one principal for the lifetime of a request."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.store = ScopedStore(principal)
        self.now = self.store.now
        # The account this turn is about, remembered from the last record lookup.
        self._focus: str | None = principal.account_id
        # Top source returned by each policy search this turn, to avoid resends.
        self._searched: dict[str, str] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._register_all()

    # -- registry ---------------------------------------------------------
    def _add(self, spec: ToolSpec) -> None:
        if spec.internal_only and not self.principal.is_internal:
            return
        self._specs[spec.name] = spec

    def schemas(self) -> list[dict]:
        return [s.schema() for s in self._specs.values()]

    def names(self) -> list[str]:
        return list(self._specs)

    def prime(self, user_message: str) -> dict | None:
        """Resolve the account this turn is about, before the model runs."""
        # Customers resolve to their own account regardless of the message.
        if not self.principal.is_internal:
            return self.t_get_account_context(None)

        text = user_message or ""
        for pattern, resolve in (
            (r"\b(ORD-\d+)\b", lambda ref: getattr(self.store.order(ref), "account_id", None)),
            (r"\b(TKT-\d+)\b", lambda ref: getattr(self.store.ticket(ref), "account_id", None)),
            (r"\b(ACCT-\d+)\b", lambda ref: ref),
        ):
            m = re.search(pattern, text, re.I)
            if m:
                acc_id = resolve(m.group(1).upper())
                if acc_id:
                    return self.t_get_account_context(acc_id)

        for acc in self.store.accounts():
            if acc.account_name.lower() in text.lower():
                return self.t_get_account_context(acc.account_id)
        return None

    def prime_records(self, user_message: str) -> list[tuple[str, str, dict]]:
        """Fetch the orders and tickets the message names by id, as (tool, ref, result)."""
        # Goes through the real handlers so results match model-requested ones.
        text = user_message or ""
        out: list[tuple[str, str, dict]] = []
        for ref in dict.fromkeys(m.upper() for m in re.findall(r"\bORD-\d+\b", text, re.I)):
            result = self.t_get_order(ref)
            if result.get("found"):
                out.append(("get_order", ref, result))
        for ref in dict.fromkeys(m.upper() for m in re.findall(r"\bTKT-\d+\b", text, re.I)):
            result = self.t_lookup_tickets(ticket_id=ref)
            if result.get("found"):
                out.append(("lookup_tickets", ref, result))
        return out

    def call(self, name: str, arguments: dict[str, Any]) -> dict:
        spec = self._specs.get(name)
        if spec is None:
            return {
                "error": f"unknown tool {name!r}",
                "available_tools": self.names(),
            }
        # Nulls mean "unused"; drop them so handler defaults apply.
        clean = {k: v for k, v in (arguments or {}).items() if v is not None}
        try:
            result = spec.handler(**clean)
        except AccessDenied as exc:
            return {"error": "access_denied", "detail": str(exc)}
        except TypeError as exc:
            missing = re.findall(r"'(\w+)'", str(exc)) if "required" in str(exc) else []
            out: dict = {"error": "missing_information", "missing": missing}
            guidance = [MISSING_ARGUMENT_PROMPTS[m] for m in missing
                        if m in MISSING_ARGUMENT_PROMPTS]
            out["what_to_do"] = " ".join(guidance) if guidance else (
                "Supply the values this tool needs and call it again. Do not "
                "mention parameter names to the user."
            )
            return out if missing else {"error": "bad_arguments", "detail": str(exc)}
        except Exception as exc:  # surfaced to the model so it can recover
            return {"error": type(exc).__name__, "detail": str(exc)}
        result.setdefault("_tool", name)
        return result

    # ==================================================================
    # THIN TOOLS
    # ==================================================================
    def t_search_policy(self, query: str, include_deprecated: bool = False,
                        limit: int = 5, account_ref: str | None = None) -> dict:
        # Narrow to the account in focus so another customer's agreement cannot win.
        focus = self._focus
        if account_ref:
            acc = self.store.resolve_account(account_ref)
            focus = acc.account_id if acc else focus
        hits = search(
            query, self.principal, limit=max(1, min(int(limit), 8)),
            include_deprecated=bool(include_deprecated),
            account_id=focus,
        )
        top = hits[0].source_ref if hits else "none"
        if top in self._searched.values():
            return {
                "query": query,
                "already_retrieved": top,
                "note": (
                    f"This returns {top}, which you already have. Nothing new is "
                    "available for this wording. Answer with what you have."
                ),
            }
        self._searched[query] = top

        return {
            "query": query,
            "results": [h.to_dict() for h in hits],
            "authority_order": (
                "Signed customer agreement > current policy/SOP > current product "
                "documentation. Deprecated documents and historical tickets are "
                "never authoritative (Support Policy v3 s1)."
            ),
            "note": (
                "No results matched. Try different wording, or the topic may not be "
                "covered by the supplied documents."
                if not hits else None
            ),
        }

    def t_get_account_context(self, account_ref: str | None = None) -> dict:
        acc = self.store.resolve_account(account_ref)
        if acc is None:
            return dict(NOT_FOUND, requested=account_ref)
        self._focus = acc.account_id
        prec = precedence.explain_for_account(acc, self.now)
        # Only the winning source per topic goes to the model; the UI gets the
        # full explanation via /api/context.
        governs = {
            topic: f"{info['source']}"
            + (" (agreement overrides policy)" if info["governed_by"] == "customer_agreement" else "")
            for topic, info in prec["topics"].items()
        }
        out = {
            "found": True,
            "account_id": acc.account_id,
            "account_name": acc.account_name,
            "plan": acc.plan,
            "status": acc.status,
            "has_signed_agreement": acc.has_contract,
            "which_source_governs_each_topic": governs,
        }
        if self.principal.is_internal:
            out["csm"] = acc.csm
            out["internal_notes"] = acc.notes
        if acc.premium_support:
            out["premium_support_flag"] = True
            out["premium_support_caveat"] = ASSUMPTION_NOTES["premium_support_undefined"]
        return out

    def t_get_order(self, order_id: str) -> dict:
        o = self.store.order(order_id)
        if o is None:
            return dict(NOT_FOUND, requested=order_id)
        self._focus = o.account_id
        out = {"found": True, "order": _order_view(o, self.principal.is_internal),
               "reference_time": self.now.strftime("%Y-%m-%d %H:%M %A")}
        # Known issues ride along with the record they affect.
        matches = known_issues.for_order(o, self.now)
        if matches:
            out["known_issues_affecting_this_order"] = [m.to_dict() for m in matches]
            out["before_you_answer"] = known_issues.workaround_note(matches)
        return out

    def t_list_orders(self, account_ref: str | None = None,
                      status: str | None = None) -> dict:
        acc = self.store.resolve_account(account_ref) if account_ref or not self.principal.is_internal else None
        orders = self.store.orders(acc.account_id if acc else None)
        if status:
            orders = [o for o in orders if o.status == status.upper()]
        return {
            "count": len(orders),
            "orders": [_order_view(o, self.principal.is_internal) for o in orders],
        }

    def t_lookup_tickets(self, ticket_id: str | None = None,
                         account_ref: str | None = None,
                         open_only: bool = False) -> dict:
        if ticket_id:
            t = self.store.ticket(ticket_id)
            if t is None:
                return dict(NOT_FOUND, requested=ticket_id)
            self._focus = t.account_id
            out = {"found": True,
                   "ticket": _ticket_view(t, self.principal.is_internal)}
            matches = known_issues.for_ticket(t, self.store.account(t.account_id))
            if matches:
                out["known_issues_that_may_explain_this"] = [m.to_dict() for m in matches]
                out["before_you_answer"] = known_issues.workaround_note(matches)
            return out
        acc = self.store.resolve_account(account_ref) if account_ref or not self.principal.is_internal else None
        tickets = self.store.tickets(acc.account_id if acc else None, open_only=bool(open_only))
        return {
            "count": len(tickets),
            "tickets": [_ticket_view(t, self.principal.is_internal) for t in tickets],
        }

    def t_list_orders(self, account_ref: str | None = None,
                      open_only: bool = False) -> dict:
        """List the shipments an account has."""
        acc = (self.store.resolve_account(account_ref)
               if account_ref or not self.principal.is_internal else None)
        orders = self.store.orders(acc.account_id if acc else None)
        if open_only:
            orders = [o for o in orders if o.status not in ("DELIVERED", "CANCELLED")]
        return {
            "count": len(orders),
            "account": acc.account_name if acc else "all accounts you may see",
            "orders": [_order_view(o, self.principal.is_internal) for o in orders],
        }

    def t_search_ticket_history(self, query: str, limit: int = 4) -> dict:
        """Internal only. Results are explicitly NOT authoritative."""
        self.principal.require("read_ticket_history")
        terms_ = [w for w in query.lower().split() if len(w) > 2]
        scored: list[tuple[int, Ticket]] = []
        for t in self.store.tickets():
            blob = f"{t.subject} {t.description} {t.historical_resolution or ''}".lower()
            score = sum(blob.count(w) for w in terms_)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda p: -p[0])
        return {
            "query": query,
            "authority": TIER_LABELS[TIER_TICKET_HISTORY],
            "reliability_warning": (
                "These are past tickets, provided as context only. Support Policy v3 s1 "
                "states historical tickets 'may contain incorrect past guidance'. Never "
                "cite a past resolution as the reason for an answer -- check the current "
                "policy or the customer's agreement instead. If a past resolution "
                "disagrees with current policy, say so explicitly."
            ),
            "count": len(scored[:limit]),
            "tickets": [_ticket_view(t, include_history=True) for _s, t in scored[:limit]],
        }

    # ==================================================================
    # THICK TOOLS -- deterministic engines
    # ==================================================================
    def t_evaluate_cancellation(self, order_id: str) -> dict:
        o = self.store.order(order_id)
        if o is None:
            return dict(NOT_FOUND, requested=order_id)
        acc = self.store.account(o.account_id)
        d = cancellation.assess(o, acc, self.now)
        return {"found": True, "order_id": order_id, **d.to_dict()}

    def t_evaluate_service_credit(self, order_id: str,
                                  delay_hours_override: float | None = None) -> dict:
        o = self.store.order(order_id)
        if o is None:
            return dict(NOT_FOUND, requested=order_id)
        acc = self.store.account(o.account_id)
        d = credit.assess(o, acc, self.now,
                          delay_hours_override=delay_hours_override)
        return {"found": True, "order_id": order_id, **d.to_dict()}

    def t_evaluate_sla(self, ticket_id: str | None = None,
                       account_ref: str | None = None) -> dict:
        """One ticket, or every open ticket in scope when no id is given."""
        if ticket_id:
            t = self.store.ticket(ticket_id)
            if t is None:
                return dict(NOT_FOUND, requested=ticket_id)
            acc = self.store.account(t.account_id)
            return {"found": True, **sla.assess_ticket(t, acc, self.now).to_dict()}

        # "Which tickets are past target?" is a question about the set. Looping
        # the engine locally costs microseconds; asking the model to loop costs
        # one HTTP round trip per ticket and runs into the per-tool budget.
        acc = (self.store.resolve_account(account_ref)
               if account_ref or not self.principal.is_internal else None)
        tickets = self.store.tickets(acc.account_id if acc else None, open_only=True)
        breached, within, not_started = [], [], []
        for t in tickets:
            a = sla.assess_ticket(t, self.store.account(t.account_id), self.now)
            ev = a.evaluation.to_dict()
            row = {
                "ticket_id": t.ticket_id,
                "account": a.account_name,
                "subject": t.subject,
                "severity": a.severity.severity,
                "target": ev["target"],
                "elapsed": ev["elapsed"],
                "outcome": a.decision.outcome,
                "finding": a.decision.summary,
            }
            if ev["breached"]:
                breached.append(row)
            elif not ev["clock_started"]:
                not_started.append(row)
            else:
                within.append(row)
        return {
            "found": True,
            "scope": acc.account_name if acc else "all open tickets you may see",
            "assessed": len(tickets),
            "breached": breached,
            "within_target": within,
            "clock_not_started": not_started,
            "note": (
                f"Every open ticket in scope was assessed ({len(tickets)} of them). "
                "This is the complete picture, nothing was skipped. A ticket under "
                "clock_not_started is outside its account's support coverage right "
                "now, so its target has not begun."
            ),
        }

    def t_get_source_reliability(self) -> dict:
        return {
            "authority_hierarchy": [
                {"rank": 1, "source": "Signed customer agreement"},
                {"rank": 2, "source": "Current support policy / SOP"},
                {"rank": 3, "source": "Current product documentation"},
                {"rank": "excluded", "source": "Deprecated documents"},
                {"rank": "never authoritative", "source": "Historical tickets"},
            ],
            "mandated_by": "Support Policy v3 s1",
            "documents": superseded_pairs(),
            "documented_assumptions": ASSUMPTION_NOTES,
            "reference_time": self.now.strftime("%Y-%m-%d %H:%M %A") + " (dataset snapshot)",
        }

    # ==================================================================
    # STATE-CHANGING -- prepare only. Commit is not a model capability.
    # ==================================================================
    def t_prepare_action(self, kind: str, reason: str,
                         summary: str | None = None,
                         ticket_id: str | None = None,
                         order_id: str | None = None,
                         account_ref: str | None = None,
                         new_status: str | None = None,
                         amount_inr: float | None = None,
                         due: str | None = None) -> dict:
        try:
            action_kind = ActionKind(kind)
        except ValueError:
            return {"error": "unknown_kind", "valid_kinds": [k.value for k in ActionKind]}

        account_id: str | None = None
        subject_label = None
        if ticket_id:
            t = self.store.ticket(ticket_id)
            if t is None:
                return dict(NOT_FOUND, requested=ticket_id)
            account_id, subject_label = t.account_id, f"{t.ticket_id}: {t.subject}"
        elif order_id:
            o = self.store.order(order_id)
            if o is None:
                return dict(NOT_FOUND, requested=order_id)
            account_id, subject_label = o.account_id, f"{o.order_id} ({o.status})"
        elif account_ref:
            acc_ref = self.store.resolve_account(account_ref)
            if acc_ref is None:
                return dict(NOT_FOUND, requested=account_ref)
            account_id, subject_label = acc_ref.account_id, acc_ref.account_name
        elif not self.principal.is_internal:
            account_id = self.principal.account_id

        acc = self.store.account(account_id) if account_id else None

        # The approval threshold comes from the quote-verified terms, never a literal.
        approval = terms.policy_defaults()["approval_and_uncertainty"]
        limit = approval["manager_approval_above_inr"]
        needs_manager = bool(amount_inr and amount_inr > limit)

        if not summary:
            bits = [ACTION_LABELS.get(action_kind, action_kind.value.replace("_", " "))]
            if amount_inr:
                bits.append(f"INR {amount_inr:,.0f}")
            if acc:
                bits.append(f"- {acc.account_name}")
            if subject_label and (ticket_id or order_id):
                bits.append(f"({ticket_id or order_id})")
            summary = " ".join(bits)

        preview = {
            "title": summary,
            "kind": action_kind.value,
            "account": acc.account_name if acc else None,
            "subject": subject_label,
            "reason": reason,
            "new_status": new_status,
            "amount_inr": amount_inr,
            "due": due,
        }
        if not self.principal.is_internal:
            # `reason` carries internal vocabulary; withhold it from the customer's
            # preview. It is still stored on the action for staff and the audit trail.
            preview.pop("reason", None)
        action = ACTIONS.prepare(
            self.principal, action_kind,
            payload={"ticket_id": ticket_id, "order_id": order_id,
                     "new_status": new_status, "amount_inr": amount_inr,
                     "due": due, "reason": reason},
            preview={k: v for k, v in preview.items() if v is not None},
            account_id=account_id,
            requires_manager_approval=needs_manager,
            now=self.now,
        )
        unchanged: dict[str, str] = {}
        matches: list = []
        if ticket_id:
            t = self.store.ticket(ticket_id)
            if t:
                matches = known_issues.for_ticket(t, acc)
                unchanged = {"ticket_status": t.status}
        elif order_id:
            o = self.store.order(order_id)
            if o:
                matches = known_issues.for_order(o, self.now)
                unchanged = {"order_status": o.status}

        out = {
            "prepared": True,
            **action.public(),
            "next_step": (
                "NOTHING HAS BEEN CREATED YET. Show this draft to the user and ask "
                "them to confirm. Confirmation happens through the interface -- you "
                "cannot execute it yourself, and you have no tool that can."
            ),
        }
        # Run the governing engine so the draft carries its own evidence.
        citations: list[dict] = []
        entitlement = self._entitlement(action_kind, ticket_id, order_id)
        if entitlement:
            citations.extend(entitlement.pop("citations", []))
            out["entitlement_check"] = entitlement

        if amount_inr is not None:
            # Return the governing clause with the verdict so the model does not
            # go searching for the approval limit itself.
            out["approval_limit"] = {
                "amount_requested_inr": amount_inr,
                "manager_approval_above_inr": limit,
                "verdict": (
                    "exceeds the approval limit" if needs_manager
                    else "within the approval limit"
                ),
            }
            citations.append({
                "source": approval["source_ref"],
                "authority": TIER_LABELS[TIER_CURRENT_POLICY],
                "tier": TIER_CURRENT_POLICY,
                "quote": approval["manager_approval_quote"],
            })

            owed = (entitlement or {}).get("entitled_amount_inr")
            if owed is not None and amount_inr > owed:
                out["exceeds_entitlement"] = (
                    f"Requested INR {amount_inr:,.0f} but the governing clause "
                    f"entitles them to INR {owed:,.0f}. Say so."
                )
        if citations:
            out["citations"] = citations
        if unchanged:
            # State the record's CURRENT value so the model does not narrate its
            # own proposal as history.
            out["record_state_unchanged"] = unchanged
        if matches:
            out["documented_issue_may_explain_this"] = [m.to_dict() for m in matches]
            out["reconsider"] = known_issues.workaround_note(matches)
        return out

    def _entitlement(self, kind: ActionKind, ticket_id: str | None,
                     order_id: str | None) -> dict | None:
        """Run the engine that governs this kind of action, if one does."""
        if kind is ActionKind.SERVICE_CREDIT and order_id:
            o = self.store.order(order_id)
            if o is None:
                return None
            d = credit.assess(o, self.store.account(o.account_id), self.now)
            return {
                "engine": "evaluate_service_credit",
                "order_id": order_id,
                "outcome": d.outcome,
                "finding": d.summary,
                "entitled_amount_inr": d.detail.get("credit_inr"),
                "verify_before_acting": d.verify_before_acting,
                "citations": [c.to_dict() for c in d.citations],
            }

        if kind is ActionKind.ESCALATION and ticket_id:
            t = self.store.ticket(ticket_id)
            if t is None:
                return None
            a = sla.assess_ticket(t, self.store.account(t.account_id), self.now)
            return {
                "engine": "evaluate_sla",
                "ticket_id": ticket_id,
                "outcome": a.decision.outcome,
                "finding": a.decision.summary,
                "severity": a.severity.severity,
                "target": a.evaluation.to_dict()["target"],
                "target_breached": a.evaluation.breached,
                "citations": [c.to_dict() for c in a.decision.citations],
            }
        return None

    def t_my_permissions(self) -> dict:
        """What this caller may do, read from the map that enforces it."""
        # Asked "am I allowed to see this?", the model answered from the role
        # name in its prompt and told a support agent they had "full access".
        # They cannot authorise a credit over the approval limit. Permissions
        # are now reported from CAPABILITIES, the same structure the checks use.
        caps = CAPABILITIES[self.principal.role]
        limit = terms.policy_defaults()["approval_and_uncertainty"]["manager_approval_above_inr"]
        can, cannot = [], []
        for label, cap in (
            ("see every account's records", "read_any_account"),
            ("see only their own account's records", "read_own_account"),
            ("read past ticket history (unverified)", "read_ticket_history"),
            ("see the operations dashboard", "view_operations_dashboard"),
            ("draft an escalation", "request_escalation"),
            ("draft a ticket update", "update_ticket"),
            ("draft a follow-up task", "create_followup_task"),
            (f"authorise a credit above INR {limit:,}", "approve_credit"),
        ):
            (can if cap in caps else cannot).append(label)
        return {
            "user": self.principal.display_name or self.principal.user_id,
            "role": self.principal.role.value.replace("_", " "),
            "account": self.principal.account_id,
            "can": can,
            "cannot": cannot,
            "always_true": (
                "Every action is a draft until a human confirms it in the "
                "interface. No role can make this assistant execute one."
            ),
        }

    def t_list_actions(self) -> dict:
        """Escalations, credits and updates this principal can see."""
        acts = ACTIONS.visible(self.principal)
        rows = []
        for a in acts:
            row = {
                "kind": a.kind.value,
                "status": ("CREATED - this exists" if a.reference
                           else f"draft only, never confirmed ({a.state.value})"),
                # Customer-facing case number; the internal action_id is dropped
                # for everyone by compact.py.
                "reference": a.reference,
                "raised": a.prepared_at.strftime("%Y-%m-%d %H:%M"),
                "about": (a.preview or {}).get("subject"),
                "title": (a.preview or {}).get("title"),
            }
            if self.principal.is_internal:
                row["account"] = (a.preview or {}).get("account")
                row["raised_by"] = a.prepared_by
                if a.requires_manager_approval:
                    row["needs_manager_approval"] = True
            rows.append(row)
        rows.sort(key=lambda r: r["raised"], reverse=True)
        created = [r for r in rows if r["reference"]]
        return {
            "created_count": len(created),
            "draft_count": len(rows) - len(created),
            "actions": rows,
            "note": (
                f"{len(created)} actually created, {len(rows) - len(created)} "
                "still unconfirmed drafts. Report everything above; if a request "
                "was created, say so and give its reference. An empty list means "
                "nothing has ever been raised on this account."
            ),
        }

    # ==================================================================
    def _register_all(self) -> None:
        S = lambda **kw: {"type": "object", **kw}  # noqa: E731

        self._add(ToolSpec(
            "search_policy_documents",
            "Search policies, SOPs, product docs, known issues, and agreements you may see. Excludes deprecated docs.",
            S(properties={
                "query": {"type": "string"},
                "include_deprecated": {"type": "boolean"},
                "limit": {"type": "integer"},
                "account_ref": {"type": "string"},
            }, required=["query"]),
            self.t_search_policy,
        ))

        self._add(ToolSpec(
            "get_account_context",
            "Account plan, whether it has a signed agreement, and which source governs each topic.",
            S(properties={
                "account_ref": {"type": "string"},
            }),
            self.t_get_account_context,
        ))

        self._add(ToolSpec(
            "get_order",
            "One shipment: status, carrier, times, fee, fault flags, any known issue affecting it.",
            S(properties={"order_id": {"type": "string"}}, required=["order_id"]),
            self.t_get_order,
        ))

        self._add(ToolSpec(
            "my_permissions",
            "What the person you are talking to is allowed to do. Use for 'am I allowed to', 'can I approve this', 'what access do I have' - never answer those from their role name.",
            S(properties={}),
            self.t_my_permissions,
        ))

        self._add(ToolSpec(
            "list_actions",
            "ALL escalations, credits and updates on this account - created and draft alike. Use for 'did that go through', 'status of my escalation', 'what is pending'. Takes no arguments; returns everything.",
            S(properties={}),
            self.t_list_actions,
        ))

        self._add(ToolSpec(
            "list_orders",
            "An account's shipments. Use this for 'what are my orders' - never guess an order id.",
            S(properties={
                "account_ref": {"type": "string"},
                "open_only": {"type": "boolean"},
            }),
            self.t_list_orders,
        ))

        self._add(ToolSpec(
            "lookup_tickets",
            "One ticket by id, or an account's tickets.",
            S(properties={
                "ticket_id": {"type": "string"},
                "account_ref": {"type": "string"},
                "open_only": {"type": "boolean"},
            }),
            self.t_lookup_tickets,
        ))

        self._add(ToolSpec(
            "search_ticket_history",
            "Past tickets. UNVERIFIED context, may contain wrong guidance. Never cite as a reason.",
            S(properties={
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            }, required=["query"]),
            self.t_search_ticket_history,
            internal_only=True,
        ))

        self._add(ToolSpec(
            "evaluate_cancellation",
            "Authoritative cancellation decision and fee for an order.",
            S(properties={"order_id": {"type": "string"}}, required=["order_id"]),
            self.t_evaluate_cancellation,
        ))

        self._add(ToolSpec(
            "evaluate_service_credit",
            "Authoritative failed-pickup credit decision and amount. delay_hours_override for a stated delay.",
            S(properties={
                "order_id": {"type": "string"},
                "delay_hours_override": {"type": "number"},
            }, required=["order_id"]),
            self.t_evaluate_service_credit,
        ))

        self._add(ToolSpec(
            "evaluate_sla",
            "Authoritative severity, response target and breach status. Pass ticket_id for one ticket. Pass NOTHING to assess every open ticket at once - use that for 'which tickets are past target', never loop over tickets one at a time.",
            S(properties={"ticket_id": {"type": "string"},
                          "account_ref": {"type": "string"}}),
            self.t_evaluate_sla,
        ))

        
        self._add(ToolSpec(
            "prepare_action",
            "Draft an action for the user to confirm: escalation, ticket_update, followup_task, service_credit. Creates a DRAFT ONLY. Also reports whether the amount needs manager approval and whether a known issue already covers it - call it rather than searching for approval limits.",
            S(properties={
                "kind": {"type": "string", "enum": _allowed_kinds(self.principal)},
                "reason": {"type": "string",
                           "description": "Why this is being raised, in the user's own terms."},
                "summary": {"type": "string",
                            "description": "Optional title. Omit it and one is composed for you."},
                "ticket_id": {"type": "string"},
                "order_id": {"type": "string"},
                "account_ref": {"type": "string",
                                "description": "Account name or id, when no ticket or order is named."},
                "new_status": {"type": "string"},
                "amount_inr": {"type": "number",
                               "description": "Credit amount. Pass it when one is named."},
                "due": {"type": "string"},
            }, required=["kind", "reason"]),
            self.t_prepare_action,
        ))

        