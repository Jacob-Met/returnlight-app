"""A merchant can recognize a case without altering its source or payment decision."""
import copy
import pytest
from returnlight_app.returnlight import (
    CaseChanged, CaseError, case_snapshot, close_case, new_case, set_case_subject,
)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

MESSAGE = "The lantern works and I am keeping it. Only the broken strap needs a $12 refund."


def test_case_name_survives_reload_without_rewriting_buyer_or_outcome(tmp_path):
    path = tmp_path / "case.json"
    original = new_case(path, "SUBJECTCAPTURE01", "12.00", MESSAGE)
    original = close_case(path, "Replacement strap agreed with the buyer.",
                          "CLOSE " + original["id"][:8])
    renamed = set_case_subject(path, "Broken strap · replacement", len(original["history"]))
    assert case_snapshot(path) == renamed
    assert renamed["subject"] == "Broken strap · replacement"
    assert renamed["history"][:-1] == original["history"]
    for key, value in original.items():
        if key != "history":
            assert renamed[key] == value
    assert renamed["history"][-1]["event"] == "merchant.subject_changed"


def test_case_name_rejects_a_stale_tab_without_losing_current_name(tmp_path):
    path = tmp_path / "case.json"
    case = new_case(path, "SUBJECTCAPTURE01", "12.00", MESSAGE)
    set_case_subject(path, "Broken strap", len(case["history"]))
    before = path.read_bytes()
    with pytest.raises(CaseChanged):
        set_case_subject(path, "Old tab name", len(case["history"]))
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", [None, 3, "x" * 121, "Two\nlines", "Two\rlines"])
def test_invalid_names_do_not_change_the_case(tmp_path, name):
    path = tmp_path / "case.json"
    case = new_case(path, "SUBJECTCAPTURE01", "12.00", MESSAGE)
    before = path.read_bytes()
    with pytest.raises(CaseError):
        set_case_subject(path, name, len(case["history"]))
    assert path.read_bytes() == before


def test_case_name_is_available_in_inbox_record_and_can_be_cleared(tmp_path):
    app = LocalCases(tmp_path)
    created = app.create({"capture_id": "SUBJECTCAPTURE01", "ceiling": "12.00", "complaint": MESSAGE})
    key, case = created["key"], created["case"]
    case = app.action(key, "subject", {"subject": 'Strap <replacement> & "return"',
                                       "case_revision": len(case["history"])})["case"]
    assert app.summary(key)["subject"] == case["subject"]
    assert 'Strap &lt;replacement&gt; &amp; &quot;return&quot;' in render_case_record(case)
    assert MESSAGE in case["complaint"]
    cleared = app.action(key, "subject", {"subject": "", "case_revision": len(case["history"])})["case"]
    assert "subject" not in cleared
    assert app.summary(key)["subject"] is None
