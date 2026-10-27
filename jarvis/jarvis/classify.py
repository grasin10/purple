"""Deterministic, conservative message classifier for junk removal / home cleaning.

Design rules:
- Only messages that are clearly about our two services become leads.
- Anything sensitive (complaints, refunds, damage, legal) goes to the owner.
- Anything uncertain goes to the review queue - never to an automatic reply.
- Instruction-like text inside a customer message ("ignore previous
  instructions...") is treated as untrusted content and forces human review.

A rule-based classifier is used first because it is predictable and testable.
An LLM-backed classifier can be added later behind the same `classify()`
signature, with these rules kept as a guardrail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class Category:
    JUNK_REMOVAL_LEAD = "JUNK_REMOVAL_LEAD"
    HOME_CLEANING_LEAD = "HOME_CLEANING_LEAD"
    EXISTING_CUSTOMER_REQUEST = "EXISTING_CUSTOMER_REQUEST"
    AMBIGUOUS = "AMBIGUOUS"
    UNRELATED = "UNRELATED"
    SPAM = "SPAM"
    SENSITIVE = "SENSITIVE_OR_ESCALATION_REQUIRED"

    LEADS = (JUNK_REMOVAL_LEAD, HOME_CLEANING_LEAD)
    NEEDS_REVIEW = (AMBIGUOUS, SENSITIVE, EXISTING_CUSTOMER_REQUEST)


# Minimum confidence for a message to be treated as a routine lead.
LEAD_CONFIDENCE_THRESHOLD = 0.8


def _rx(*patterns: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)


JUNK_STRONG = _rx(
    r"junk", r"haul(?:ing|ed)?(?: away| off)?", r"clean(?:ed)?[\s-]?outs?", r"clear(?:ing)? out",
    r"dumpster", r"debris", r"dump run", r"trash (?:pick[\s-]?up|removal)",
    r"(?:furniture|appliance|mattress|couch|sofa|piano|hot tub|shed) (?:removal|disposal|pick[\s-]?up)",
    r"(?:get rid of|dispose of|throw away|take away|pick up) (?:an? |my |the |some |our )?(?:old )?"
    r"(?:couch|sofa|mattress|fridge|refrigerator|washer|dryer|furniture|appliances?|tv|desk|dresser|recliner|treadmill|boxes|stuff|junk)",
    r"remov(?:e|al of) (?:an? |my |the |some |our )?(?:old )?"
    r"(?:couch|sofa|mattress|fridge|refrigerator|washer|dryer|furniture|appliances?|hot tub|piano|carpet|shed|debris)",
)
JUNK_WEAK = _rx(
    r"couch", r"sofa", r"mattress(?:es)?", r"fridge", r"refrigerator", r"washer", r"dryer",
    r"furniture", r"appliances?", r"recliner", r"treadmill", r"garage", r"attic", r"basement",
    r"truck ?load", r"estate", r"hoard(?:er|ing)?", r"scrap",
)
CLEAN_STRONG = _rx(
    r"(?:house|home|apartment|condo|office|move[\s-]?(?:in|out)|deep|recurring|weekly|bi[\s-]?weekly|monthly|"
    r"airbnb|post[\s-]?construction|maid|residential|regular|standard) clean(?:ed|ing|er|ers)?",
    r"clean(?:ing)? (?:service|services|lady|crew|company)",
    r"(?:need|want|looking for|hire) (?:a |someone to )?clean(?:er|ers|ing)?",
    r"clean (?:my|our|the) (?:house|home|apartment|condo|place|kitchen|bathrooms?)",
    r"maid service", r"housekeep(?:er|ing)",
)
CLEAN_WEAK = _rx(
    r"clean(?:ing|er|ers)?", r"\d+\s?(?:bed(?:room)?s?|br|bd)", r"\d+(?:\.\d)?\s?(?:bath(?:room)?s?|ba)",
    r"sq\.? ?ft", r"square feet", r"dust(?:ing)?", r"mop(?:ping)?", r"vacuum(?:ing)?", r"baseboards", r"oven",
)
INTENT = _rx(
    r"quote", r"estimate", r"price", r"pricing", r"how much", r"cost", r"rates?", r"availab(?:le|ility)",
    r"schedule", r"book(?:ing)?", r"appointment", r"do you (?:service|serve|cover|come to|do)", r"service area",
    r"can you", r"are you able", r"need", r"looking for", r"interested", r"come (?:out|by)",
)
# Business-specific intent: enough on its own to justify human review even
# when no service is named ("I need a quote"). Generic phrases are not.
BUSINESS_INTENT = _rx(
    r"quote", r"estimate", r"pricing", r"how much", r"rates", r"availability", r"book(?:ing)? (?:a|an)",
    r"do you (?:service|serve|cover|come to)", r"service area",
)
EXISTING = _rx(
    r"my (?:booking|appointment|reservation|cleaning|pickup|pick[\s-]?up|job)", r"resched\w*",
    r"cancel(?:l?ing|l?ed)? my", r"(?:are|is) (?:you|the crew|your team) still coming", r"confirm(?:ing)? (?:my|our|the) (?:appointment|booking|time)",
    r"you (?:came|cleaned|hauled|removed)", r"last (?:week|time|visit)", r"following up on", r"running late",
)
SENSITIVE = _rx(
    r"refund", r"charge ?back", r"dispute", r"complain\w*", r"damag\w*", r"broke(?:n)?", r"scratch(?:ed)?",
    r"stol(?:e|en)", r"missing (?:item|jewel\w*|money)", r"lawyer", r"attorney", r"sue", r"lawsuit", r"legal action",
    r"police", r"injur\w*", r"hurt", r"insurance claim", r"unacceptable", r"terrible service", r"worst",
    r"bbb", r"report you", r"never (?:showed|came)", r"no[\s-]show", r"overcharg\w*", r"money back",
)
SPAM = _rx(
    r"seo", r"rank(?:ing)? (?:on|your) google", r"backlinks?", r"web ?design services?", r"crypto(?:currency)? (?:investment|opportunity)",
    r"bitcoin", r"forex signals", r"gift ?cards?", r"wire transfer", r"click (?:here|the link)", r"limited time offer",
    r"guaranteed (?:leads|traffic|results|income)", r"work from home", r"make \$?\d+[k]? (?:a|per) (?:day|week)",
    r"viagra", r"casino", r"loan approval", r"merchant cash advance", r"we buy houses", r"buy followers", r"lead generation agency",
    r"you(?:'ve| have) won", r"claim your (?:prize|reward)", r"verify your account",
)
INJECTION = re.compile(
    r"(ignore (?:all |any |the )?(?:previous|prior|above) (?:instructions|rules)|system prompt|you are now|"
    r"act as (?:an?|the) |developer mode|disregard (?:your|the) (?:rules|instructions)|new instructions:|"
    r"reveal (?:your|the) (?:prompt|instructions|api key|token)|send me (?:the|your) (?:api|token|password))",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class Classification:
    category: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    service_type: str = "unknown"
    injection_suspected: bool = False

    @property
    def is_lead(self) -> bool:
        return self.category in Category.LEADS and self.confidence >= LEAD_CONFIDENCE_THRESHOLD

    @property
    def needs_review(self) -> bool:
        return not self.is_lead and self.category not in (Category.SPAM, Category.UNRELATED)


def _hits(rx: re.Pattern, text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in rx.finditer(text)})


def classify(text: str, *, has_existing_lead: bool = False) -> Classification:
    body = (text or "").strip()
    if not body:
        return Classification(Category.UNRELATED, 0.9, ["empty message"])

    reasons: list[str] = []
    injection = bool(INJECTION.search(body))

    sensitive = _hits(SENSITIVE, body)
    spam = _hits(SPAM, body)
    urls = URL_RE.findall(body)
    js, jw = _hits(JUNK_STRONG, body), _hits(JUNK_WEAK, body)
    cs, cw = _hits(CLEAN_STRONG, body), _hits(CLEAN_WEAK, body)
    # "clean out" / "cleanout" is junk vocabulary, not house cleaning.
    cw = [w for w in cw if not any("clean" in j and "out" in j for j in js)]
    intent = _hits(INTENT, body)
    business_intent = _hits(BUSINESS_INTENT, body)
    existing = _hits(EXISTING, body)

    junk_score = 2 * len(js) + min(len(jw), 2)
    clean_score = 2 * len(cs) + min(len(cw), 2)
    service_type = "unknown"
    if junk_score > clean_score:
        service_type = "junk_removal"
    elif clean_score > junk_score:
        service_type = "home_cleaning"

    if injection:
        reasons.append("message contains instruction-like text (possible prompt injection)")
        return Classification(Category.AMBIGUOUS, 0.5, reasons, service_type, injection_suspected=True)

    if sensitive:
        reasons.append("sensitive terms: " + ", ".join(sensitive))
        return Classification(Category.SENSITIVE, 0.9, reasons, service_type)

    if spam or (len(urls) >= 2 and not (js or cs)):
        reasons.append("spam signals: " + ", ".join(spam or ["multiple links"]))
        return Classification(Category.SPAM, 0.9 if spam else 0.7, reasons, service_type)

    if existing or (has_existing_lead and (junk_score or clean_score or intent)):
        reasons.append("refers to an existing job/booking" if existing else "sender has an open lead")
        if existing:
            reasons.append("matched: " + ", ".join(existing))
        return Classification(Category.EXISTING_CUSTOMER_REQUEST, 0.85, reasons, service_type)

    if junk_score == 0 and clean_score == 0:
        if has_existing_lead:
            reasons.append("reply from a sender with an open lead")
            return Classification(Category.EXISTING_CUSTOMER_REQUEST, 0.7, reasons)
        if business_intent and len(body) < 400:
            reasons.append("asks about a service but none of ours is identifiable")
            return Classification(Category.AMBIGUOUS, 0.4, reasons)
        reasons.append("no junk-removal or cleaning signals")
        return Classification(Category.UNRELATED, 0.8, reasons)

    if junk_score and clean_score and abs(junk_score - clean_score) < 2:
        reasons.append("mentions both services with similar weight")
        return Classification(Category.AMBIGUOUS, 0.5, reasons, service_type)

    score = max(junk_score, clean_score)
    strong = js if service_type == "junk_removal" else cs
    weak = jw if service_type == "junk_removal" else cw
    if strong:
        confidence = 0.9 if intent or score >= 3 else 0.8
        reasons.append("strong signals: " + ", ".join(strong))
    elif weak and intent:
        confidence = 0.8 if len(weak) >= 2 else 0.65
        reasons.append("weak signals: " + ", ".join(weak) + "; intent: " + ", ".join(intent))
    else:
        confidence = 0.4
        reasons.append("only weak signals without a request: " + ", ".join(weak))

    if confidence < LEAD_CONFIDENCE_THRESHOLD:
        return Classification(Category.AMBIGUOUS, confidence, reasons, service_type)
    category = Category.JUNK_REMOVAL_LEAD if service_type == "junk_removal" else Category.HOME_CLEANING_LEAD
    return Classification(category, confidence, reasons, service_type)
