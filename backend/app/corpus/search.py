"""Authority-filtered BM25 retrieval over the policy corpus."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from rank_bm25 import BM25Okapi

from app.config import (
    AUTHORITATIVE_TIERS,
    CORPUS_FILE,
    TIER_DEPRECATED,
    TIER_LABELS,
)
from app.security.session import Principal

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Domain synonyms, to offset keyword search's brittleness to vocabulary.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "cancel": ("cancellation", "cancelled"),
    "cancellation": ("cancel", "cancelled"),
    "fee": ("charge", "cost"),
    "credit": ("refund", "compensation"),
    "sla": ("response", "target", "first-response"),
    "refund": ("credit",),
    "late": ("delay", "delayed", "past"),
    "delay": ("late", "delayed"),
    "pickup": ("collection", "collected", "picked"),
    "severity": ("p1", "p2", "p3", "critical", "high", "normal"),
    "outage": ("down", "unavailable", "failing"),
    "csv": ("bulk", "upload"),
    "bulk": ("csv", "upload"),
    "escalate": ("escalation",),
    "waive": ("waiver", "waives", "waived"),
}


def _stem(token: str) -> str:
    """Strip a plural 's'."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _tokenise(text: str) -> list[str]:
    """Tokenise, also emitting hyphen parts and stems."""
    # Must be applied identically to corpus and query.
    out: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        out.append(token)
        if "-" in token:
            out.extend(p for p in token.split("-") if p)
    return out + [_stem(t) for t in out if _stem(t) != t]


def _expand(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for t in tokens:
        out.extend(SYNONYMS.get(t, ()))
    return out


@dataclass
class Hit:
    chunk_id: str
    source_ref: str
    doc_title: str
    section: str
    text: str
    authority_tier: int
    status: str
    effective: str | None
    account_scope: str | None
    score: float

    @property
    def authority_label(self) -> str:
        return TIER_LABELS.get(self.authority_tier, "Unknown")

    def to_dict(self) -> dict:
        d = {
            "source": self.source_ref,
            "authority": self.authority_label,
            "tier": self.authority_tier,
            "document": self.doc_title,
            "section": self.section,
            "effective": self.effective,
            "text": self.text,
            "relevance": round(self.score, 3),
        }
        if self.authority_tier == TIER_DEPRECATED:
            d["warning"] = (
                "DEPRECATED source. Not current policy. Use only to explain what "
                "changed, never as the basis for an answer."
            )
        return d


@lru_cache(maxsize=1)
def _index() -> tuple[list[dict], BM25Okapi]:
    if not CORPUS_FILE.exists():
        raise RuntimeError(f"{CORPUS_FILE} missing. Run: python -m app.corpus.build")
    chunks = json.loads(CORPUS_FILE.read_text())["chunks"]
    corpus = [_tokenise(f"{c['source_ref']} {c['section']} {c['text']}") for c in chunks]
    return chunks, BM25Okapi(corpus)


def visible_chunks(principal: Principal, include_deprecated: bool = False) -> list[dict]:
    """The chunks this principal is permitted to see, before ranking."""
    chunks, _ = _index()
    out = []
    for c in chunks:
        if c["authority_tier"] == TIER_DEPRECATED and not include_deprecated:
            continue
        # Contract chunks are account-scoped.
        if c["account_scope"] and not principal.may_see_account(c["account_scope"]):
            continue
        out.append(c)
    return out


def search(
    query: str,
    principal: Principal,
    limit: int = 5,
    include_deprecated: bool = False,
    account_id: str | None = None,
) -> list[Hit]:
    """Rank authoritative policy/contract passages for a query."""
    principal.require("search_policy")
    chunks, bm25 = _index()
    allowed = {c["chunk_id"] for c in visible_chunks(principal, include_deprecated)}

    # Drop other customers' agreements from the ranking when an account is named.
    if account_id:
        allowed = {
            c["chunk_id"]
            for c in chunks
            if c["chunk_id"] in allowed
            and (c["account_scope"] is None or c["account_scope"] == account_id)
        }

    q_tokens = _tokenise(query)
    scores = bm25.get_scores(_expand(q_tokens))

    # Section-heading boost, to offset BM25's length normalisation penalising
    # long definitional chunks.
    q_set = set(q_tokens) | set(_expand(q_tokens))
    boosted = []
    for s, c in zip(scores, chunks):
        if c["chunk_id"] not in allowed or s <= 0:
            continue
        heading = set(_tokenise(c["section"]))
        overlap = len(q_set & heading)
        boosted.append((s * (1.0 + 0.35 * overlap), c))

    ranked = sorted(boosted, key=lambda pair: (-pair[0], pair[1]["authority_tier"]))

    # Tie-break by authority (Support Policy v3 s1).
    ranked.sort(key=lambda pair: (-round(pair[0], 4), pair[1]["authority_tier"]))

    return [
        Hit(
            chunk_id=c["chunk_id"],
            source_ref=c["source_ref"],
            doc_title=c["doc_title"],
            section=c["section"],
            text=c["text"],
            authority_tier=c["authority_tier"],
            status=c["status"],
            effective=c["effective"],
            account_scope=c["account_scope"],
            score=float(s),
        )
        for s, c in ranked[:limit]
    ]


def superseded_pairs() -> list[dict]:
    """Document supersession chains."""
    chunks, _ = _index()
    seen: dict[str, dict] = {}
    for c in chunks:
        if c["doc_id"] in seen:
            continue
        seen[c["doc_id"]] = {
            "doc": c["doc_title"],
            "status": c["status"],
            "tier": c["authority_tier"],
            "effective": c["effective"],
            "supersedes": c["supersedes"],
            "superseded_by": c["superseded_by"],
        }
    return list(seen.values())


def authoritative_only(hits: list[Hit]) -> list[Hit]:
    return [h for h in hits if h.authority_tier in AUTHORITATIVE_TIERS]
