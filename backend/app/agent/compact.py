"""Shrink tool results before they go back to the model; the UI keeps the full object."""
from __future__ import annotations

from typing import Any

QUOTE_CHARS = 120
TEXT_CHARS = 330
MAX_SEARCH_RESULTS = 4
MAX_LIST_ITEMS = 8
HARD_CAP = 3500

# Keys the model has no use for. action_id is withheld so it cannot leak into
# customer-facing prose; the confirm card gets it from the `done` event.
DROP_KEYS = {"_tool", "relevance", "document", "authority_order", "tier_label",
             "action_id", "prepared_by", "prepared_at"}

# Compressed stand-ins for config.ASSUMPTION_NOTES.
ASSUMPTION_LABELS = {
    "business_hours": "business hours assumed 09:00-18:00 IST Mon-Fri (not defined in the sources)",
    "business_day": "1 business day read as 9 business hours (not defined in the sources)",
    "enterprise_p2_coverage": "Enterprise P2 '2 hours' read as clock hours, not business hours",
    "first_response_unobservable": "dataset has no first_response_at column, so a breach cannot be observed, only inferred from elapsed time",
    "premium_support_undefined": "premium_support flag has no defined effect in any supplied document",
}


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _citation(c: dict) -> dict:
    return {
        "source": c.get("source"),
        "authority": c.get("authority"),
        "quote": _clip(c.get("quote", ""), QUOTE_CHARS),
    }


def _search_hit(hit: dict) -> dict:
    out = {
        "source": hit.get("source"),
        "authority": hit.get("authority"),
        "text": _clip(hit.get("text", ""), TEXT_CHARS),
    }
    if hit.get("warning"):
        out["warning"] = hit["warning"]
    return out


def compact(result: dict) -> dict:
    """Return a token-lean view of a tool result. Never mutates the input."""
    if not isinstance(result, dict):
        return result

    out: dict[str, Any] = {}

    for key, value in result.items():
        if key in DROP_KEYS:
            continue

        if key == "citations" and isinstance(value, list):
            out[key] = [_citation(c) for c in value if isinstance(c, dict)]

        elif key == "results" and isinstance(value, list):
            out[key] = [_search_hit(h) for h in value[:MAX_SEARCH_RESULTS] if isinstance(h, dict)]
            if len(value) > MAX_SEARCH_RESULTS:
                out["results_truncated"] = f"{len(value) - MAX_SEARCH_RESULTS} lower-ranked passages omitted"

        elif key == "assumptions" and isinstance(value, list):
            # Arrives as full prose; map back to short labels where we can.
            labels = []
            for note in value:
                match = next(
                    (lab for k, lab in ASSUMPTION_LABELS.items()
                     if note.startswith(ASSUMPTION_LABELS[k][:18]) or k in note),
                    None,
                )
                labels.append(match or _clip(note, 140))
            out[key] = labels

        elif key in ("tickets", "orders", "actions") and isinstance(value, list):
            out[key] = [compact(v) if isinstance(v, dict) else v for v in value[:MAX_LIST_ITEMS]]
            if len(value) > MAX_LIST_ITEMS:
                out[f"{key}_truncated"] = f"{len(value) - MAX_LIST_ITEMS} more not shown"

        elif key == "reasoning" and isinstance(value, list):
            out[key] = [_clip(step, 170) for step in value[:6]]

        elif isinstance(value, dict):
            out[key] = compact(value)

        elif isinstance(value, str):
            out[key] = _clip(value, TEXT_CHARS)

        else:
            out[key] = value

    return out


def compact_json_size(result: dict) -> int:
    import json

    return len(json.dumps(compact(result), default=str))


def serialise_for_model(result: dict) -> str:
    """Compact, JSON-encode, and hard-cap. This is what the model actually sees."""
    import json

    text = json.dumps(compact(result), default=str)
    if len(text) <= HARD_CAP:
        return text
    return text[:HARD_CAP] + '…","_truncated":"result too large; ask for something narrower"}'


def assumption_labels(keys: list[str]) -> list[str]:
    return [ASSUMPTION_LABELS.get(k, k) for k in keys]
