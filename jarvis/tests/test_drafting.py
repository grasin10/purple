import itertools

import pytest

from jarvis.config import BusinessProfile
from jarvis.drafting import QUESTIONS, compose_reply, draft_reply, extract_details, missing_fields, policy_check


def test_extract_details_junk(profile):
    d = extract_details("Need a couch and 2 mattresses hauled from 30301 next Tuesday, photos attached", profile)
    assert d.zip_code == "30301"
    assert {"couch", "mattresses"} <= set(d.items)
    assert d.timing.lower() == "next tuesday"
    assert d.photos_mentioned


def test_extract_details_cleaning(profile):
    d = extract_details("Deep clean, 3 bed 2.5 bath, about 1,800 sq ft in Atlanta", profile)
    assert (d.bedrooms, d.bathrooms, d.square_feet, d.cleaning_type, d.city) == ("3", "2.5", "1800", "deep", "atlanta")


def test_does_not_reask_known_information():
    details = {"zip_code": "30301", "items": ["couch", "fridge"], "photos_mentioned": True, "timing": "friday"}
    assert missing_fields("junk_removal", details) == []
    details = {"bedrooms": "3", "cleaning_type": "deep"}
    assert missing_fields("home_cleaning", details) == ["location", "timing"]


@pytest.mark.parametrize("service", ["junk_removal", "home_cleaning"])
def test_no_draft_combination_violates_policy(service, profile):
    keys = list(QUESTIONS)
    for n in range(len(keys) + 1):
        for combo in itertools.combinations(keys, n):
            for prof in (profile, BusinessProfile()):
                body = compose_reply("Dana Smith", service, list(combo), prof)
                assert policy_check(body) == [], body


@pytest.mark.parametrize("text,reason", [
    ("That will be $150 total", "states a price"),
    ("We guarantee same day service", "makes a guarantee"),
    ("You're booked for Tuesday at 9", "confirms an appointment"),
    ("We are fully insured", "claims insurance/licensing"),
    ("Get 20% off this week", "offers a discount/promotion"),
    ("Yes we do service your area", "asserts coverage/availability"),
])
def test_policy_check_catches_commitments(text, reason):
    assert reason in policy_check(text)


def test_out_of_area_zip_is_flagged(profile):
    r = draft_reply(name="", service_type="junk_removal", details={"zip_code": "99999"}, attachments=None, profile=profile)
    assert any("99999" in n for n in r.notes)
    assert "99999" not in r.body  # never tells the customer we don't serve them


def test_signs_with_business_name_only_when_configured(profile):
    assert "Test Haul & Clean" in compose_reply("", "junk_removal", [], profile)
    assert compose_reply("", "junk_removal", [], BusinessProfile()).endswith("Thanks!")
