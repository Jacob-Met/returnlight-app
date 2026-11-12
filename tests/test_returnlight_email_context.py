import json
import pytest
from returnlight_app.returnlight import new_case, case_snapshot, CaseError
from returnlight_app.returnlight_intake import import_buyer_email

def test_imported_headers_survive_reload_without_changing_buyer_words(tmp_path):
    original = "Please replace the broken lid, and leave the bowl unchanged."
    context = {"sender": "Maya <maya@example.com>", "subject": "Replacement lid"}
    path = tmp_path / "case.json"
    created = new_case(path, "CAPTURE12345", "19.50", original, imported_email=context)
    assert case_snapshot(path)["imported_email"] == context
    assert created["complaint"] == original
    assert created["review"] is None
    assert created["request_id"] is None
    assert created["state"] == "OPEN"
    context["sender"] = "Changed"
    assert case_snapshot(path)["imported_email"]["sender"] == "Maya <maya@example.com>"

@pytest.mark.parametrize("context", [
    {}, {"sender": "Maya", "subject": "x", "verified": True},
    {"sender": "Maya\nOther", "subject": "x"},
    {"sender": "Maya", "subject": "x"*999},
    {"sender": 42, "subject": "x"},
])
def test_invalid_email_context_cannot_create_case(tmp_path, context):
    path = tmp_path / "case.json"
    with pytest.raises(CaseError):
        new_case(path, "CAPTURE12345", "19.50", "Please replace the broken lid.", imported_email=context)
    assert not path.exists()

def test_legacy_manual_case_remains_compatible(tmp_path):
    path = tmp_path / "case.json"
    new_case(path, "CAPTURE12345", "19.50", "Please replace the broken lid.")
    assert "imported_email" not in case_snapshot(path)

def test_long_header_is_not_silently_lost():
    raw = "From: Maya <maya@example.com>\nSubject: " + "x"*999 + "\n\nPlease replace the broken lid."
    with pytest.raises(ValueError, match="headers"):
        import_buyer_email(raw)
