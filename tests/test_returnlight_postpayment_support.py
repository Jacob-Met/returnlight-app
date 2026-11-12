"""Post-payment support uses authored payment states; no provider or model calls."""
import json
import pytest
from returnlight_app.returnlight import (CaseError, new_case, case_snapshot, add_post_payment_message,
    resolve_post_payment_support, review_case, approve_case, execute_case)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

MESSAGE = "The refund is still missing from my statement. Please check its original reference."
REASON = "Compared the recorded refund reference and explained the recorded pending status."

def attempted(tmp_path, state="COMPLETED"):
    cases=LocalCases(tmp_path)
    result=cases.create({"capture_id":"SUPPORTCAPTURE2122","ceiling":"28.50",
                         "complaint":"The travel pouch clasp is broken. Please refund this damaged item."})
    path=cases.path(result["key"]); case=result["case"]
    case.update(state=state,approved_cents=2850,request_id="authored-request",refund_id="AUTHOREDREFUND")
    path.write_text(json.dumps(case))
    return cases,result["key"],path,case

@pytest.mark.parametrize("state",["UNKNOWN","PENDING","COMPLETED","FAILED"])
def test_support_lifecycle_preserves_payment_and_never_reopens_dispatch(tmp_path,state):
    cases,key,path,before=attempted(tmp_path,state)
    after=cases.action(key,"postpayment",{"text":MESSAGE,"case_revision":len(before["history"])})["case"]
    for field in ("id","capture_id","state","review","approved_cents","request_id","refund_id","complaint"):
        assert after[field]==before[field]
    assert after["history"][:-1]==before["history"]
    assert after["post_payment_messages"]==[{"text":MESSAGE,"payment_state":state,
                                           "recorded_at":after["history"][-1]["recorded_at"]}]
    assert cases.summary(key)["queue"]=="followup"
    assert MESSAGE in cases.summary(key)["complaint"]
    class Never:
        def __getattr__(self,name):
            raise AssertionError("support must never permit another model/provider action")
    with pytest.raises(CaseError): review_case(path,Never())
    with pytest.raises(CaseError): approve_case(path,"1.00","anything")
    with pytest.raises(CaseError): execute_case(path,Never())
    resolved=cases.action(key,"supporthandled",{"reason":REASON,"case_revision":len(after["history"])})["case"]
    assert resolved["state"]==state and not resolved["support_followup_open"]
    assert resolved["request_id"]==before["request_id"] and resolved["refund_id"]==before["refund_id"]
    assert cases.summary(key)["queue"]==("finished" if state=="COMPLETED" else "followup")
    assert resolved["support_resolutions"][0]["reason"]==REASON
    assert resolved["support_resolutions"][0]["through_message"]==1
    assert case_snapshot(path)==resolved
    second=add_post_payment_message(path,"The buyer asks for another update after the earlier explanation.",len(resolved["history"]))
    assert second["support_followup_open"] and len(second["post_payment_messages"])==2
    assert second["support_resolutions"]==resolved["support_resolutions"]
    record=render_case_record(second)
    assert MESSAGE in record and REASON in record and "Support follow-up open" in record
    assert "Recorded state" in record and state in record

def test_stale_support_resolution_cannot_hide_new_buyer_message(tmp_path):
    _,_,path,before=attempted(tmp_path)
    first=add_post_payment_message(path,MESSAGE,len(before["history"]))
    second=add_post_payment_message(path,"New buyer evidence arrived while the other merchant tab was open.",len(first["history"]))
    with pytest.raises(CaseError):
        resolve_post_payment_support(path,REASON,len(first["history"]))
    with pytest.raises(CaseError):
        add_post_payment_message(path,MESSAGE,len(first["history"]))
    assert case_snapshot(path)==second

def test_pre_payment_case_uses_existing_evidence_flow_and_record_escapes_support(tmp_path):
    cases=LocalCases(tmp_path)
    result=cases.create({"capture_id":"NEWCAPTURE2122","ceiling":"15.00","complaint":"An unopened case has no payment attempt to follow up."})
    path=cases.path(result["key"]); before=case_snapshot(path)
    with pytest.raises(CaseError):
        add_post_payment_message(path,MESSAGE,len(before["history"]))
    assert case_snapshot(path)==before
    _,_,path,before=attempted(tmp_path/"other")
    updated=add_post_payment_message(path,'Buyer says <script> & quotes must remain exact words.',len(before["history"]))
    record=render_case_record(updated)
    assert "&lt;script&gt; &amp;" in record and "<script>" not in record
    assert "Buyer message after refund attempt 1" in record

@pytest.mark.parametrize("state", ["UNKNOWN", "PENDING", "COMPLETED", "FAILED"])
@pytest.mark.parametrize("text", ["No.", "✓"])
def test_short_postpayment_reply_preserves_payment_and_opens_support(tmp_path, state, text):
    cases, key, path, before = attempted(tmp_path, state)
    after = cases.action(key, "postpayment", {"text": text, "case_revision": len(before["history"])})["case"]
    assert after["post_payment_messages"][-1]["text"] == text
    assert after["support_followup_open"]
    for field in ("id", "capture_id", "state", "approved_cents", "request_id", "refund_id"):
        assert after[field] == before[field]
