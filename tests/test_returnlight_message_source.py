import json
from pathlib import Path
import pytest
from returnlight_app.returnlight import (
    new_case, case_snapshot, add_buyer_followup, add_post_payment_message,
    reopen_case, human_review_case, close_case, CaseError,
)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

SOURCE = {"sender": "Maya <maya@example.com>", "subject": "The replacement lid",
          "attachments_omitted": 1}
WORDS = "The replacement lid is still too small for the original bowl."

@pytest.mark.parametrize("kind", ["followup", "reopen", "postpayment"])
def test_email_context_is_saved_with_its_message_and_record(tmp_path, kind):
    key = "a" * 32
    path = tmp_path / (key + ".json")
    new_case(path, "SOURCECAPTURE123", "42.00", "The bowl lid arrived broken. Please send a replacement.")
    if kind == "reopen":
        c = case_snapshot(path)
        human_review_case(path, c["complaint"], True, expected_case_revision=len(c["history"]))
        c = case_snapshot(path)
        close_case(path, "The replacement was arranged.", "CLOSE " + c["id"][:8],
                   expected_case_revision=len(c["history"]))
    elif kind == "postpayment":
        c = json.loads(path.read_text())
        c.update(state="PENDING", request_id="synthetic-request", refund_id="SYNTHETICREFUND123", approved_cents=2100)
        path.write_text(json.dumps(c))
    before = case_snapshot(path)
    action = {"followup": add_buyer_followup, "reopen": reopen_case,
              "postpayment": add_post_payment_message}[kind]
    action(path, WORDS, len(before["history"]), imported_email=SOURCE)
    after = case_snapshot(path)
    entry = after["post_payment_messages" if kind == "postpayment" else "buyer_followups"][-1]
    assert entry["imported_email"] == SOURCE and entry["text"] == WORDS
    assert "imported_email" not in after
    record = render_case_record(after)
    assert SOURCE["subject"] in record and "maya@example.com" in record
    assert "1 attachment not imported" in record
    assert LocalCases(tmp_path).summary(key)["email_contexts"] == [SOURCE]
    for field in ["capture_id", "request_id", "refund_id"]:
        assert after.get(field) == before.get(field)
    if kind == "postpayment":
        assert after["state"] == before["state"] and after["approved_cents"] == before["approved_cents"]

@pytest.mark.parametrize("source", [
    {"sender": "Maya <maya@example.com>", "subject": "Subject", "attachments_omitted": -1},
    {"sender": "Maya <maya@example.com>", "subject": "Subject", "attachments_omitted": True},
    {"sender": "Maya\nAnother header", "subject": "Subject"},
])
def test_invalid_message_source_changes_no_case(tmp_path, source):
    path = tmp_path / ("b"*32 + ".json")
    new_case(path, "SOURCECAPTURE123", "42.00", "Please replace the lid that arrived broken.")
    before = path.read_bytes();case = case_snapshot(path)
    with pytest.raises(CaseError):
        add_buyer_followup(path, WORDS, len(case["history"]), imported_email=source)
    assert path.read_bytes() == before

@pytest.mark.parametrize("kind", ["followup", "reopen", "postpayment"])
def test_buyer_message_recording_recovers_exact_original_once(tmp_path, kind):
    key="a"*32;path=tmp_path/(key+".json")
    new_case(path,"SOURCECAPTURE123","42.00","The bowl lid arrived broken. Please send a replacement.")
    if kind=="reopen":
        c=case_snapshot(path);human_review_case(path,c["complaint"],True,expected_case_revision=len(c["history"]))
        c=case_snapshot(path);close_case(path,"Replacement arranged.","CLOSE "+c["id"][:8],expected_case_revision=len(c["history"]))
    elif kind=="postpayment":
        c=case_snapshot(path);c.update(state="PENDING",request_id="synthetic-request",refund_id="SYNTHETICREFUND123",approved_cents=2100);path.write_text(json.dumps(c))
    before=case_snapshot(path);action={"followup":add_buyer_followup,"reopen":reopen_case,"postpayment":add_post_payment_message}[kind]
    app=LocalCases(tmp_path)
    payload={"text":WORDS,"case_revision":len(before["history"]),"imported_email":SOURCE,"record_id":"d"*32}
    app.action(key,kind,payload)
    saved=path.read_bytes();current=case_snapshot(path)
    app.action(key,kind,payload)
    assert path.read_bytes()==saved
    with pytest.raises(CaseError): action(path,WORDS+" Changed",len(current["history"]),imported_email=SOURCE,record_id="d"*32)
    with pytest.raises(CaseError): action(path,WORDS,len(current["history"]),imported_email={**SOURCE,"subject":"Different source"},record_id="d"*32)
    assert path.read_bytes()==saved
    with pytest.raises(CaseError): action(path,WORDS,len(before["history"]),imported_email=SOURCE,record_id="f"*32)
    for invalid in ("", "bad", 7, True):
        with pytest.raises(CaseError): action(path,WORDS,len(current["history"]),imported_email=SOURCE,record_id=invalid)
    assert path.read_bytes()==saved
    # Identical words are still a distinct message when deliberately recorded later.
    later=add_post_payment_message if kind=="postpayment" else add_buyer_followup
    later(path,WORDS,len(current["history"]),imported_email=SOURCE,record_id="e"*32)
    rows=case_snapshot(path)["post_payment_messages" if kind=="postpayment" else "buyer_followups"]
    assert [row["record_id"] for row in rows]==["d"*32,"e"*32]
