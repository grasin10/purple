import pytest

from jarvis.router import Context, handle, parse


@pytest.mark.parametrize("text,cmd", [
    ("/leads", "leads"),
    ("/leads@JarvisBot", "leads"),
    ("JARVIS, show me every new junk removal inquiry.", "junk"),
    ("JARVIS, find cleaning leads that haven't received a reply.", "unanswered"),
    ("JARVIS, what are my most important business tasks today?", "briefing"),
    ("JARVIS, show me which integrations aren't connected.", "status"),
    ("JARVIS, summarize the leads and jobs from today.", "evening"),
    ("JARVIS, prepare five original Facebook posts.", "marketing"),
    ("show me follow ups", "followups"),
    ("/nonsense", None),
])
def test_parse(text, cmd):
    assert parse(text)[0] == cmd


def test_unanswered_cleaning_filter():
    assert parse("find cleaning leads that haven't received a reply") == ("unanswered", "home_cleaning")


def test_marketing_is_honest_about_status(db, settings, profile):
    ctx = Context(db=db, settings=settings, profile=profile, actor="test")
    reply = handle(ctx, "/marketing")
    assert "not built yet" in reply and "Nothing has been drafted or posted" in reply


def test_stage_validation(db, settings, profile):
    ctx = Context(db=db, settings=settings, profile=profile, actor="test", message_ref="r1")
    handle(ctx, "/inquiry Need an old couch hauled away")
    assert "Unknown stage" in handle(ctx, "/stage 1 PAID")
    assert "moved to BOOKED" in handle(ctx, "/stage 1 booked")
    assert "BOOKED" in handle(ctx, "/lead 1")


def test_unknown_command(db, settings, profile):
    ctx = Context(db=db, settings=settings, profile=profile, actor="test")
    assert "/help" in handle(ctx, "blorp")
