"""Access control, enforced in the data layer."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, TypeVar

from app.data.store import Account, Order, Store, Ticket, load_store


class Role(str, Enum):
    CUSTOMER = "customer"
    SUPPORT_AGENT = "support_agent"
    SUPPORT_MANAGER = "support_manager"


# Capabilities per role, checked by tools before acting.
CAPABILITIES: dict[Role, frozenset[str]] = {
    Role.CUSTOMER: frozenset({
        "read_own_account", "read_own_orders", "read_own_tickets",
        "search_policy", "request_escalation",
    }),
    Role.SUPPORT_AGENT: frozenset({
        "read_any_account", "read_any_orders", "read_any_tickets",
        "search_policy", "read_ticket_history", "request_escalation",
        "create_escalation", "update_ticket", "create_followup_task",
        "view_operations_dashboard",
    }),
    Role.SUPPORT_MANAGER: frozenset({
        "read_any_account", "read_any_orders", "read_any_tickets",
        "search_policy", "read_ticket_history", "request_escalation",
        "create_escalation", "update_ticket", "create_followup_task",
        "view_operations_dashboard", "approve_credit",
    }),
}


class AccessDenied(PermissionError):
    """Raised when a principal lacks a capability."""


@dataclass(frozen=True)
class Principal:
    """Who is asking. Mocked auth."""

    role: Role
    account_id: str | None = None  # required for CUSTOMER, ignored otherwise
    display_name: str = ""
    user_id: str = ""

    def __post_init__(self) -> None:
        if self.role is Role.CUSTOMER and not self.account_id:
            raise ValueError("a customer principal must be bound to an account_id")

    @property
    def is_internal(self) -> bool:
        return self.role in (Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER)

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES[self.role]

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        if not self.can(capability):
            raise AccessDenied(
                f"role '{self.role.value}' does not have capability '{capability}'"
            )

    @property
    def visible_account_ids(self) -> frozenset[str] | None:
        """None means unrestricted (internal). Otherwise the allowed set."""
        return None if self.is_internal else frozenset({self.account_id})  # type: ignore[arg-type]

    def may_see_account(self, account_id: str | None) -> bool:
        if account_id is None:
            return True
        allowed = self.visible_account_ids
        return allowed is None or account_id in allowed


T = TypeVar("T", Order, Ticket)


class ScopedStore:
    """The only way tools touch structured data. Scope is applied here, once."""

    def __init__(self, principal: Principal, store: Store | None = None) -> None:
        self._p = principal
        self._s = store or load_store()

    @property
    def principal(self) -> Principal:
        return self._p

    @property
    def now(self):
        return self._s.now

    @property
    def snapshot(self):
        return self._s.snapshot

    @property
    def raw(self) -> Store:
        """Unscoped store; internal principals only."""
        self._p.require("view_operations_dashboard")
        return self._s

    def _filter(self, items: Iterable[T]) -> list[T]:
        allowed = self._p.visible_account_ids
        if allowed is None:
            return list(items)
        return [i for i in items if i.account_id in allowed]

    def account(self, account_id: str) -> Account | None:
        if not self._p.may_see_account(account_id):
            return None
        return self._s.account(account_id)

    def resolve_account(self, ref: str | None) -> Account | None:
        """Resolve an account by id or customer name, within scope."""
        # Customers resolve to their own account regardless of the ref passed.
        if not self._p.is_internal:
            return self._s.account(self._p.account_id)  # type: ignore[arg-type]
        if not ref:
            return None
        return self._s.account(ref) or self._s.account_by_name(ref)

    def order(self, order_id: str) -> Order | None:
        o = self._s.orders.get(order_id)
        if o is None or not self._p.may_see_account(o.account_id):
            return None
        return o

    def orders(self, account_id: str | None = None) -> list[Order]:
        items = self._filter(self._s.orders.values())
        return [o for o in items if account_id is None or o.account_id == account_id]

    def ticket(self, ticket_id: str) -> Ticket | None:
        t = self._s.tickets.get(ticket_id)
        if t is None or not self._p.may_see_account(t.account_id):
            return None
        return t

    def tickets(self, account_id: str | None = None, open_only: bool = False) -> list[Ticket]:
        items = self._filter(self._s.tickets.values())
        if account_id is not None:
            items = [t for t in items if t.account_id == account_id]
        if open_only:
            items = [t for t in items if t.is_open]
        return items

    def accounts(self) -> list[Account]:
        allowed = self._p.visible_account_ids
        if allowed is None:
            return list(self._s.accounts.values())
        return [a for a in self._s.accounts.values() if a.account_id in allowed]


# Demo principals (mocked auth).
DEMO_PRINCIPALS: dict[str, Principal] = {
    "customer_northstar": Principal(Role.CUSTOMER, "ACCT-001", "Northstar Logistics", "cust-001"),
    "customer_lumenworks": Principal(Role.CUSTOMER, "ACCT-002", "LumenWorks", "cust-002"),
    "customer_beacon": Principal(Role.CUSTOMER, "ACCT-003", "Beacon Retail", "cust-003"),
    "customer_axis": Principal(Role.CUSTOMER, "ACCT-004", "Axis Labs", "cust-004"),
    "agent_maya": Principal(Role.SUPPORT_AGENT, None, "Maya (Support Agent)", "agent-maya"),
    "agent_rohit": Principal(Role.SUPPORT_AGENT, None, "Rohit (Support Agent)", "agent-rohit"),
    "manager_priya": Principal(Role.SUPPORT_MANAGER, None, "Priya Mehta (Support Manager)", "mgr-priya"),
}
