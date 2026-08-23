"""Shared result shapes. Every decision carries its citations and assumptions."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import ASSUMPTION_NOTES, TIER_LABELS


@dataclass
class Citation:
    source_ref: str
    tier: int
    quote: str

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "Unknown")

    def to_dict(self) -> dict:
        return {
            "source": self.source_ref,
            "authority": self.tier_label,
            "tier": self.tier,
            "quote": self.quote,
        }


@dataclass
class Decision:
    """A deterministic outcome plus the audit trail that produced it."""

    outcome: str
    summary: str
    detail: dict = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    assumption_keys: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    requires_manager_approval: bool = False
    escalate: bool = False
    escalation_reason: str | None = None
    verify_before_acting: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    def cite(self, source_ref: str, tier: int, quote: str) -> None:
        if not any(c.source_ref == source_ref and c.quote == quote for c in self.citations):
            self.citations.append(Citation(source_ref, tier, quote))

    def assume(self, key: str) -> None:
        if key and key not in self.assumption_keys:
            self.assumption_keys.append(key)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "detail": self.detail,
            "reasoning": self.reasoning,
            "citations": [c.to_dict() for c in self.citations],
            "assumptions": [ASSUMPTION_NOTES[k] for k in self.assumption_keys if k in ASSUMPTION_NOTES],
            "requires_confirmation": self.requires_confirmation,
            "requires_manager_approval": self.requires_manager_approval,
            "escalate": self.escalate,
            "escalation_reason": self.escalation_reason,
            "verify_before_acting": self.verify_before_acting,
            "conflicts": self.conflicts,
        }
