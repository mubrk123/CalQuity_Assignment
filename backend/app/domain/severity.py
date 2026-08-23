"""Severity classification against Support Policy v3 s2."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.sources import terms

SEVERITIES = ("P1", "P2", "P3")


@dataclass
class SeverityVerdict:
    severity: str
    criterion: str          # which policy criterion matched
    matched_terms: list[str]
    quote: str              # verbatim policy definition
    source_ref: str
    method: str             # "rules" | "llm"
    confidence: str         # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "criterion": self.criterion,
            "matched_terms": self.matched_terms,
            "policy_quote": self.quote,
            "source": self.source_ref,
            "method": self.method,
            "confidence": self.confidence,
        }


# P1 criteria, taken directly from the policy wording.
_P1_SECURITY = re.compile(
    r"\b(api[ -]?key|credential|password|secret|token|private key|"
    r"security incident|breach|exposed|exposure|leak(?:ed)?)\b", re.I
)
_P1_TOTAL_SCOPE = re.compile(r"\b(all|every|any|entire|complete|no one|nobody)\b", re.I)
_P1_CREATION = re.compile(
    r"\b(shipment creation|creat\w*\s+(?:a\s+|any\s+)?shipment|book\w*\s+shipment)\b", re.I
)
_P1_FAILURE = re.compile(r"\b(fail\w*|error|down|outage|500|unavailable|broken|cannot|can't|unable)\b", re.I)

_P2_MAJOR_FEATURE = re.compile(r"\b(bulk upload|bulk-upload|integration|api|webhook|report\w*|export)\b", re.I)

# Deliberately narrow: a workaround must restore the AFFECTED capability, so
# "can still be viewed" must not downgrade a shipment-creation outage.
_WORKAROUND = re.compile(
    r"(?:\bone[- ]by[- ]one\b|\bindividually\b|\bworkaround\b|\bstill works?\b|"
    r"\bcan still (?:create|book|ship|submit|upload)\b|"
    r"\bcreat\w*\s+\w*\s?shipments?\s+\w*\s?still works?\b|"
    r"\bremains? possible\b)", re.I
)

_P3_HOWTO = re.compile(
    r"\b(how (?:do|can|to)|where (?:do|can)|change|update|replace|configure|"
    r"configuration|setting|billing contact|add a user|rename)\b", re.I
)
_P3_MINOR = re.compile(r"\b(shows?|display\w*|still shows|label|typo|cosmetic|delay\w*|late)\b", re.I)


def _defs() -> dict:
    return terms.policy_defaults()["severity_definitions"]


def _verdict(sev: str, criterion: str, matched: list[str], confidence: str) -> SeverityVerdict:
    d = _defs()
    return SeverityVerdict(
        severity=sev,
        criterion=criterion,
        matched_terms=sorted(set(m.lower() for m in matched if m)),
        quote=d[sev]["quote"],
        source_ref=d["source_ref"],
        method="rules",
        confidence=confidence,
    )


def classify(subject: str, description: str = "") -> SeverityVerdict:
    """Classify a ticket's severity from its free text."""
    text = f"{subject} {description}".strip()

    # Suspicion alone is sufficient under the policy; do not require confirmation.
    if hits := _P1_SECURITY.findall(text):
        return _verdict(
            "P1",
            "confirmed security incident or suspected credential exposure",
            [h if isinstance(h, str) else h[0] for h in hits],
            "high",
        )

    scope = _P1_TOTAL_SCOPE.findall(text)
    creation = _P1_CREATION.findall(text)
    failure = _P1_FAILURE.findall(text)
    if scope and creation and failure and not _WORKAROUND.search(text):
        return _verdict(
            "P1",
            "complete production outage preventing all shipment creation",
            [*scope, *[c if isinstance(c, str) else c[0] for c in creation], *failure],
            "high",
        )

    feature = _P2_MAJOR_FEATURE.findall(text)
    if feature and failure:
        wa = _WORKAROUND.findall(text)
        return _verdict(
            "P2",
            "major feature unavailable or degraded, but core operations remain possible or a workaround exists",
            [*feature, *failure, *[w if isinstance(w, str) else w[0] for w in wa]],
            "high" if wa else "medium",
        )

    howto = _P3_HOWTO.findall(text)
    minor = _P3_MINOR.findall(text)
    if howto or minor:
        return _verdict(
            "P3",
            "minor defect, how-to question, configuration request, or limited operational impact",
            [*[h if isinstance(h, str) else h[0] for h in howto],
             *[m if isinstance(m, str) else m[0] for m in minor]],
            "high" if howto else "medium",
        )

    return _verdict(
        "P3",
        "no P1 or P2 criterion matched; defaulting to the lowest severity",
        [],
        "low",
    )
