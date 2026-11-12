from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from returnlight_app.returnlight import (
    CaseChanged, CaseError, add_buyer_followup, approval_phrase, approve_case,
    case_snapshot, close_case, human_review_case, review_case, validate_model_review,
)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

MESSAGE = "The linen pouch arrived with a <blue> stain. Please send cleaning guidance before considering a refund."
FOLLOWUP = "I have tried the guidance and would prefer a replacement pouch now."


@pytest.fixture
def opened(tmp_path):
    def forbidden():
        raise AssertionError("Manual review must not contact a model or payment provider")
    service = LocalCases(tmp_path, reviewer_factory=forbidden, gateway_factory=forbidden)
    created = service.create({"capture_id": "SYNTHETICMANUAL41", "ceiling": "38.50",
                              "complaint": MESSAGE})
    return service, created["key"], service.path(created["key"])


def manual(path, quote=MESSAGE, confirmed=True, revision=None):
    revision = len(case_snapshot(path)["history"]) if revision is None else revision
    return human_review_case(path, quote, confirmed, expected_case_revision=revision)


def test_manual_review_and_approval_use_the_actual_service_without_inference(opened):
    service, key, path = opened
    case = service.action(key, "human-review", {
        "quote": MESSAGE, "confirmed": True, "case_revision": 1})["case"]
    assert case["state"] == "REVIEWED"
    assert case["review"] == {"kind": "human_selection", "quote": MESSAGE,
                              "context": MESSAGE, "quote_start": 0}
    assert case["approved_cents"] == 0
    assert not case.get("request_id") and not case.get("refund_id")
    assert case["history"][-1]["event"] == "merchant.reviewed"
    result = service.action(key, "approve", {
        "amount": "3.25", "confirmation": approval_phrase(case, "3.25"),
        "case_revision": len(case["history"])})["case"]
    assert result["state"] == "APPROVED" and result["approved_cents"] == 325
    assert not result.get("request_id") and not result.get("refund_id")


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_manual_review_requires_an_explicit_confirmation(opened, confirmed):
    _, _, path = opened
    before = path.read_bytes()
    with pytest.raises(CaseError, match="Confirm"):
        manual(path, confirmed=confirmed)
    assert path.read_bytes() == before


def test_manual_review_preserves_new_evidence_when_revision_or_quote_is_stale(opened):
    _, _, path = opened
    add_buyer_followup(path, FOLLOWUP, 1)
    before = path.read_bytes()
    with pytest.raises(CaseChanged):
        manual(path, revision=1)
    with pytest.raises(CaseChanged):
        manual(path, quote=MESSAGE)
    assert path.read_bytes() == before
    case = manual(path, quote=FOLLOWUP)
    assert case["review"]["quote"] == FOLLOWUP
    assert case["complaint"] == MESSAGE


@pytest.mark.parametrize("outcome", ["approved", "closed"])
def test_manual_review_cannot_replace_a_finished_merchant_decision(opened, outcome):
    _, _, path = opened
    case = manual(path)
    if outcome == "approved":
        approve_case(path, "3.25", approval_phrase(case, "3.25"),
                     expected_case_revision=len(case["history"]))
    else:
        close_case(path, "The buyer accepted cleaning guidance without a refund.",
                   "CLOSE " + case["id"][:8], expected_case_revision=len(case["history"]))
    before = path.read_bytes()
    with pytest.raises(CaseError, match="unapproved"):
        manual(path)
    assert path.read_bytes() == before


def test_manual_review_finishes_while_ai_is_running_and_late_ai_cannot_replace_it(opened):
    _, _, path = opened
    started, release = Event(), Event()

    class SlowModel:
        def review(self, complaint):
            started.set()
            assert release.wait(5)
            return {"kind": "source_selection", "quote": MESSAGE}

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(review_case, path, SlowModel())
        try:
            assert started.wait(5)
            accepted = manual(path)
        finally:
            release.set()
        with pytest.raises(CaseChanged):
            pending.result(timeout=5)
    assert case_snapshot(path) == accepted
    assert [e["event"] for e in accepted["history"]] == ["case.opened", "merchant.reviewed"]


def test_model_output_cannot_claim_a_merchant_review():
    with pytest.raises(CaseError):
        validate_model_review({"kind": "human_selection", "quote": MESSAGE}, MESSAGE)


def test_private_record_distinguishes_current_and_superseded_merchant_reviews(opened):
    _, _, path = opened
    case = manual(path)
    record = render_case_record(case)
    assert "<h2>Merchant review</h2>" in record
    assert "Reviewed buyer message" in record
    assert "&lt;blue&gt;" in record and "<blue>" not in record
    assert "<h2>Model review</h2>" not in record
    changed = add_buyer_followup(path, FOLLOWUP, len(case["history"]))
    record = render_case_record(changed)
    assert "Previous merchant review · superseded" in record
    assert "Previous model review · superseded" not in record
