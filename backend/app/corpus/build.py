"""Build step: source PDFs -> metadata-tagged chunk corpus (python -m app.corpus.build)."""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import (  # noqa: E402
    BUILD_DIR,
    CORPUS_FILE,
    RAW_TEXT_FILE,
    SOURCE_PACK_DIR,
    TIER_CURRENT_POLICY,
    TIER_CUSTOMER_AGREEMENT,
    TIER_DEPRECATED,
    TIER_PRODUCT_DOC,
)


def normalise(text: str) -> str:
    """Collapse whitespace and fold unicode punctuation for stable matching."""
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("•", "*").replace("●", "*").replace("○", "-")
        .replace("\xa0", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    section: str
    text: str
    authority_tier: int
    status: str
    effective: str | None
    account_scope: str | None  # ACCT-xxx for contracts, None = applies globally
    supersedes: str | None
    superseded_by: str | None
    source_ref: str  # human-readable citation, e.g. "Support Policy v3 s3"


@dataclass
class Document:
    doc_id: str
    title: str
    filename: str
    status: str
    effective: str | None
    supersedes: str | None
    superseded_by: str | None
    account_scope: str | None
    authority_tier: int
    short_ref: str
    raw_text: str = ""
    chunks: list[Chunk] = field(default_factory=list)


# Only the filename -> citation label mapping is declared; every other
# attribute is parsed out of the document body.
SHORT_REFS = {
    "01_Support_Policy_v3_CURRENT": "Support Policy v3",
    "02_Support_Policy_v2_DEPRECATED": "Support Policy v2 (DEPRECATED)",
    "03_Cancellation_and_Service_Credit_SOP_v4": "Cancellation & Service Credit SOP v4",
    "04_Product_Operations_Guide_and_Known_Issues": "Product Operations Guide",
    "05_Northstar_Logistics_Enterprise_Agreement": "Northstar Logistics Enterprise Agreement",
    "06_LumenWorks_Service_Agreement": "LumenWorks Service Agreement",
}

_TITLE_STOP = re.compile(
    r"^(Status|Effective|Updated|Supersedes|Superseded by|Account|Customer|Term|Plan)\s*:",
    re.I,
)


def _field(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else None


def parse_document(path: Path) -> Document:
    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    raw = "\n".join(pages)
    flat = normalise(raw)
    stem = path.stem

    status = (_field(r"Status\s*:\s*([A-Za-z ]+?)(?:\s*-|\s+Effective|\s+Updated|$)", flat) or "CURRENT").upper()
    status = "DEPRECATED" if "DEPRECATED" in status else ("ACTIVE" if "ACTIVE" in status else "CURRENT")

    effective = _field(r"(?:Effective|Updated)\s*:\s*([0-9]{1,2} [A-Za-z]+ [0-9]{4})", flat)
    supersedes = _field(r"Supersedes\s*:\s*([^.]+?)(?:\s+1\.|\s*$)", flat)
    superseded_by = _field(r"Superseded by\s*:\s*([^.]+?)(?:\s+Severity|\s*$)", flat)
    account_scope = _field(r"Account\s*:\s*(ACCT-\d+)", flat)

    # Authority tier, derived from parsed facts per Support Policy v3 s1.
    if status == "DEPRECATED" or superseded_by:
        tier = TIER_DEPRECATED
    elif account_scope:
        tier = TIER_CUSTOMER_AGREEMENT
    elif re.search(r"Product Operations Guide", flat, re.I):
        tier = TIER_PRODUCT_DOC
    else:
        tier = TIER_CURRENT_POLICY

    title_lines = [ln.strip() for ln in pages[0].splitlines() if ln.strip()]
    title_parts: list[str] = []
    for ln in title_lines[:3]:
        if _TITLE_STOP.match(ln):
            break
        title_parts.append(ln)
    title = " ".join(title_parts) or stem

    return Document(
        doc_id=stem,
        title=normalise(title),
        filename=path.name,
        status=status,
        effective=effective,
        supersedes=supersedes,
        superseded_by=superseded_by,
        account_scope=account_scope,
        authority_tier=tier,
        short_ref=SHORT_REFS.get(stem, stem),
        raw_text=raw,
    )


# A numbered section heading, e.g. "1. Scope and source precedence"
_SECTION = re.compile(r"(?m)^\s*(\d+)\.\s+([A-Z][^\n]{2,80})$")
# A known-issue heading, e.g. "KI-208 - Bulk Upload failures on large CSVs"
_KI = re.compile(r"(?m)^\s*(KI-\d+)\s*[-–]\s*([^\n]+)$")


def chunk_document(doc: Document) -> list[Chunk]:
    body = re.sub(r"--- page \d+ ---\n?", "", doc.raw_text)

    marks: list[tuple[int, str]] = []
    for m in _SECTION.finditer(body):
        marks.append((m.start(), f"s{m.group(1)} {m.group(2).strip()}"))
    for m in _KI.finditer(body):
        marks.append((m.start(), f"{m.group(1)} {m.group(2).strip()}"))
    marks.sort()

    if not marks:
        marks = [(0, "body")]
    if marks[0][0] > 0:
        marks.insert(0, (0, "header"))

    chunks: list[Chunk] = []
    for i, (start, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        text = normalise(body[start:end])
        if len(text) < 25:
            continue
        section_no = label.split()[0]
        chunks.append(
            Chunk(
                chunk_id=f"{doc.doc_id}#{section_no}",
                doc_id=doc.doc_id,
                doc_title=doc.title,
                section=label,
                text=text,
                authority_tier=doc.authority_tier,
                status=doc.status,
                effective=doc.effective,
                account_scope=doc.account_scope,
                supersedes=doc.supersedes,
                superseded_by=doc.superseded_by,
                source_ref=f"{doc.short_ref} {section_no}" if section_no not in ("header", "body") else doc.short_ref,
            )
        )
    return chunks


def build() -> dict:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    docs: list[Document] = []
    for path in sorted(SOURCE_PACK_DIR.glob("*.pdf")):
        doc = parse_document(path)
        doc.chunks = chunk_document(doc)
        docs.append(doc)

    corpus = {
        "documents": [
            {k: v for k, v in asdict(d).items() if k not in ("raw_text", "chunks")}
            for d in docs
        ],
        "chunks": [asdict(c) for d in docs for c in d.chunks],
    }
    CORPUS_FILE.write_text(json.dumps(corpus, indent=2))
    RAW_TEXT_FILE.write_text(
        json.dumps({d.doc_id: normalise(d.raw_text) for d in docs}, indent=2)
    )
    return corpus


if __name__ == "__main__":
    c = build()
    print(f"documents: {len(c['documents'])}  chunks: {len(c['chunks'])}\n")
    for d in c["documents"]:
        print(
            f"  tier {d['authority_tier']:>2}  {d['status']:<10} "
            f"eff={str(d['effective']):<16} scope={str(d['account_scope']):<9} {d['short_ref']}"
        )
    print()
    for ch in c["chunks"]:
        print(f"  [{ch['authority_tier']:>2}] {ch['source_ref']:<45} {ch['text'][:60]}...")
