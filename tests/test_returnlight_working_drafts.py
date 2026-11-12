import copy
import json
import os
import stat
from pathlib import Path
import pytest
from returnlight_app.returnlight import CaseError, CaseChanged, new_case, add_buyer_followup, case_snapshot
from returnlight_app.returnlight_working_drafts import WorkingDraftStore

@pytest.fixture
def work(tmp_path):
    path=tmp_path/("a"*32+".json")
    case=new_case(path,"DRAFTCAPTURE01","40.00","The ceramic bowl arrived with a broken handle.")
    return path,case,WorkingDraftStore(tmp_path)

def payload(case,version=None,text="Please keep the box while I check the replacement."):
    return {"case_revision":len(case["history"]),"draft_version":version,"draft":{
        "decision":{"capture":case["capture_id"],"amount":"12.80","reason":"","revision":len(case["history"]),"active":"amount"},
        "reply":{"capture":case["capture_id"],"text":text,"version":json.dumps([case["capture_id"],case["state"],0,None,len(case["history"])])}}}

def test_save_restart_exact_retry_and_source_untouched(work):
    path,case,store=work;before=path.read_bytes()
    assert store.detail(path)[1]["version"] is None
    assert not store.directory.exists()
    request=payload(case);first=store.save(path,request)
    assert store.save(path,request)==first
    restored=WorkingDraftStore(path.parent).detail(path)[1]
    assert restored==first and path.read_bytes()==before
    assert stat.S_IMODE(store.directory.stat().st_mode)==0o700
    assert stat.S_IMODE((store.directory/path.name).stat().st_mode)==0o600

def test_concurrent_window_cannot_replace_or_delete_newer_work(work):
    path,case,store=work
    first=store.save(path,payload(case))
    newer=store.save(path,payload(case,first["version"],"A replacement is available after I check the buyer conditions."))
    with pytest.raises(CaseChanged,match="another window"):
        store.save(path,payload(case,first["version"],"Old window text"))
    with pytest.raises(CaseChanged,match="another window"):
        store.save(path,{"case_revision":1,"draft_version":first["version"],"draft":{"decision":None,"reply":None}})
    assert store.detail(path)[1]==newer

def test_case_change_requires_refresh_without_losing_saved_work(work):
    path,case,store=work;first=store.save(path,payload(case))
    add_buyer_followup(path,"Please keep the replacement conditional on Friday delivery.",len(case["history"]))
    before=path.read_bytes()
    with pytest.raises(CaseChanged,match="case changed"):
        store.save(path,payload(case,first["version"],"A new draft"))
    assert store.detail(path)[1]==first and path.read_bytes()==before

def test_discard_survives_restart_without_clearing_other_part(work):
    path,case,store=work;first=store.save(path,payload(case))
    change=payload(case,first["version"]);change["draft"]["reply"]=None
    second=store.save(path,change)
    assert second["draft"]["decision"]==first["draft"]["decision"]
    change={"case_revision":1,"draft_version":second["version"],"draft":{"decision":None,"reply":None}}
    saved=store.save(path,change)
    assert WorkingDraftStore(path.parent).detail(path)[1]==saved

@pytest.mark.parametrize("part,field,value",[("reply","text","x"*3001),("decision","amount","x"*257),("decision","revision",True),("decision","active","approve"),("reply","capture","ANOTHERCAPTURE")])
def test_reject_invalid_or_wrong_payment_without_writes(work,part,field,value):
    path,case,store=work;request=payload(case);request["draft"][part][field]=value;before=path.read_bytes()
    with pytest.raises(CaseError):store.save(path,request)
    assert not store.directory.exists() and path.read_bytes()==before

def test_damaged_work_does_not_hide_case_or_get_overwritten(work):
    path,case,store=work;store.save(path,payload(case));draft=store.directory/path.name;draft.write_text("broken")
    actual,saved=store.detail(path)
    assert actual==case and "could not be read" in saved["error"]
    with pytest.raises(CaseError):store.save(path,payload(case))
    assert draft.read_text()=="broken"

def test_case_identity_and_symlinks_do_not_cross_load(work,tmp_path):
    path,case,store=work;store.save(path,payload(case))
    other=tmp_path/("b"*32+".json");new_case(other,"OTHERCAPTURE2","40.00","Another buyer reports the same broken bowl.")
    (store.directory/other.name).write_bytes((store.directory/path.name).read_bytes())
    assert "error" in store.detail(other)[1]
    (store.directory/other.name).unlink();(store.directory/other.name).symlink_to(store.directory/path.name)
    assert "error" in store.detail(other)[1]

def test_recorded_reply_retires_only_exact_saved_work(work):
    from returnlight_app.returnlight import record_merchant_reply
    path,case,store=work;request=payload(case)
    request["draft"]["reply"]["record_id"]="f"*32
    first=store.save(path,request)
    record_merchant_reply(path,request["draft"]["reply"]["text"],1,True,"f"*32)
    # Even interrupted cleanup cannot resurrect the already recorded reply.
    assert store.detail(path)[1]["draft"]["reply"] is None
    store.retire_completed(path)
    disk=json.loads((store.directory/path.name).read_text())
    assert disk["draft"]["reply"] is None
    assert disk["draft"]["decision"]==first["draft"]["decision"]

def test_recorded_reply_does_not_remove_newer_words(work):
    from returnlight_app.returnlight import record_merchant_reply
    path,case,store=work;first=store.save(path,payload(case))
    newer=store.save(path,payload(case,first["version"],"A newer unfinished reply must remain intact."))
    record_merchant_reply(path,first["draft"]["reply"]["text"],1,True,"e"*32)
    store.retire_completed(path)
    assert store.detail(path)[1]==newer

def test_approved_draft_does_not_return_after_withdrawal(work):
    from returnlight_app.returnlight import human_review_case, approve_case, approval_phrase, approval_revision, revise_approval
    path,case,store=work
    case=human_review_case(path,case["complaint"],True,expected_case_revision=len(case["history"]))
    saved=store.save(path,payload(case))
    case=approve_case(path,"12.80",approval_phrase(case,"12.80"),expected_case_revision=len(case["history"]))
    assert store.detail(path)[1]["draft"]["decision"] is None
    case=revise_approval(path,"CHANGE "+case["id"][:8]+" 12.80",approval_revision(case))
    assert store.detail(path)[1]["draft"]["decision"] is None
    store.retire_completed(path)
    actual=store.detail(path)[1]
    assert actual["draft"]["reply"]==saved["draft"]["reply"]
    fresh=payload(case,actual["version"]);fresh["draft"]["decision"]["amount"]="9.20"
    assert store.save(path,fresh)["draft"]["decision"]["amount"]=="9.20"

def test_closed_draft_does_not_return_after_reopening(work):
    from returnlight_app.returnlight import close_case, reopen_case
    path,case,store=work
    saved=store.save(path,payload(case))
    case=close_case(path,"A replacement was agreed with the buyer.","CLOSE "+case["id"][:8],expected_case_revision=len(case["history"]))
    case=reopen_case(path,"The replacement is delayed; please reconsider the refund.",len(case["history"]))
    assert store.detail(path)[1]["draft"]["decision"] is None
    store.retire_completed(path)
    assert store.detail(path)[1]["draft"]["reply"]==saved["draft"]["reply"]

def test_saved_work_summary_preserves_payment_queue(work):
    from returnlight_app.returnlight_web import LocalCases
    path,case,store=work
    app=LocalCases(path.parent)
    before=app.summary(path.stem)
    assert not before["saved_reply"] and not before["saved_decision"]
    store.save(path,payload(case))
    after=app.summary(path.stem)
    assert after["saved_reply"] and after["saved_decision"]
    assert before["queue"]==after["queue"]

def test_approval_consumes_amount_but_preserves_unsubmitted_reason(work):
    from returnlight_app.returnlight import human_review_case, approve_case, approval_phrase, approval_revision, revise_approval
    path,case,store=work
    case=human_review_case(path,case["complaint"],True,expected_case_revision=len(case["history"]))
    request=payload(case)
    request["draft"]["decision"]["reason"]="The buyer may accept a replacement; I am still checking delivery."
    saved=store.save(path,request)
    case=approve_case(path,"12.80",approval_phrase(case,"12.80"),expected_case_revision=len(case["history"]))
    visible=store.detail(path)[1]["draft"]["decision"]
    assert visible["amount"]=="" and visible["reason"]==request["draft"]["decision"]["reason"]
    assert visible["revision"]==len(case["history"])
    store.retire_completed(path)
    case=revise_approval(path,"CHANGE "+case["id"][:8]+" 12.80",approval_revision(case))
    restored=WorkingDraftStore(path.parent).detail(path)[1]
    assert restored["draft"]["decision"]==visible
    assert restored["draft"]["reply"]==saved["draft"]["reply"]
    stale=payload(case,restored["version"]);stale["draft"]["decision"]=request["draft"]["decision"]
    with pytest.raises(CaseChanged): store.save(path,stale)

def test_saved_followup_restart_consumption_and_concurrent_draft(work):
    from returnlight_app.returnlight import close_case, update_merchant_followup
    path,case,store=work
    case=close_case(path,"The buyer accepted a replacement shade.","CLOSE "+case["id"][:8],expected_case_revision=len(case["history"]),followup_needed=True)
    note={"capture":case["capture_id"],"text":"The replacement was delivered; awaiting confirmation.","revision":len(case["history"]),"completed":True}
    req={"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":None,"followup":note}}
    before=path.read_bytes();saved=store.save(path,req)
    assert WorkingDraftStore(path.parent).detail(path)[1]==saved and path.read_bytes()==before
    newer={**note,"text":"The replacement fits, but the buyer asks about disposal."}
    req2={**req,"draft_version":saved["version"],"draft":{**req["draft"],"followup":newer}}
    saved2=store.save(path,req2)
    with pytest.raises(CaseChanged):store.save(path,{**req,"draft":{"decision":None,"reply":None,"followup":None}})
    update_merchant_followup(path,note["text"],True,len(case["history"]))
    store.retire_completed(path)
    assert store.detail(path)[1]["draft"]["followup"]==newer

@pytest.mark.parametrize("completed", [False, True])
def test_exact_recorded_followup_stays_retired_after_restart(work, completed):
    from returnlight_app.returnlight import close_case, update_merchant_followup
    path,case,store=work
    case=close_case(path,"The buyer accepted a replacement shade.","CLOSE "+case["id"][:8],expected_case_revision=len(case["history"]),followup_needed=True)
    note={"capture":case["capture_id"],"text":"The buyer confirmed the replacement fits.","revision":len(case["history"]),"completed":True}
    store.save(path,{"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":None,"followup":note}})
    update_merchant_followup(path,note["text"],completed,len(case["history"]))
    assert store.detail(path)[1]["draft"]["followup"] is None
    store.retire_completed(path)
    assert WorkingDraftStore(path.parent).detail(path)[1]["draft"]["followup"] is None

def test_older_reply_client_preserves_saved_followup(work):
    from returnlight_app.returnlight import close_case
    path,case,store=work
    case=close_case(path,"The buyer accepted a replacement shade.","CLOSE "+case["id"][:8],expected_case_revision=len(case["history"]),followup_needed=True)
    note={"capture":case["capture_id"],"text":"Awaiting confirmation that the replacement fits.","revision":len(case["history"]),"completed":True}
    saved=store.save(path,{"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":None,"followup":note}})
    changed=store.save(path,{"case_revision":len(case["history"]),"draft_version":saved["version"],"draft":{"decision":None,"reply":None}})
    assert changed["draft"]["followup"]==note

def condition_request(case, version=None):
    return {"case_revision":len(case["history"]),"draft_version":version,"draft":{
        "decision":None,"reply":None,"conditions":{"capture":case["capture_id"],"revision":len(case["history"]),
        "items":[{"message_index":0,"source_start":0,"quote":case["complaint"]}]}}}

def test_unfinished_conditions_restart_without_review_or_payment(work):
    path,case,store=work;before=path.read_bytes()
    saved=store.save(path,condition_request(case))
    assert WorkingDraftStore(path.parent).detail(path)[1]==saved
    assert path.read_bytes()==before and case_snapshot(path)["state"]=="OPEN"
    from returnlight_app.returnlight_web import LocalCases
    assert LocalCases(path.parent).summary(path.stem)["saved_conditions"]
    old=payload(case,saved["version"])
    oldsaved=store.save(path,old)
    assert oldsaved["draft"]["conditions"]==saved["draft"]["conditions"]
    empty={**old,"draft_version":oldsaved["version"],"draft":{**old["draft"],"conditions":None}}
    assert store.save(path,empty)["draft"]["conditions"] is None

def test_unfinished_conditions_retire_on_new_buyer_or_explicit_review(work):
    from returnlight_app.returnlight import human_review_case
    path,case,store=work;request=condition_request(case);store.save(path,request)
    case=add_buyer_followup(path,"Please do not replace it after all.",len(case["history"]))
    assert store.detail(path)[1]["draft"]["conditions"] is None
    with pytest.raises(CaseChanged):store.save(path,{**request,"case_revision":len(case["history"])})
    fresh=condition_request(case,store.detail(path)[1]["version"]);store.save(path,fresh)
    human_review_case(path,case["buyer_followups"][-1]["text"],True,expected_case_revision=len(case["history"]),
                      selected_conditions=fresh["draft"]["conditions"]["items"])
    assert store.detail(path)[1]["draft"]["conditions"] is None
    store.retire_completed(path)
    assert WorkingDraftStore(path.parent).detail(path)[1]["draft"]["conditions"] is None

@pytest.mark.parametrize("change",[{"quote":"Words the buyer never wrote"},{"source_start":True},{"message_index":1}])
def test_unfinished_condition_exact_source_guards(work,change):
    path,case,store=work;request=condition_request(case);request["draft"]["conditions"]["items"][0].update(change)
    before=path.read_bytes()
    with pytest.raises(CaseError):store.save(path,request)
    assert path.read_bytes()==before and not store.directory.exists()

def test_unfinished_conditions_do_not_overwrite_newer_window(work):
    path,case,store=work;first=store.save(path,condition_request(case))
    changed=condition_request(case,first["version"]);changed["draft"]["conditions"]["items"][0]["quote"]="The ceramic bowl"
    second=store.save(path,changed)
    with pytest.raises(CaseChanged):store.save(path,{**condition_request(case,first["version"]),"draft":{"decision":None,"reply":None,"conditions":None}})
    assert store.detail(path)[1]==second

def test_saved_conditions_survive_ai_reading_and_dismissal(work):
    from returnlight_app.returnlight import review_case, dismiss_review
    path,case,store=work;request=condition_request(case);saved=store.save(path,request)
    class ExactSuggestion:
        def review(self,text):
            return {"kind":"source_selection","quote":"a broken handle."}
    suggested=review_case(path,ExactSuggestion())
    assert suggested["state"]=="OPEN"
    assert WorkingDraftStore(path.parent).detail(path)[1]==saved
    dismiss_review(path,len(suggested["history"]))
    store.retire_completed(path)
    assert WorkingDraftStore(path.parent).detail(path)[1]==saved


def test_inbox_indexes_saved_words_separately_and_retires_discarded_or_recorded_reply(work):
    from returnlight_app.returnlight_web import LocalCases
    from returnlight_app.returnlight import buyer_evidence, record_merchant_reply
    path, case, store = work
    app = LocalCases(path.parent)
    words = "Keep the parcel marked amber until I confirm the collection."
    request = payload(case, text=words)
    request["draft"]["reply"]["record_id"] = "d"*32
    original = path.read_bytes()
    saved = store.save(path, request)
    summary = app.summary(path.stem)
    assert {"kind":"reply", "label":"Saved unsent reply", "text":words} in summary["saved_work"]
    assert any(part["kind"] == "decision" and "12.80" in part["text"] for part in summary["saved_work"])
    assert words not in summary["complaint"] and words not in buyer_evidence(case)
    assert path.read_bytes() == original
    request["draft_version"] = saved["version"]
    request["draft"]["reply"] = None
    discarded = store.save(path, request)
    assert not any(part["kind"] == "reply" for part in app.summary(path.stem)["saved_work"])
    request = payload(case, discarded["version"], words)
    request["draft"]["reply"]["record_id"] = "d"*32
    store.save(path, request)
    record_merchant_reply(path, words, len(case["history"]), True, "d"*32)
    summary = app.summary(path.stem)
    assert not any(part["kind"] == "reply" for part in summary["saved_work"])
    assert words in summary["complaint"]

