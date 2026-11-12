"""Offline chronology fixtures; actual recording is covered by the installed lifecycle."""
import json
import uuid
from returnlight_app.returnlight_record import render_case_record
from test_returnlight_postpayment_support import attempted
from returnlight_app.returnlight import add_post_payment_message, resolve_post_payment_support


def reply_fixture(case, text, timestamp):
    case.setdefault("merchant_replies", []).append({
        "id": uuid.uuid4().hex, "text": text, "recorded_at": timestamp,
        "case_revision": len(case["history"]), "capture_id": case["capture_id"],
        "payment_state": case["state"], "source":"merchant_reported", "approved_cents": case["approved_cents"],
        "refund_id": case.get("refund_id"),
        "through_buyer_followup": len(case.get("buyer_followups", [])),
        "through_post_payment_message": len(case.get("post_payment_messages", [])),
    })
    case["history"].append({"event": "merchant.reply_recorded", "detail": "External reply recorded.", "recorded_at": timestamp})


def test_replies_keep_their_place_around_support_handling_despite_clock_order(tmp_path):
    _, _, path, current = attempted(tmp_path)
    first = "First buyer asks about the recorded refund."
    sent = "First merchant replies with the original reference."
    handled = "Merchant separately marks that bank question handled."
    second = "Second buyer asks a distinct delivery question."
    sent_again = "Second merchant replies about the delivery question."
    current = add_post_payment_message(path, first, len(current["history"]))
    reply_fixture(current, sent, "2026-11-11T11:00:00Z")
    path.write_text(json.dumps(current))
    current = resolve_post_payment_support(path, handled, len(current["history"]))
    current = add_post_payment_message(path, second, len(current["history"]))
    reply_fixture(current, sent_again, "2026-11-11T09:00:00Z")
    before = json.dumps(current, sort_keys=True)
    html = render_case_record(current)
    positions = [html.index(text) for text in (first, sent, handled, second, sent_again)]
    assert positions == sorted(positions)
    assert html.count("Merchant reply") == 2
    assert "Support follow-up open" in html
    assert "Recorded as sent outside Returnlight" in html
    assert json.dumps(current, sort_keys=True) == before


def test_reply_html_is_escaped_and_retains_exact_newlines(tmp_path):
    _, _, _, case = attempted(tmp_path)
    reply_fixture(case, 'Hello,\n<buyer> & "receipt" are the exact words.', "2026-11-11T10:00:00Z")
    html = render_case_record(case)
    assert 'Hello,\n&lt;buyer&gt; &amp; &quot;receipt&quot; are the exact words.' in html
    assert "<buyer>" not in html
    assert html.index("Merchant reply") < html.index("Payment references")


def test_reopened_buyer_message_counts_once_between_recorded_replies(tmp_path):
    from returnlight_app.returnlight_web import LocalCases
    cases = LocalCases(tmp_path)
    result = cases.create({"capture_id": "REPLYREOPEN123", "ceiling": "12.00",
                           "complaint": "Please replace the lid rather than refunding this purchase."})
    key = result["key"]; current = result["case"]; path = cases.path(key)
    current = cases.action(key, "human-review", {"quote": current["complaint"], "confirmed": True,
                                              "case_revision": len(current["history"])})["case"]
    current = cases.action(key, "close", {"reason": "We agreed to send a replacement lid.",
                                        "confirmation": "CLOSE " + current["id"][:8],
                                        "case_revision": len(current["history"])})["case"]
    reply_fixture(current, "Merchant confirms the original replacement agreement.", "2026-11-11T11:00:00Z")
    path.write_text(json.dumps(current))
    current = cases.action(key, "reopen", {"text": "Buyer reports that the replacement lid has now arrived cracked.",
                                         "case_revision": len(current["history"])})["case"]
    reply_fixture(current, "Merchant asks for a photo of the replacement damage.", "2026-11-11T11:00:00Z")
    html = render_case_record(current)
    first = html.index("Merchant confirms the original replacement agreement.")
    buyer = html.index("Buyer reports that the replacement lid has now arrived cracked.")
    last = html.index("Merchant asks for a photo of the replacement damage.")
    assert first < buyer < last
    assert html.count("Merchant reply") == 2


def test_record_exact_reply_is_durable_idempotent_and_keeps_payment_and_support(tmp_path):
    import pytest
    from returnlight_app.returnlight import record_merchant_reply, CaseError, buyer_evidence
    _, _, path, case = attempted(tmp_path)
    case = add_post_payment_message(path, "Where is my refund?", len(case["history"]))
    before = json.loads(path.read_text())
    text = "  Hello,\nYour bank is still processing this.  "
    key = uuid.uuid4().hex
    current = record_merchant_reply(path, text, len(case["history"]), True, key)
    assert current["merchant_replies"][0]["text"] == text
    assert text not in buyer_evidence(current)
    for field in ("state", "approved_cents", "refund_id", "request_id", "support_followup_open", "review"):
        assert current.get(field) == before.get(field)
    stored = path.read_bytes()
    assert record_merchant_reply(path, text, len(case["history"]), True, key) == current
    assert path.read_bytes() == stored
    with pytest.raises(CaseError):
        record_merchant_reply(path, text + "changed", len(current["history"]), True, key)
    with pytest.raises(CaseError):
        record_merchant_reply(path, text, len(case["history"]), True, uuid.uuid4().hex)
    with pytest.raises(CaseError):
        record_merchant_reply(path, text, len(current["history"]), False, uuid.uuid4().hex)
    assert path.read_bytes() == stored


def test_saved_reply_rechecks_changed_case_without_rewriting_words(tmp_path):
    _, _, path, case = attempted(tmp_path)
    words = "I will retain the original refund reference."
    draft = {"draft": {"reply": {"capture": case["capture_id"], "text": words,
             "version": json.dumps([case["capture_id"], case["state"], case["approved_cents"],
                                    case.get("refund_id"), len(case["history"])]),
             "review_required": False}}}
    original = render_case_record(case, working_draft=draft)
    assert "Case changed · check this saved reply" not in original
    changed = add_post_payment_message(path, "Correction: please do not contact the bank.", len(case["history"]))
    record = render_case_record(changed, working_draft=draft)
    assert "Case changed · check this saved reply" in record
    assert words in record and draft["draft"]["reply"]["text"] == words
    draft["draft"]["reply"]["capture"] = "DIFFERENTCAPTURE"
    assert "Saved unsent reply" not in render_case_record(changed, working_draft=draft)
