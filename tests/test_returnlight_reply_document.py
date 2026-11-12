import base64
import copy
import json
import pytest
from returnlight_app.returnlight import CaseError, CaseChanged, new_case, add_buyer_followup
from returnlight_app.returnlight_web import LocalCases

PDF=b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"

@pytest.fixture
def app(tmp_path):
    key="a"*32
    case=new_case(tmp_path/(key+".json"),"RECEIPTCAPTURE01","30.50","Please send the original itemized receipt for my mug and bowl.")
    return LocalCases(tmp_path),key,case

def request(case,version=None,data=PDF,name="shop-receipt.pdf"):
    return {"case_revision":len(case["history"]),"draft_version":version,"filename":name,"data_base64":base64.b64encode(data).decode()}

def test_original_survives_restart_exact_bytes_and_case_unchanged(app):
    a,key,c=app;before=a.path(key).read_bytes()
    result=a.action(key,"reply-document",request(c));work=result["working_draft"];ref=work["draft"]["reply_document"]
    assert ref["name"]=="shop-receipt.pdf" and ref["mime"]=="application/pdf"
    restarted=LocalCases(a.directory)
    assert restarted.detail(key)["working_draft"]==work
    assert restarted.read_reply_document(key,ref["id"],ref["sha256"])==(ref,PDF)
    assert a.action(key,"reply-document",request(c))==result
    assert a.path(key).read_bytes()==before

def test_ordinary_save_preserves_server_reference_and_refuses_injection(app):
    a,key,c=app
    forged={"id":"e"*32,"name":"other.pdf","mime":"application/pdf","bytes":len(PDF),"sha256":"f"*64,"width":0,"height":0}
    plain={"case_revision":len(c["history"]),"draft_version":None,"draft":{"decision":None,"reply":None,"reply_document":forged}}
    with pytest.raises(CaseError):a.action(key,"working-draft",plain)
    work=a.action(key,"reply-document",request(c))["working_draft"]
    plain["draft_version"]=work["version"];plain["draft"].pop("reply_document")
    assert a.action(key,"working-draft",plain)["working_draft"]==work
    plain["draft"]["reply_document"]=None
    with pytest.raises(CaseError):a.action(key,"working-draft",plain)

def test_cross_case_changed_digest_and_corrupt_bytes_fail(app):
    a,key,c=app;ref=a.action(key,"reply-document",request(c))["working_draft"]["draft"]["reply_document"]
    other="b"*32;new_case(a.path(other),"OTHERCAPTURE01","30.50","Another buyer asks for their receipt.")
    with pytest.raises(CaseChanged):a.read_reply_document(other,ref["id"],ref["sha256"])
    with pytest.raises(CaseChanged):a.read_reply_document(key,ref["id"],"f"*64)
    a.photos.path(ref["id"]).write_bytes(PDF.replace(b"Catalog",b"Changed"))
    with pytest.raises(CaseError,match="unavailable or changed"):a.read_reply_document(key,ref["id"],ref["sha256"])

def test_replace_remove_concurrent_revision_and_old_preview(app):
    a,key,c=app;first=a.action(key,"reply-document",request(c))["working_draft"];old=first["draft"]["reply_document"]
    with pytest.raises(CaseChanged):a.action(key,"reply-document",request(c,None,name="new.pdf"))
    second=a.action(key,"reply-document",request(c,first["version"],name="new.pdf"))["working_draft"]
    with pytest.raises(CaseChanged):a.read_reply_document(key,old["id"],old["sha256"])
    ref=second["draft"]["reply_document"]
    removed=a.action(key,"reply-document",{"case_revision":len(c["history"]),"draft_version":second["version"],"remove":ref["id"]})["working_draft"]
    assert removed["draft"]["reply_document"] is None
    with pytest.raises(CaseChanged):a.read_reply_document(key,ref["id"],ref["sha256"])
    add_buyer_followup(a.path(key),"Please use my corrected buyer details for this receipt.",len(c["history"]))
    with pytest.raises(CaseChanged):a.action(key,"reply-document",request(c,removed["version"]))

@pytest.mark.parametrize("update",[
 {"data_base64":"!!"}, {"data_base64":base64.b64encode(b"not a supported document").decode()},
 {"filename":"bad\nname.pdf"},{"filename":""},{"draft_version":"bad"},
 {"case_revision":True},{"extra":"untrusted"}])
def test_invalid_upload_leaves_case_and_saved_work_unchanged(app,update):
    a,key,c=app;before=a.path(key).read_bytes();p=request(c);p.update(update)
    with pytest.raises(CaseError):a.action(key,"reply-document",p)
    assert a.path(key).read_bytes()==before and a.detail(key)["working_draft"]["version"] is None

def test_client_path_is_reduced_to_original_basename(app):
    a,key,c=app
    ref=a.action(key,"reply-document",request(c,name="C:\\private\\receipt.pdf"))["working_draft"]["draft"]["reply_document"]
    assert ref["name"]=="receipt.pdf"

def test_upload_preview_route_requires_local_session_and_case_bound_reference(app):
    import threading
    import urllib.request
    import urllib.error
    from returnlight_app.returnlight_web import LocalReturnlightServer
    a,key,c=app
    server=LocalReturnlightServer(("127.0.0.1",0),a)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin="http://127.0.0.1:"+str(server.server_port)
    before=a.path(key).read_bytes()
    # Exercise the larger document envelope without changing ordinary case limits.
    document=PDF[:-6]+b"\n% original fixture padding "+b"x"*100000+b"\n%%EOF\n"
    body=json.dumps(request(c,data=document)).encode()
    try:
        req=urllib.request.Request(origin+"/api/cases/"+key+"/reply-document",data=body,headers={"Content-Type":"application/json"})
        with pytest.raises(urllib.error.HTTPError) as denied:urllib.request.urlopen(req)
        assert denied.value.code==403
        req.add_header("X-Returnlight-Token",server.token)
        with urllib.request.urlopen(req) as response:result=json.load(response)
        ref=result["working_draft"]["draft"]["reply_document"]
        url=origin+"/api/cases/"+key+"/reply-document?id="+ref["id"]+"&sha="+ref["sha256"]
        with urllib.request.urlopen(url) as response:
            assert response.headers["Content-Type"]=="application/pdf"
            assert response.read()==document
        other="b"*32;new_case(a.path(other),"OTHERCAPTURE01","30.50","Please locate my own original receipt.")
        with pytest.raises(urllib.error.HTTPError) as denied:urllib.request.urlopen(url.replace("/"+key+"/","/"+other+"/"))
        assert denied.value.code==422
        assert a.path(key).read_bytes()==before
    finally:server.shutdown();server.server_close();thread.join(timeout=5)

def test_original_upload_preserves_reply_words_and_support_input(app):
    from returnlight_app.returnlight_support_review import support_content
    a,key,c=app;source=support_content(c)
    reply={"capture":c["capture_id"],"text":"I found the original receipt and will check it before attaching.",
           "version":json.dumps([c["capture_id"],c["state"],c["approved_cents"],c.get("refund_id"),len(c["history"])])}
    first=a.action(key,"working-draft",{"case_revision":len(c["history"]),"draft_version":None,"draft":{"decision":None,"reply":reply}})["working_draft"]
    saved=a.action(key,"reply-document",request(c,first["version"]))["working_draft"]
    assert saved["draft"]["reply"]==reply
    assert support_content(a.detail(key)["case"])==source

def test_original_can_be_restored_after_local_file_damage(app):
    a,key,c=app
    first=a.action(key,"reply-document",request(c))["working_draft"]
    old=first["draft"]["reply_document"];a.photos.path(old["id"]).unlink()
    with pytest.raises(CaseError):a.read_reply_document(key,old["id"],old["sha256"])
    restored=a.action(key,"reply-document",request(c,first["version"]))["working_draft"]
    ref=restored["draft"]["reply_document"]
    assert ref["id"]!=old["id"] and a.read_reply_document(key,ref["id"],ref["sha256"])[1]==PDF

def test_upload_does_not_return_an_already_recorded_reply(app):
    from returnlight_app.returnlight import record_merchant_reply
    a,key,c=app
    words="I sent the original receipt after checking both item lines."
    reply={"capture":c["capture_id"],"text":words,"version":json.dumps([c["capture_id"],c["state"],c["approved_cents"],c.get("refund_id"),len(c["history"])]),"record_id":"f"*32}
    first=a.action(key,"working-draft",{"case_revision":len(c["history"]),"draft_version":None,"draft":{"decision":None,"reply":reply}})["working_draft"]
    updated=record_merchant_reply(a.path(key),words,len(c["history"]),True,"f"*32)
    assert a.detail(key)["working_draft"]["draft"]["reply"] is None
    saved=a.action(key,"reply-document",request(updated,first["version"]))["working_draft"]
    assert saved["draft"]["reply"] is None
