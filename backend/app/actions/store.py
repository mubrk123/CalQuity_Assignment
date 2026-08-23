"""State-changing actions: the model may only prepare; the UI commits."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.security.session import AccessDenied, Principal


class ActionKind(str, Enum):
    ESCALATION = "escalation"
    TICKET_UPDATE = "ticket_update"
    FOLLOWUP_TASK = "followup_task"
    SERVICE_CREDIT = "service_credit"


# Capability required to prepare each action kind.
REQUIRED_CAPABILITY = {
    ActionKind.ESCALATION: "request_escalation",
    ActionKind.TICKET_UPDATE: "update_ticket",
    ActionKind.FOLLOWUP_TASK: "create_followup_task",
    ActionKind.SERVICE_CREDIT: "request_escalation",
}


class ActionState(str, Enum):
    PENDING = "pending_confirmation"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class Action:
    action_id: str
    kind: ActionKind
    state: ActionState
    payload: dict[str, Any]
    preview: dict[str, Any]
    prepared_by: str
    prepared_by_role: str
    account_id: str | None
    prepared_at: datetime
    # Never leaves the server; not returned by any tool.
    _token: str = field(repr=False, default="")
    committed_at: datetime | None = None
    committed_by: str | None = None
    reference: str | None = None
    requires_manager_approval: bool = False
    blocked_reason: str | None = None

    def public(self, include_token: bool = False) -> dict:
        """Representation safe to return to the model."""
        d = {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "preview": self.preview,
            "account_id": self.account_id,
            "prepared_by": self.prepared_by,
            "prepared_at": self.prepared_at.strftime("%Y-%m-%d %H:%M"),
            "requires_manager_approval": self.requires_manager_approval,
            "reference": self.reference,
        }
        if self.blocked_reason:
            d["blocked_reason"] = self.blocked_reason
        if include_token:
            d["confirmation_token"] = self._token
        return d


class ActionStore:
    """In-memory action ledger, one instance per process."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}
        self._audit: list[dict] = []
        self._counter = {k: 0 for k in ActionKind}

    def prepare(
        self,
        principal: Principal,
        kind: ActionKind,
        payload: dict[str, Any],
        preview: dict[str, Any],
        account_id: str | None = None,
        requires_manager_approval: bool = False,
        now: datetime | None = None,
    ) -> Action:
        principal.require(REQUIRED_CAPABILITY[kind])

        if account_id and not principal.may_see_account(account_id):
            raise AccessDenied("cannot prepare an action against another account")

        action = Action(
            action_id=f"ACT-{secrets.token_hex(4).upper()}",
            kind=kind,
            state=ActionState.PENDING,
            payload=payload,
            preview=preview,
            prepared_by=principal.display_name or principal.user_id,
            prepared_by_role=principal.role.value,
            account_id=account_id,
            prepared_at=now or datetime.now(),
            _token=secrets.token_urlsafe(24),
            requires_manager_approval=requires_manager_approval,
        )

        # Recorded at preparation time so the UI can render the block up front.
        if requires_manager_approval and not principal.can("approve_credit"):
            action.blocked_reason = (
                "This action exceeds the approval limit for your role and needs a "
                "support manager to authorise it."
            )

        self._actions[action.action_id] = action
        self._log("prepared", action, principal)
        return action

    def confirmation_token(self, action_id: str) -> str:
        """Read the token. Called by the HTTP layer, never by a tool."""
        return self._actions[action_id]._token

    def commit(self, principal: Principal, action_id: str, token: str,
               now: datetime | None = None) -> Action:
        action = self._actions.get(action_id)
        if action is None:
            raise KeyError(f"unknown action {action_id}")
        if action.state is not ActionState.PENDING:
            raise ValueError(f"action {action_id} is already {action.state.value}")
        if not secrets.compare_digest(token, action._token):
            self._log("commit_rejected_bad_token", action, principal)
            raise PermissionError("invalid confirmation token")
        if action.blocked_reason and not principal.can("approve_credit"):
            raise AccessDenied(action.blocked_reason)
        if action.account_id and not principal.may_see_account(action.account_id):
            raise AccessDenied("cannot commit an action against another account")

        self._counter[action.kind] += 1
        prefix = {
            ActionKind.ESCALATION: "ESC",
            ActionKind.TICKET_UPDATE: "UPD",
            ActionKind.FOLLOWUP_TASK: "TASK",
            ActionKind.SERVICE_CREDIT: "CRD",
        }[action.kind]

        action.state = ActionState.COMMITTED
        action.committed_at = now or datetime.now()
        action.committed_by = principal.display_name or principal.user_id
        action.reference = f"{prefix}-{self._counter[action.kind]:04d}"
        action._token = ""  # single use
        self._log("committed", action, principal)
        return action

    def cancel(self, principal: Principal, action_id: str) -> Action:
        action = self._actions[action_id]
        if action.state is ActionState.PENDING:
            action.state = ActionState.CANCELLED
            action._token = ""
            self._log("cancelled", action, principal)
        return action

    def get(self, action_id: str) -> Action | None:
        return self._actions.get(action_id)

    def visible(self, principal: Principal) -> list[Action]:
        return [
            a for a in self._actions.values()
            if principal.may_see_account(a.account_id)
        ]

    def audit(self, principal: Principal) -> list[dict]:
        return [
            e for e in self._audit
            if principal.may_see_account(e.get("account_id"))
        ]

    def _log(self, event: str, action: Action, principal: Principal) -> None:
        self._audit.append({
            "event": event,
            "action_id": action.action_id,
            "kind": action.kind.value,
            "account_id": action.account_id,
            "actor": principal.display_name or principal.user_id,
            "actor_role": principal.role.value,
            "reference": action.reference,
            "at": datetime.now().isoformat(timespec="seconds"),
        })


ACTIONS = ActionStore()
