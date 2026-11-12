"""Existing support work notes are authored local records, not verified external outcomes."""
import json
import re
import pytest
from test_returnlight_support_review import fixture, answer, Reviewer
from returnlight_app.returnlight import (CaseError, CaseChanged, update_merchant_followup,
    review_support_case, case_snapshot)
from returnlight_app.returnlight_support_review import (support_sources, support_content,
    support_basis, current_support_review, support_work_sources, validate_support_review)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

PLAN = "Check the original reference and keep the Friday update."
DONE = "Original reference checked in the saved case. Buyer receipt remains unconfirmed."

def work_case(tmp_path):
    path, case = fixture(tmp_path)
    case = update_merchant_followup(path, PLAN, False, len(case["history"]))
    planned = json.loads(json.dumps(case))
    case = update_merchant_followup(path, DONE, True, len(case["history"]))
    return path, planned, case

def work_answer(case):
    value = answer(case)
    bank = support_sources(case)
    number = next(i for i, source in enumerate(bank, 1) if source.get("kind") == "work" and source["work_completed"])
    value["source_quotes"].append({"source_number": number, "quote": DONE})
    value["reply_draft"] = {"text": "I checked the saved original reference; I still need to confirm receipt.",
                            "source_numbers": [len(value["source_quotes"])]}
    return value

def test_exact_work_notes_and_status_reach_support_without_renumbering_other_sources(tmp_path):
    path, original = fixture(tmp_path)
    old = support_sources(original)
    planned = update_merchant_followup(path, PLAN, False, len(original["history"]))
    done = update_merchant_followup(path, DONE, True, len(planned["history"]))
    bank = support_sources(done)
    assert bank[:len(old)] == old
    notes = support_work_sources(done)
    assert [s["text"] for s in notes] == [PLAN, DONE]
    assert [s["work_completed"] for s in notes] == [False, True]
    assert [s["work_seq"] for s in notes] == [planned["history"][-1]["seq"], done["history"][-1]["seq"]]
    content = support_content(done)
    assert PLAN in content and DONE in content
    assert "planned" in notes[0]["label"] and "completed" in notes[1]["label"]
    assert all(s["recorded_at"] in s["label"] for s in notes)
    assert "not messages sent to the buyer" in content
    assert "does not resolve the buyer" in content
    for key in ["state", "approved_cents", "refund_id", "request_id", "support_followup_open", "post_payment_messages"]:
        assert done[key] == original[key]
    assert done["support_followup_open"] and not done["merchant_followup"]["open"]

def test_each_work_change_retires_the_old_support_reading_and_offline_answer(tmp_path):
    path, original = fixture(tmp_path)
    read = review_support_case(path, Reviewer(), len(original["history"]))
    old = support_basis(read)
    planned = update_merchant_followup(path, PLAN, False, len(read["history"]))
    assert support_basis(planned) != old and current_support_review(planned) is None
    assert LocalCases(tmp_path).detail(path.stem)["case"].get("support_review") is None
    read = review_support_case(path, Reviewer(), len(planned["history"]))
    done = update_merchant_followup(path, DONE, True, len(read["history"]))
    assert current_support_review(done) is None
    assert "AI reading · current support" not in render_case_record(done)

def test_work_quotes_use_exact_history_and_cannot_be_buyer_questions(tmp_path):
    _, _, case = work_case(tmp_path)
    value = work_answer(case)
    read = validate_support_review(value, case)
    source = read["sources"][-1]
    assert source["kind"] == "work" and source["work_completed"] is True
    assert source["work_seq"] == case["history"][-1]["seq"] and source["quote_start"] == 0
    value["open_questions"] = [len(value["source_quotes"])]
    with pytest.raises(CaseError, match="unanswered question"):validate_support_review(value, case)
    value["open_questions"] = []
    value["source_quotes"][-1]["quote"] = "The buyer received the refund."
    with pytest.raises(CaseError, match="exact"):validate_support_review(value, case)

def test_support_work_requires_explicit_new_scope_before_any_dispatch(tmp_path):
    path, _, case = work_case(tmp_path)
    class Remote(Reviewer):
        destination_id = "controlled-destination"
        calls = 0
        def review_support_case(self, case, *, before_request):
            self.calls += 1
            before_request()
            return validate_support_review(work_answer(case), case)
    reader = Remote(); app = LocalCases(tmp_path, remote_reviewer=reader)
    payload = {"remote_confirmed": True, "destination_id": reader.destination_id,
               "case_revision": len(case["history"])}
    for old in ["current-support-v1", "current-support-purchase-v2",
                "current-support-refund-check-v3", "current-support-recorded-purchase-v4"]:
        with pytest.raises(CaseError, match="internal merchant work"):
            app.action(path.stem, "support-review", {**payload, "review_scope": old})
        assert reader.calls == 0 and case_snapshot(path) == case
    received = app.action(path.stem, "support-review", {**payload, "review_scope": "current-support-work-v5"})["case"]
    assert reader.calls == 1 and received["support_review"]["sources"][-1]["quote"] == DONE

def test_offline_work_citation_points_to_its_exact_internal_history(tmp_path):
    _, _, case = work_case(tmp_path)
    case["support_review"] = validate_support_review(work_answer(case), case)
    html = render_case_record(case)
    seq = case["history"][-1]["seq"]
    passage = re.search(r'<p class="verbatim" data-work-source="' + str(seq) + r'">(.*?)</p>', html, re.S)
    assert passage and DONE in passage[1] and "<mark" in passage[1]
    assert 'id="ai-source-' in passage[1]

def test_new_work_during_reading_rejects_late_answer(tmp_path):
    path, _, case = work_case(tmp_path)
    class Racing:
        def review_support_case(self, before, *, before_request):
            before_request()
            update_merchant_followup(path, "Check the original document; the item allocation is still unknown.", False, len(before["history"]))
            return validate_support_review(work_answer(before), before)
    with pytest.raises(CaseChanged):review_support_case(path, Racing(), len(case["history"]))
    assert not case_snapshot(path).get("support_review")
