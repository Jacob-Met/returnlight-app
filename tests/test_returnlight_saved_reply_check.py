import base64
import json
import pytest
from returnlight_app.returnlight import CaseChanged, new_case, add_buyer_followup
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_working_drafts import WorkingDraftStore

@pytest.fixture
def saved(tmp_path):
    key="c"*32
    case=new_case(tmp_path/(key+".json"),"SAVEDCHECKCAP01","5.40","My refund is still missing. Please check the original refund.")
    app=LocalCases(tmp_path)
    reply={"capture":case["capture_id"],"text":"Our records show your USD5.40 refund is still pending.",
           "version":json.dumps([case["capture_id"],case["state"],case["approved_cents"],case.get("refund_id"),len(case["history"])])}
    request={"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":reply}}
    work=app.action(key,"working-draft",request)["working_draft"]
    return app,key,case,request,work

def test_review_marker_survives_fresh_store_without_changing_words_or_case(saved):
    app,key,case,request,work=saved;before=app.path(key).read_bytes()
    pdf=b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
    work=app.action(key,"reply-document",{"case_revision":len(case["history"]),"draft_version":work["version"],"filename":"original.pdf","data_base64":base64.b64encode(pdf).decode()})["working_draft"]
    checked=app.working_drafts.mark_reply_for_review(app.path(key),len(case["history"]))
    assert checked["draft"]["reply"]=={**work["draft"]["reply"],"review_required":True}
    assert checked["draft"]["reply_document"]==work["draft"]["reply_document"]
    assert checked["version"]!=work["version"]
    assert WorkingDraftStore(app.directory).detail(app.path(key))[1]==checked
    assert app.working_drafts.mark_reply_for_review(app.path(key),len(case["history"]))==checked
    assert app.path(key).read_bytes()==before
    request["draft_version"]=work["version"]
    with pytest.raises(CaseChanged):app.action(key,"working-draft",request)

def test_changed_case_cannot_mark_an_unrelated_saved_basis(saved):
    app,key,case,request,work=saved
    add_buyer_followup(app.path(key),"I received the refund; please send the original receipt instead.",len(case["history"]))
    with pytest.raises(CaseChanged):app.working_drafts.mark_reply_for_review(app.path(key),len(case["history"]))
    assert app.detail(key)["working_draft"]==work

def test_no_saved_reply_creates_no_new_work(tmp_path):
    path=tmp_path/("a"*32+".json");case=new_case(path,"NOSAVEDCHECK01","5.40","Please check the original refund status.")
    store=WorkingDraftStore(tmp_path)
    assert store.mark_reply_for_review(path,len(case["history"]))=={"version":None,"draft":{"decision":None,"reply":None}}
    assert not store.directory.exists()
