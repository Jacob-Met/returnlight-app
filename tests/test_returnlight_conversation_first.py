
import json
import pytest
from returnlight_app.returnlight import (
    CaseError, new_case, case_snapshot, human_review_case, approve_case,
    approval_phrase, execute_case, correct_payment_details, record_merchant_reply,
    close_case, reopen_case,
)
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_working_drafts import WorkingDraftStore
from returnlight_app.returnlight_record import render_case_record

WORDS = "The pitcher arrived broken. Please explain the return; keep the matching cups."
REPLY = "Please send the receipt so I can check the pitcher. Keep the matching cups."


def opened(tmp_path, capture=None):
    path = tmp_path / ("a" * 32 + ".json")
    return path, new_case(path, capture, None, WORDS, payment_pending=True)


@pytest.mark.parametrize("capture", [None, "KNOWNCAPTURE123"])
def test_conversation_opens_without_inventing_payment_or_limit(tmp_path, capture):
    path, case = opened(tmp_path, capture)
    assert case_snapshot(path)["capture_id"] == capture
    assert case["payment_pending"] is True
    assert case["ceiling_cents"] == case["approved_cents"] == 0
    assert not case["refund_id"] and not case["request_id"]
    assert WORDS in render_case_record(case)
    assert "Not set" in render_case_record(case)
    if capture is None:
        assert "Not linked" in render_case_record(case)


@pytest.mark.parametrize("capture,ceiling,pending", [
    (None, None, False), (None, "25.00", True), ("bad", None, True),
    (None, None, "true"), (None, None, 1),
])
def test_partial_setup_cannot_claim_linked_payment(tmp_path, capture, ceiling, pending):
    with pytest.raises(CaseError):
        new_case(tmp_path/"case.json", capture, ceiling, WORDS, payment_pending=pending)


def test_unlinked_case_rejects_payment_read_and_approval_before_gateway(tmp_path):
    calls = []
    def gateway():
        calls.append("gateway")
        raise AssertionError("No provider client should be needed")
    app = LocalCases(tmp_path, gateway_factory=gateway)
    opened_case = app.create({"capture_id": None, "ceiling": None, "complaint": WORDS, "payment_pending": True})
    key, case = opened_case["key"], opened_case["case"]
    assert app.list_cases()[0]["payment_pending"]
    with pytest.raises(CaseError, match="Link a payment"):
        app.inspect_capture(key)
    reviewed = app.action(key, "human-review", {"quote": WORDS, "confirmed": True, "case_revision": len(case["history"])})["case"]
    before = app.path(key).read_bytes()
    with pytest.raises(CaseError, match="Link the payment"):
        approve_case(app.path(key), "10.00", approval_phrase(reviewed, "10.00"), expected_case_revision=len(reviewed["history"]))
    with pytest.raises(CaseError):
        execute_case(app.path(key), None, gateway_factory=gateway)
    assert app.path(key).read_bytes() == before and not calls


@pytest.mark.parametrize("mutation", [
    {"state": "APPROVED"}, {"state": "PENDING"}, {"approved_cents": 100},
    {"request_id": "reserved-request"}, {"refund_id": "REFUND12345"},
    {"ceiling_cents": 100}, {"payment_pending": False},
])
def test_saved_unlinked_case_cannot_contain_refund_authorization(tmp_path, mutation):
    path, case = opened(tmp_path)
    path.write_text(json.dumps({**case, **mutation}))
    with pytest.raises(CaseError):
        case_snapshot(path)


def test_reply_and_no_refund_resolution_work_without_payment(tmp_path):
    path, case = opened(tmp_path)
    recorded = record_merchant_reply(path, REPLY, len(case["history"]), True, "b"*32)
    assert case_snapshot(path)["merchant_replies"][0]["capture_id"] is None
    assert recorded["merchant_replies"][0]["payment_pending"]
    closed = close_case(path, "Buyer will send the receipt before any remedy is agreed.",
                        "CLOSE " + case["id"][:8], expected_case_revision=len(recorded["history"]), followup_needed=True)
    assert closed["state"] == "CLOSED" and closed["payment_pending"]
    reopened = reopen_case(path, "Here is the receipt for the pitcher.", len(closed["history"]))
    assert reopened["state"] == "OPEN" and reopened["payment_pending"]
    assert reopened["merchant_replies"][0]["text"] == REPLY


def test_first_payment_link_preserves_words_and_checked_draft_boundary(tmp_path):
    path, case = opened(tmp_path)
    case = human_review_case(path, WORDS, True, expected_case_revision=len(case["history"]))
    store = WorkingDraftStore(tmp_path)
    reply = {"capture": None, "text": REPLY,
             "version": json.dumps([None, case["state"], 0, None, len(case["history"])])}
    store.save(path, {"case_revision": len(case["history"]), "draft_version": None,
                     "draft": {"decision": None, "reply": reply}})
    linked = correct_payment_details(path, "RECEIPTCAPTURE123", "38.00",
                                     "The buyer receipt matches this pitcher purchase.", len(case["history"]))
    assert "payment_pending" not in linked
    assert linked["complaint"] == WORDS and linked["approved_cents"] == 0
    assert linked["history"][-1]["event"] == "merchant.payment_linked"
    current, saved = WorkingDraftStore(tmp_path).detail(path)
    assert saved["draft"]["reply"]["text"] == REPLY
    assert saved["draft"]["reply"]["capture"] == "RECEIPTCAPTURE123"
    assert saved["draft"]["reply"]["review_required"] is True
    # A later ordinary correction must not silently retarget that old draft.
    corrected = correct_payment_details(path, "OTHERCAPTURE123", "20.00",
                                         "Correcting a mismatched receipt reference.", len(current["history"]))
    assert store.detail(path)[1]["draft"]["reply"]["capture"] != corrected["capture_id"]


def test_later_link_allows_existing_explicit_approval_without_payment_attempt(tmp_path):
    path, case = opened(tmp_path, "KNOWNCAPTURE123")
    case = human_review_case(path, WORDS, True, expected_case_revision=len(case["history"]))
    case = correct_payment_details(path, "KNOWNCAPTURE123", "38.00",
                                   "Receipt confirms this payment and merchant limit.", len(case["history"]))
    approved = approve_case(path, "12.00", approval_phrase(case, "12.00"),
                            expected_case_revision=len(case["history"]))
    assert approved["approved_cents"] == 1200 and approved["state"] == "APPROVED"
    assert not approved["request_id"] and not approved["refund_id"]
