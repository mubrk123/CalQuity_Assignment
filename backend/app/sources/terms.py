"""Load the structured policy/contract terms and verify their quotes against the PDFs."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from app.config import RAW_TEXT_FILE
from app.corpus.build import normalise

_HERE = Path(__file__).resolve().parent
POLICY_FILE = _HERE / "policy_defaults.json"
CONTRACTS_FILE = _HERE / "contract_terms.json"


class TermVerificationError(RuntimeError):
    """A declared quote is absent from its source document."""


def _load_raw_text() -> dict[str, str]:
    if not RAW_TEXT_FILE.exists():
        raise TermVerificationError(
            f"{RAW_TEXT_FILE} missing. Run: python -m app.corpus.build"
        )
    return json.loads(RAW_TEXT_FILE.read_text())


def _walk_quotes(node: Any, source_doc: str | None, path: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield (json_path, source_doc, quote) for every *quote-bearing key."""
    if isinstance(node, dict):
        source_doc = node.get("source_doc", source_doc)
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key.endswith("quote") and isinstance(value, str):
                if source_doc is None:
                    raise TermVerificationError(f"{child}: quote with no source_doc in scope")
                yield child, source_doc, value
            else:
                yield from _walk_quotes(value, source_doc, child)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_quotes(item, source_doc, f"{path}[{i}]")


def verify(terms: dict, raw_text: dict[str, str]) -> list[tuple[str, str]]:
    """Return the verified (json_path, source_doc) pairs; raise on any miss."""
    verified: list[tuple[str, str]] = []
    failures: list[str] = []
    for json_path, doc_id, quote in _walk_quotes(terms, None):
        doc_text = raw_text.get(doc_id)
        if doc_text is None:
            failures.append(f"{json_path}: unknown source_doc {doc_id!r}")
            continue
        if normalise(quote) not in doc_text:
            failures.append(
                f"{json_path}: quote not found in {doc_id}\n      quote: {normalise(quote)[:110]}"
            )
        else:
            verified.append((json_path, doc_id))
    if failures:
        raise TermVerificationError(
            "Structured terms no longer match the source documents:\n  - "
            + "\n  - ".join(failures)
        )
    return verified


@lru_cache(maxsize=1)
def load() -> tuple[dict, dict, list[tuple[str, str]]]:
    """Return (policy_defaults, contract_terms, verified_quotes)."""
    raw = _load_raw_text()
    policy = json.loads(POLICY_FILE.read_text())
    contracts = json.loads(CONTRACTS_FILE.read_text())
    verified = verify(policy, raw) + verify(contracts, raw)
    return policy, contracts, verified


def policy_defaults() -> dict:
    return load()[0]


def contract_terms() -> dict:
    return load()[1]


def contract_for(account_id: str) -> dict | None:
    return load()[1].get(account_id)


if __name__ == "__main__":
    _p, _c, ok = load()
    print(f"verified {len(ok)} source quotes against the extracted PDF text")
    for json_path, doc in ok:
        print(f"  OK  {doc:<45} {json_path}")
