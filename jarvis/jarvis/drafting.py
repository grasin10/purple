"""Customer reply drafting with detail extraction and a policy guard.

Drafts only ask for missing information and acknowledge the request. They never
state a price, availability, coverage, discount, insurance, guarantee, or an
appointment confirmation - those require the owner's approved rules or explicit
approval. `policy_check()` enforces this on every draft before it is stored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import BusinessProfile

ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
BED_RE = re.compile(r"(\d+)\s?(?:bed(?:room)?s?|br|bd)\b", re.I)
BATH_RE = re.compile(r"(\d+(?:\.\d)?)\s?(?:bath(?:room)?s?|ba)\b", re.I)
SQFT_RE = re.compile(r"(\d[\d,]{2,})\s?(?:sq\.? ?ft|square feet|sf)\b", re.I)
TIMING_RE = re.compile(
    r"\b(today|tonight|tomorrow|this (?:week(?:end)?|morning|afternoon|evening)|next (?:week|month|\w+day)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|asap|as soon as possible|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.I,
)
PHOTO_RE = re.compile(r"\b(photos?|pictures?|pics?|images?|attached)\b", re.I)
VOLUME_RE = re.compile(
    r"\b(\d+|one|two|three|four|five|a few|several|single|whole|full|half|quarter)\s+"
    r"(items?|pieces?|truck ?loads?|loads?|bags?|boxes|rooms?|garage|basement|attic|house)\b"
    r"|\b(single item|one item|truck ?load|whole (?:house|garage|basement)|full (?:garage|basement|house))\b",
    re.I,
)
JUNK_ITEMS_RE = re.compile(
    r"\b(couch(?:es)?|sofas?|mattress(?:es)?|box ?springs?|fridges?|refrigerators?|freezers?|washers?|dryers?|"
    r"stoves?|dishwashers?|tvs?|televisions?|desks?|dressers?|beds?|tables?|chairs?|recliners?|treadmills?|"
    r"pianos?|hot tubs?|sheds?|carpets?|debris|boxes|furniture|appliances?|tires?|grills?|cabinets?|"
    r"exercise equipment|yard waste|construction debris)\b",
    re.I,
)
CLEAN_TYPE_RE = re.compile(
    r"\b(deep|standard|regular|basic|move[\s-]?(?:in|out)|recurring|weekly|bi[\s-]?weekly|monthly|one[\s-]?time|"
    r"post[\s-]?construction|airbnb|vacation rental|office)\b",
    re.I,
)


@dataclass
class ExtractedDetails:
    zip_code: str = ""
    city: str = ""
    timing: str = ""
    photos_mentioned: bool = False
    items: list[str] = field(default_factory=list)
    volume: str = ""
    bedrooms: str = ""
    bathrooms: str = ""
    square_feet: str = ""
    cleaning_type: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


def extract_details(text: str, profile: BusinessProfile | None = None) -> ExtractedDetails:
    d = ExtractedDetails()
    if m := ZIP_RE.search(text):
        d.zip_code = m.group(1)
    if profile:
        low = text.lower()
        for city in profile.service_area.cities:
            if city and re.search(r"\b" + re.escape(city) + r"\b", low):
                d.city = city
                break
    if m := TIMING_RE.search(text):
        d.timing = m.group(1)
    d.photos_mentioned = bool(PHOTO_RE.search(text))
    d.items = sorted({m.group(1).lower() for m in JUNK_ITEMS_RE.finditer(text)})
    if m := VOLUME_RE.search(text):
        d.volume = m.group(0)
    if m := BED_RE.search(text):
        d.bedrooms = m.group(1)
    if m := BATH_RE.search(text):
        d.bathrooms = m.group(1)
    if m := SQFT_RE.search(text):
        d.square_feet = m.group(1).replace(",", "")
    if m := CLEAN_TYPE_RE.search(text):
        d.cleaning_type = m.group(1).lower()
    return d


def merge_details(old: dict, new: ExtractedDetails) -> dict:
    """Combine details across a conversation so we never re-ask for known info."""
    merged = dict(old or {})
    for k, v in new.as_dict().items():
        if k == "items":
            merged["items"] = sorted(set(merged.get("items", [])) | set(v))
        elif k == "photos_mentioned":
            merged[k] = merged.get(k, False) or v
        else:
            merged.setdefault(k, v)
    return merged


QUESTIONS = {
    "location": "What's the ZIP code (or address) where the job is?",
    "items": "What items need to go?",
    "volume": "Roughly how much is there - a few items, a partial truckload, or a full room/garage?",
    "photos": "If it's easy, a few photos of everything that needs to go would help a lot.",
    "timing": "What day and time would work best for you?",
    "property_size": "How many bedrooms and bathrooms (or roughly how many square feet)?",
    "cleaning_type": "What kind of cleaning are you looking for - standard, deep, or move-in/move-out? One-time or recurring?",
}


def missing_fields(service_type: str, details: dict, attachments: list | None = None) -> list[str]:
    missing: list[str] = []
    if not (details.get("zip_code") or details.get("city")):
        missing.append("location")
    if service_type == "junk_removal":
        if not details.get("items"):
            missing.append("items")
        if not details.get("volume") and len(details.get("items", [])) < 2:
            missing.append("volume")
        if not (details.get("photos_mentioned") or attachments):
            missing.append("photos")
    elif service_type == "home_cleaning":
        if not (details.get("bedrooms") or details.get("square_feet")):
            missing.append("property_size")
        if not details.get("cleaning_type"):
            missing.append("cleaning_type")
    if not details.get("timing"):
        missing.append("timing")
    return missing


SERVICE_LABEL = {"junk_removal": "junk removal", "home_cleaning": "home cleaning"}


def compose_reply(name: str, service_type: str, missing: list[str], profile: BusinessProfile) -> str:
    first = (name or "").strip().split(" ")[0] if name else ""
    greeting = f"Hi {first}," if first else "Hi there,"
    service = SERVICE_LABEL.get(service_type, "our services")
    lines = [greeting, "", f"Thanks for reaching out about {service}!"]
    if missing:
        lines.append("To put together an accurate quote, it would help to know:")
        lines += [f"- {QUESTIONS[m]}" for m in missing]
    else:
        lines.append("Thanks for the details - we'll review everything and follow up with a quote.")
    lines.append("")
    sign = f"Thanks,\n{profile.name}" if profile.name else "Thanks!"
    lines.append(sign)
    return "\n".join(lines)


# Commitments a draft must never make without approved rules / owner approval.
POLICY_PATTERNS = [
    (re.compile(r"\$\s?\d|\d+\s?(?:dollars|usd)\b", re.I), "states a price"),
    (re.compile(r"\bguarantee", re.I), "makes a guarantee"),
    (re.compile(r"\b(?:you(?:'re| are) (?:all )?(?:set|booked|confirmed)|confirmed for|appointment is confirmed|see you (?:on|at|tomorrow))\b", re.I), "confirms an appointment"),
    (re.compile(r"\binsur(?:ed|ance)\b|\blicensed\b|\bbonded\b", re.I), "claims insurance/licensing"),
    (re.compile(r"\b\d+\s?%\s?off\b|\bdiscount\b|\bpromo(?:tion)?\b|\bfree\b", re.I), "offers a discount/promotion"),
    (re.compile(r"\bwe (?:do|don't|do not) (?:service|serve|cover)\b|\bwe(?:'re| are) available\b", re.I), "asserts coverage/availability"),
]


def policy_check(body: str) -> list[str]:
    return [reason for rx, reason in POLICY_PATTERNS if rx.search(body)]


@dataclass
class DraftResult:
    body: str
    missing: list[str]
    policy_issues: list[str]
    notes: list[str]


def draft_reply(*, name: str, service_type: str, details: dict, attachments: list | None,
                profile: BusinessProfile) -> DraftResult:
    missing = missing_fields(service_type, details, attachments)
    body = compose_reply(name, service_type, missing, profile)
    notes: list[str] = []
    area = profile.service_area
    zip_code = details.get("zip_code")
    if zip_code and area.zips and zip_code not in area.zips:
        notes.append(f"ZIP {zip_code} is not in your configured service area - review before replying")
    if not area.configured:
        notes.append("service area not configured - coverage not checked")
    if not missing:
        notes.append("all basic details present - quote needs your approval (no approved pricing rules applied)")
    return DraftResult(body=body, missing=missing, policy_issues=policy_check(body), notes=notes)
