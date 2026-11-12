"""Merchant-reported external replies never mutate a payment or resolve support."""
import uuid
import copy
import json
import pytest
from returnlight_app.returnlight import CaseChanged, CaseError, new_case, case_snapshot, record_merchant_reply, add_buyer_followup
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_record import render_case_record

TEXT = "Hi Maya,\nI have arranged the replacement shade. Please keep the working base.\nThanks, Sam"

@pytest.mark.parametrize("state", ["OPEN", "REVIEWED", "APPROVED", "CLOSED", "UNKNOWN", "PENDING", "COMPLETED", "FAILED"])
def test_reply_preserves_case_decision_and_support(tmp_path, state):
    path = tmp_path / "case.json"
    before = new_case(path, "REPLYCAPTURE123", "49.00", "The shade cracked but the lamp base still works.")
    before.update(state=state, approved_cents=0 if state in ("OPEN", "REVIEWED", "CLOSED") else 1200,
                  support_followup_open=True, review={"kind":"human_selection","quote":before["complaint"],"context":before["complaint"]})
    if state in ("UNKNOWN", "PENDING", "COMPLETED", "FAILED"):
        before.update(request_id="retained-request", refund_id="RETAINEDREFUND")
    path.write_text(json.dumps(before))
    after = record_merchant_reply(path, TEXT, len(before["history"]), True, uuid.uuid4().hex)
    for key, value in before.items():
        if key != "history":
            assert after[key] == value
    assert after["history"][:-1] == before["history"]
    assert after["merchant_replies"][0]["text"] == TEXT
    assert after["merchant_replies"][0]["payment_state"] == state
    assert after["merchant_replies"][0]["source"] == "merchant_reported"
    assert case_snapshot(path) == after

def test_stale_and_replayed_reply_cannot_add_another_record(tmp_path):
    path = tmp_path / "case.json"
    before = new_case(path, "REPLYCAPTURE123", "49.00", "The shade cracked but the lamp base still works.")
    saved = record_merchant_reply(path, TEXT, len(before["history"]), True, uuid.uuid4().hex)
    with pytest.raises(CaseChanged):
        record_merchant_reply(path, TEXT, len(before["history"]), True, uuid.uuid4().hex)
    changed = add_buyer_followup(path, "Please hold the replacement until I confirm the colour.", len(saved["history"]))
    with pytest.raises(CaseChanged):
        record_merchant_reply(path, TEXT, len(saved["history"]), True, uuid.uuid4().hex)
    assert case_snapshot(path) == changed
    assert len(changed["merchant_replies"]) == 1

@pytest.mark.parametrize("text,confirmation", [("",True),(" "*12,True),("x"*3001,True),(TEXT,False),(TEXT,1)])
def test_invalid_or_unconfirmed_reply_leaves_file_unchanged(tmp_path,text,confirmation):
    path = tmp_path / "case.json"
    before = new_case(path, "REPLYCAPTURE123", "49.00", "The shade cracked but the lamp base still works.")
    raw=path.read_bytes()
    with pytest.raises(CaseError):
        record_merchant_reply(path,text,len(before["history"]),confirmation,uuid.uuid4().hex)
    assert path.read_bytes()==raw

def test_route_snapshot_and_offline_record_keep_exact_escaped_words(tmp_path):
    cases=LocalCases(tmp_path)
    created=cases.create({"capture_id":"REPLYCAPTURE123","ceiling":"49.00","complaint":"The shade cracked but the lamp base still works."})
    reply='We will send the <blue> shade & keep the base.\nNo refund was sent.'
    saved=cases.action(created["key"],"merchant-reply",{"text":reply,"case_revision":len(created["case"]["history"]),"confirmed_sent":True,"record_id":uuid.uuid4().hex})["case"]
    rendered=render_case_record(saved)
    assert "Merchant reply" in rendered
    assert "&lt;blue&gt; shade &amp;" in rendered and "<blue>" not in rendered
    assert "Recorded as sent outside Returnlight" in rendered
    assert cases.detail(created["key"])["case"]["merchant_replies"][0]["text"]==reply
