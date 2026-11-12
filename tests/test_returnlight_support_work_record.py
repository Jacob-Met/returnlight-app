"""Support work citations keep the exact internal history event."""
import pytest
from returnlight_app.returnlight import new_case, update_merchant_followup, merchant_work_sources
from returnlight_app.returnlight_record import _buyer_source_markup

WORDS = "Courier confirmed a prepaid label is available. Collection is not booked."

def work_case(tmp_path):
    path = tmp_path / "case.json"
    case = new_case(path, "WORKCITATION01", "49.00", WORDS)
    case = update_merchant_followup(path, WORDS, False, len(case["history"]))
    case = update_merchant_followup(path, WORDS, True, len(case["history"]))
    return case

def markup(case, source):
    reading = {"kind": "support_brief", "sources": [source]}
    links, extras = {}, {}
    buyers, _, _ = _buyer_source_markup(case, links, reading, extras)
    return buyers, links, extras

def test_support_work_quote_targets_the_selected_event_not_identical_buyer_or_plan(tmp_path):
    case = work_case(tmp_path)
    planned, completed = merchant_work_sources(case)
    source = {"kind": "work", "work_seq": completed["seq"], "quote_start": 0, "quote": WORDS}
    buyers, links, extras = markup(case, source)
    target = links[id(source)]
    assert target in extras[("work", completed["seq"])]
    assert '<mark class="kept-source"' in extras[("work", completed["seq"])]
    assert target not in extras[("work", planned["seq"])]
    assert target not in buyers[0]
    assert planned["completed"] is False and completed["completed"] is True

@pytest.mark.parametrize("changed", [
    {"work_seq": True}, {"work_seq": 0}, {"work_seq": -1}, {"work_seq": 999},
    {"quote_start": 1}, {"quote": "Collection is booked."},
])
def test_invalid_work_location_never_falls_back_to_matching_other_words(tmp_path, changed):
    case = work_case(tmp_path)
    source = {"kind": "work", "work_seq": merchant_work_sources(case)[-1]["seq"],
              "quote_start": 0, "quote": WORDS, **changed}
    _, links, extras = markup(case, source)
    assert not links
    assert all('id="ai-source-' not in text for text in extras.values())
