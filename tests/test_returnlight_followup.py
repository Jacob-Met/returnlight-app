"""Buyer follow-up behavior; models/providers below are explicit local test doubles."""
import json
import pytest
from returnlight_app.returnlight import (
    CaseError, add_buyer_followup, approval_phrase, approval_revision, approve_case,
    buyer_evidence, case_snapshot, execute_case, new_case, review_case, human_review_case,
)
from returnlight_app.returnlight_record import render_case_record

ORIGINAL = "The speaker stand arrived with a cracked base. I would like a USD 74.50 refund."
FOLLOWUP = "I tightened the base and can use the stand. Please refund only USD 12.50 for the missing rubber foot."

class Model:
    def review(self, text):
        return {"category": "damaged", "summary": "Buyer reports cracked speaker stand.", "quote": "cracked base"}

def make_case(tmp_path):
    path = tmp_path / "speaker.json"
    new_case(path, "FOLLOWUPSPEAKER1", "74.50", ORIGINAL)
    return path

def test_followup_retains_original_and_requires_fresh_review_and_approval(tmp_path):
    path = make_case(tmp_path)
    review_case(path, Model())
    before = case_snapshot(path)
    human_review_case(path, ORIGINAL, True, expected_case_revision=len(before["history"]))
    approved = approve_case(path, "74.50", approval_phrase(before, "74.50"))
    old_revision = approval_revision(approved)
    updated = add_buyer_followup(path, FOLLOWUP, len(approved["history"]))
    assert updated["complaint"] == ORIGINAL
    assert updated["buyer_followups"][0]["text"] == FOLLOWUP
    assert updated["buyer_followups"][0]["superseded_review"] == approved["review"]
    assert updated["buyer_followups"][0]["recorded_at"]
    assert updated["state"] == "OPEN" and updated["review"] is None and updated["approved_cents"] == 0
    assert any(e["event"] == "merchant.approval_withdrawn" and "74.50" in e["detail"] for e in updated["history"])
    with pytest.raises(CaseError, match="Confirm the current"):
        approve_case(path, "12.50", approval_phrase(updated, "12.50"))
    calls = []
    class FollowupModel:
        def review(self, text):
            calls.append(text)
            return {"category": "missing", "summary": "Buyer requests only missing-foot cost.", "quote": FOLLOWUP}
    reviewed = review_case(path, FollowupModel())
    assert calls == [ORIGINAL + "\n\n" + FOLLOWUP]
    assert reviewed["review"]["quote"] == FOLLOWUP
    human_review_case(path, FOLLOWUP, True, expected_case_revision=len(reviewed["history"]))
    reapproved = approve_case(path, "12.50", approval_phrase(reviewed, "12.50"))
    assert approval_revision(reapproved) != old_revision
    class NeverGateway:
        def capture(self, *_):
            raise AssertionError("Stale approval must be refused before provider inspection")
    with pytest.raises(CaseError):
        execute_case(path, NeverGateway(), expected_approval_revision=old_revision,
                     send_confirmation="SEND " + reapproved["id"][:8] + " 74.50")
    saved = case_snapshot(path)
    assert saved == reapproved and saved["request_id"] is None
    record = render_case_record(saved)
    assert ORIGINAL in record and FOLLOWUP in record
    assert "Buyer follow-up 1" in record and "USD 12.50" in record

def test_stale_or_empty_followup_does_not_mutate_case(tmp_path):
    path = make_case(tmp_path)
    before = case_snapshot(path)
    with pytest.raises(CaseError, match="1-15000"):
        add_buyer_followup(path, " ", len(before["history"]))
    first = add_buyer_followup(path, FOLLOWUP, len(before["history"]))
    with pytest.raises(CaseError, match="changed in another tab"):
        add_buyer_followup(path, "Another buyer correction must not overwrite unseen evidence.", len(before["history"]))
    assert case_snapshot(path) == first

@pytest.mark.parametrize("state", ["CLOSED", "UNKNOWN", "PENDING", "COMPLETED", "FAILED"])
def test_terminal_or_attempted_cases_cannot_reopen_by_adding_words(tmp_path, state):
    path = make_case(tmp_path)
    case = case_snapshot(path)
    case["state"] = state
    if state != "CLOSED":
        case["request_id"] = "existing-request"
    path.write_text(json.dumps(case))
    before = path.read_bytes()
    with pytest.raises(CaseError, match="before a refund attempt or case closure"):
        add_buyer_followup(path, FOLLOWUP, len(case["history"]))
    assert path.read_bytes() == before

def test_followup_html_is_literal_and_old_cases_need_no_migration(tmp_path):
    path = make_case(tmp_path)
    before = case_snapshot(path)
    assert buyer_evidence(before) == ORIGINAL
    text = 'The part reads <Foot & Base>; this is the buyer’s correction.'
    updated = add_buyer_followup(path, text, len(before["history"]))
    record = render_case_record(updated)
    assert "&lt;Foot &amp; Base&gt;" in record
    assert "<Foot & Base>" not in record


def test_record_preserves_superseded_evidence_without_legacy_remedy(tmp_path):
    path = make_case(tmp_path)
    review_case(path, Model())
    before = case_snapshot(path)
    updated = add_buyer_followup(path, FOLLOWUP, len(before["history"]))
    prior = updated["buyer_followups"][0]["superseded_review"]
    prior["summary"] = "Earlier <unsafe> interpretation & context"
    prior["suggested_refund_percent"] = "LEGACY_PERCENT_MUST_NOT_RENDER"
    prior["reasoning"] = "LEGACY_REMEDY_MUST_NOT_RENDER"
    record = render_case_record(updated)
    assert "Previous model review · superseded" in record
    assert '<details class="past-review">' in record
    assert "Earlier &lt;unsafe&gt; interpretation &amp; context" in record
    assert "cracked base" in record
    assert "LEGACY_PERCENT_MUST_NOT_RENDER" not in record
    assert "LEGACY_REMEDY_MUST_NOT_RENDER" not in record
    assert "<h2>Model review</h2>" not in record
    assert updated["review"] is None and updated["approved_cents"] == 0


def test_complete_conversation_budget_includes_message_separators(tmp_path):
    path = tmp_path / "long.json"
    new_case(path, "LONGTHREADCAP123", "36.00", "b" * 14997)
    before = case_snapshot(path)
    accepted = add_buyer_followup(path, "Y", len(before["history"]))
    assert len(buyer_evidence(accepted)) == 15000
    original_bytes = path.read_bytes()
    with pytest.raises(CaseError, match="too much buyer text"):
        add_buyer_followup(path, "Z", len(accepted["history"]))
    assert path.read_bytes() == original_bytes
