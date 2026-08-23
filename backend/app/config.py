"""Central configuration and documented assumptions."""
from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from app import env as _env  # noqa: F401  -- loads backend/.env on import

# Inherited from the workbook; no policy document states a timezone.
TIMEZONE = ZoneInfo("Asia/Kolkata")
TZ_LABEL = "Asia/Kolkata (IST)"

# Business calendar -- ASSUMPTION: the source pack never defines it.
BUSINESS_DAY_START = time(9, 0)
BUSINESS_DAY_END = time(18, 0)
BUSINESS_WEEKDAYS = (0, 1, 2, 3, 4)  # Mon..Fri (Python weekday())
BUSINESS_HOLIDAYS: tuple[str, ...] = ()  # none supplied

BUSINESS_HOURS_PER_DAY = 9.0  # derived from 09:00-18:00

ASSUMPTION_NOTES = {
    "business_hours": (
        f"Business hours assumed {BUSINESS_DAY_START.strftime('%H:%M')}-"
        f"{BUSINESS_DAY_END.strftime('%H:%M')} {TZ_LABEL}, Monday to Friday, "
        "no public holidays. The supplied documents do not define business "
        "hours; this is a documented assumption of this system."
    ),
    "business_day": (
        f"'1 business day' is interpreted as {BUSINESS_HOURS_PER_DAY:g} business "
        "hours (one full working day). The supplied documents do not define it."
    ),
    "enterprise_p2_coverage": (
        "Support Policy v3 s3 states the Enterprise P2 target as '2 hours' "
        "without the word 'business', unlike every Growth/Standard cell. We "
        "interpret the omission as meaningful and treat it as 2 clock hours "
        "(24x7). This is an interpretation, not a stated rule."
    ),
    "first_response_unobservable": (
        "The tickets dataset has no first_response_at column, so whether a "
        "first response was actually sent cannot be observed. This system "
        "reports 'no first response recorded' plus elapsed time, rather than "
        "asserting an SLA breach as established fact."
    ),
    "premium_support_undefined": (
        "The accounts sheet carries a premium_support flag, but no supplied "
        "document defines its effect. This system deliberately assigns it no "
        "entitlement and will say so if asked."
    ),
}

# Source authority tiers -- mandated by Support Policy v3 s1.
TIER_CUSTOMER_AGREEMENT = 1
TIER_CURRENT_POLICY = 2
TIER_PRODUCT_DOC = 3
TIER_DEPRECATED = 90  # excluded from answers unless explicitly asked
TIER_TICKET_HISTORY = 99  # never authoritative

TIER_LABELS = {
    TIER_CUSTOMER_AGREEMENT: "Signed customer agreement",
    TIER_CURRENT_POLICY: "Current support policy / SOP",
    TIER_PRODUCT_DOC: "Current product documentation",
    TIER_DEPRECATED: "Deprecated document (not current policy)",
    TIER_TICKET_HISTORY: "Historical ticket (context only, may be incorrect)",
}

# Tiers a policy answer may cite.
AUTHORITATIVE_TIERS = (TIER_CUSTOMER_AGREEMENT, TIER_CURRENT_POLICY, TIER_PRODUCT_DOC)

from pathlib import Path  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent.parent
SOURCE_PACK_DIR = REPO_ROOT / "data"
BUILD_DIR = REPO_ROOT / "build"
CORPUS_FILE = BUILD_DIR / "corpus.json"
RAW_TEXT_FILE = BUILD_DIR / "raw_text.json"
WORKBOOK = SOURCE_PACK_DIR / "ParcelPilot_Assessment_Data.xlsx"
