import base64
import copy
import io
import json
import subprocess
import pytest

from returnlight_app.returnlight import CaseError, CaseChanged, new_case, case_snapshot, add_buyer_followup
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_document_sources import (
    EXTRACTOR, DocumentPassageStore, extract_pdf_page, validated_document_passages)

def pdf(text="AUTHORED TEST RECEIPT - Blue mug USD 12.50", encrypted=False):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 20 200 Td (" + text + ") Tj ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("fixture-password")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

@pytest.fixture
def prepared(tmp_path):
    app = LocalCases(tmp_path)
    key = "a" * 32
    case = new_case(app.path(key), "DOCPASSAGE01", "30.50", "Please send the original itemized receipt.")
    data = pdf()
    work = app.action(key, "reply-document", {
        "case_revision": len(case["history"]), "draft_version": None,
        "filename": "authored-receipt.pdf", "data_base64": base64.b64encode(data).decode()})["working_draft"]
    return app, key, case, work

def preview(prepared):
    app, key, _, work = prepared
    return app.document_passages.preview(app.path(key), work["draft"]["reply_document"]["id"],
                                        1, app.photos, app.working_drafts)

def keep(app, key, value, start=0, end=None):
    return app.action(key, "document-passage", {
        "preview_id": value["preview_id"], "case_revision": value["case_revision"],
        "start": start, "end": len(value["text"]) if end is None else end})

def test_real_pdf_selection_restart_and_removal_leave_payment_authority(prepared):
    app, key, before, work = prepared
    value = preview(prepared)
    assert value["page_count"] == 1 and "AUTHORED TEST RECEIPT" in value["text"]
    start = value["text"].index("Blue mug")
    after = keep(app, key, value, start)["case"]
    item = validated_document_passages(after)[0]
    assert item["text"] == value["text"][start:]
    assert item["file"] == work["draft"]["reply_document"] and item["page"] == 1
    assert item["extractor"] == EXTRACTOR
    for field in ["capture_id", "state", "approved_cents", "request_id", "refund_id"]:
        assert after.get(field) == before.get(field)
    reopened = LocalCases(app.directory)
    assert reopened.detail(key)["case"]["document_passages"] == [item]
    removed = reopened.action(key, "document-passage", {
        "remove": item["id"], "case_revision": len(after["history"])})["case"]
    assert removed["document_passages"] == []
    with pytest.raises(CaseChanged):
        keep(app, key, value, start)

def test_exact_unicode_codepoint_selection_and_no_client_authored_text(prepared, monkeypatch):
    app, key, _, _ = prepared
    monkeypatch.setattr("returnlight_app.returnlight_document_sources.extract_pdf_page",
                        lambda data, page: {"text": "📦 receipt: bowl £18", "page_count": 1})
    value = preview(prepared)
    result = keep(app, key, value, 11, 19)["case"]
    assert result["document_passages"][0]["text"] == "bowl £18"
    before = app.path(key).read_bytes()
    with pytest.raises(CaseError):
        app.action(key, "document-passage", {"preview_id": value["preview_id"], "case_revision": 2,
                                           "start": 0, "end": 3, "text": "forged"})
    assert app.path(key).read_bytes() == before

@pytest.mark.parametrize("change", ["missing", "damaged", "replaced", "expired", "case"])
def test_changed_original_preview_or_case_cannot_supply_new_words(prepared, change):
    app, key, case, work = prepared
    value = preview(prepared)
    reference = work["draft"]["reply_document"]
    if change == "missing":
        app.photos.path(reference["id"]).unlink()
    elif change == "damaged":
        app.photos.path(reference["id"]).write_bytes(b"x" * reference["bytes"])
    elif change == "replaced":
        app.action(key, "reply-document", {"case_revision": len(case["history"]),
            "draft_version": work["version"], "filename": "another.pdf",
            "data_base64": base64.b64encode(pdf("DIFFERENT WORDS")).decode()})
    elif change == "expired":
        app.document_passages._previews[value["preview_id"]]["stored_at"] -= 601
    else:
        add_buyer_followup(app.path(key), "Please use my corrected receipt address.", len(case["history"]))
    before = app.path(key).read_bytes()
    with pytest.raises(CaseError):
        keep(app, key, value)
    assert app.path(key).read_bytes() == before

def test_cross_case_preview_and_restarted_preview_are_rejected(prepared):
    app, key, _, _ = prepared
    value = preview(prepared)
    other = "b" * 32
    new_case(app.path(other), "OTHERDOC01", "30.50", "Please read my own receipt.")
    with pytest.raises(CaseError):
        keep(app, other, value)
    app.document_passages = DocumentPassageStore()
    with pytest.raises(CaseError):
        keep(app, key, value)

def test_historical_passage_survives_missing_original_without_claiming_availability(prepared):
    app, key, _, work = prepared
    item = keep(app, key, preview(prepared))["case"]["document_passages"][0]
    app.photos.path(item["file"]["id"]).unlink()
    assert validated_document_passages(case_snapshot(app.path(key))) == [item]
    with pytest.raises(CaseError, match="unavailable"):
        preview(prepared)

@pytest.mark.parametrize("field,value", [("text", "invented"), ("start", True),
    ("end", 999999), ("page", 0), ("extractor", "unknown"), ("selected_at", "2026-11-12"),
    ("page_text", "short")])
def test_corrupt_saved_selection_is_not_a_source(prepared, field, value):
    app, key, _, _ = prepared
    case = keep(app, key, preview(prepared))["case"]
    case["document_passages"][0][field] = value
    with pytest.raises(CaseError):
        validated_document_passages(case)

def test_empty_encrypted_missing_page_and_parser_timeout_fall_back():
    with pytest.raises(CaseError, match="no selectable text"):
        extract_pdf_page(pdf(""), 1)
    with pytest.raises(CaseError, match="encrypted"):
        extract_pdf_page(pdf(encrypted=True), 1)
    with pytest.raises(CaseError, match="existing PDF page"):
        extract_pdf_page(pdf(), 2)

def test_timeout_is_bounded_and_releases_slot(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 8
        raise subprocess.TimeoutExpired(args[0], 8)
    monkeypatch.setattr(subprocess, "run", timeout)
    for _ in range(3):
        with pytest.raises(CaseError, match="local limit"):
            extract_pdf_page(pdf(), 1)

def test_real_http_preview_and_keep_require_case_and_session(prepared):
    import threading
    import urllib.request
    import urllib.error
    from returnlight_app.returnlight_web import LocalReturnlightServer
    app, key, _, work = prepared
    server = LocalReturnlightServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        reference = work["draft"]["reply_document"]
        with urllib.request.urlopen(origin + "/api/cases/" + key + "/document-text?file=" + reference["id"] + "&page=1") as response:
            value = json.load(response)
        assert value["ok"] and "Blue mug" in value["text"]
        payload = {"preview_id": value["preview_id"], "case_revision": value["case_revision"],
                   "start": 0, "end": len(value["text"])}
        request = urllib.request.Request(origin + "/api/cases/" + key + "/document-passage",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 403
        request.add_header("X-Returnlight-Token", server.token)
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        assert saved["case"]["document_passages"][0]["text"] == value["text"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_kept_passage_remains_independent_of_outgoing_attachment(prepared):
    app,key,case,work=prepared
    saved=keep(app,key,preview(prepared))["case"]
    reference=work["draft"]["reply_document"]
    app.action(key,"reply-document",{"case_revision":len(saved["history"]),"draft_version":work["version"],"remove":reference["id"]})
    reopened=app.document_passages.preview(app.path(key),reference["id"],1,app.photos,app.working_drafts)
    assert "Blue mug" in reopened["text"]
    assert app.detail(key)["working_draft"]["draft"].get("reply_document") is None

@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_source_original_uses_existing_readable_fallback(prepared, damage):
    import threading
    import urllib.request
    import urllib.error
    from returnlight_app.returnlight_web import LocalReturnlightServer
    app, key, _, work = prepared
    reference = work["draft"]["reply_document"]
    original = app.photos.read(reference)
    saved = keep(app, key, preview(prepared))["case"]
    app.action(key, "reply-document", {"case_revision": len(saved["history"]),
        "draft_version": work["version"], "remove": reference["id"]})
    server = LocalReturnlightServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    url = origin + "/api/cases/" + key + "/reply-document?source=document&id=" + reference["id"] + "&sha=" + reference["sha256"]
    before = app.path(key).read_bytes()
    try:
        with urllib.request.urlopen(url) as response:
            assert response.headers["Content-Type"] == "application/pdf"
            assert response.read() == original
        other = "b" * 32
        new_case(app.path(other), "OTHERDOCPDF01", "30.50", "Please read my original receipt.")
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url.replace("/" + key + "/", "/" + other + "/"))
        assert denied.value.code == 422
        assert "This document is unavailable" in denied.value.read().decode()
        if damage == "missing":
            app.photos.path(reference["id"]).unlink()
        else:
            app.photos.path(reference["id"]).write_bytes(b"x" * reference["bytes"])
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(url)
        assert missing.value.code == 422
        assert missing.value.headers["Content-Type"].startswith("text/html")
        page = missing.value.read().decode()
        assert "This document is unavailable" in page and "/#case=" + key in page
        assert app.path(key).read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)

def test_reading_upload_keeps_outgoing_original_and_survives_reopen(prepared):
    app,key,before,work=prepared
    outgoing=work["draft"]["reply_document"]
    working_path=app.working_drafts._path(app.path(key))
    working_bytes=working_path.read_bytes()
    data=pdf("SECOND SOURCE - Green bowl USD 18.00")
    result=app.action(key,"document-source",{"case_revision":len(before["history"]),
        "filename":"second-source.pdf","data_base64":base64.b64encode(data).decode()})
    source=result["document_source"]
    assert source != outgoing and app.photos.read(source)==data
    assert working_path.read_bytes()==working_bytes
    reopened=LocalCases(app.directory)
    assert reopened.detail(key)["working_draft"]["draft"]["reply_document"]==outgoing
    value=reopened.document_passages.preview(reopened.path(key),source["id"],1,reopened.photos,reopened.working_drafts)
    assert "SECOND SOURCE" in value["text"]
    selected=keep(reopened,key,value)["case"]
    assert selected["document_passages"][0]["file"]==source
    assert working_path.read_bytes()==working_bytes
    assert reopened.read_document_source(key,source["id"],source["sha256"])[1]==data
    assert reopened.read_reply_document(key,outgoing["id"],outgoing["sha256"])[1]==pdf()
    for field in ["capture_id","state","approved_cents","request_id","refund_id"]:
        assert selected.get(field)==before.get(field)
    with pytest.raises(CaseError,match="selected words"):
        reopened.action(key,"document-source",{"case_revision":len(selected["history"]),"remove":source["id"]})
    removed=reopened.action(key,"document-passage",{"case_revision":len(selected["history"]),
        "remove":selected["document_passages"][0]["id"]})["case"]
    removed=reopened.action(key,"document-source",{"case_revision":len(removed["history"]),"remove":source["id"]})["case"]
    assert removed["document_sources"]==[]
    assert working_path.read_bytes()==working_bytes

def test_source_upload_without_reply_draft_and_stale_or_corrupt_source(prepared):
    app,key,before,work=prepared
    request={"case_revision":len(before["history"]),"filename":"source.pdf",
        "data_base64":base64.b64encode(pdf("Separate source")).decode()}
    result=app.action(key,"document-source",request)
    with pytest.raises(CaseChanged):
        app.action(key,"document-source",request)
    c=result["case"]
    current={"case_revision":len(c["history"]),"filename":"source.pdf","data_base64":request["data_base64"]}
    duplicate=app.action(key,"document-source",current)
    assert duplicate["case"]==c
    corrupt=copy.deepcopy(c);corrupt["document_sources"][0]["mime"]="image/png"
    with pytest.raises(CaseError):
        validated_document_passages(corrupt)
    other="b"*32;other_case=new_case(app.path(other),"SOURCEONLY01","30.50","Please read this receipt.")
    result=app.action(other,"document-source",{**request,"case_revision":len(other_case["history"])})
    assert result["case"]["document_sources"] and app.detail(other)["working_draft"]["draft"].get("reply_document") is None
    with pytest.raises(CaseError):
        app.action(other,"document-source",{"case_revision":len(result["case"]["history"]),
            "filename":"x.pdf","data_base64":base64.b64encode(b"not a pdf").decode()})

def test_http_reading_upload_requires_session_and_keeps_outgoing_bytes(prepared):
    import threading
    import urllib.request
    import urllib.error
    from returnlight_app.returnlight_web import LocalReturnlightServer
    app,key,before,work=prepared
    outgoing=work["draft"]["reply_document"]
    original=app.photos.read(outgoing)
    body=pdf("READING ORIGINAL") + b"\n" * (400 * 1024)
    server=LocalReturnlightServer(("127.0.0.1",0),app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin="http://127.0.0.1:"+str(server.server_port)
    payload={"case_revision":len(before["history"]),"filename":"reading-source.pdf",
        "data_base64":base64.b64encode(body).decode()}
    try:
        request=urllib.request.Request(origin+"/api/cases/"+key+"/document-source",
            data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
        # The server refuses missing session authority before consuming a large
        # body. Read that early response without racing sendall against its close.
        import http.client
        denied = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            denied.putrequest("POST", "/api/cases/"+key+"/document-source")
            denied.putheader("Content-Type", "application/json")
            denied.putheader("Content-Length", str(len(request.data)))
            denied.endheaders()
            refusal = denied.getresponse()
            assert refusal.status == 403
            refusal.read()
        finally:
            denied.close()
        assert app.detail(key)["case"]==before
        request.add_header("X-Returnlight-Token",server.token)
        with urllib.request.urlopen(request) as response:
            result=json.load(response)
        source=result["document_source"]
        assert result["working_draft"]["draft"]["reply_document"]==outgoing
        assert app.read_reply_document(key,outgoing["id"],outgoing["sha256"])[1]==original
        with urllib.request.urlopen(origin+"/api/cases/"+key+"/reply-document?source=document&id="+source["id"]+"&sha="+source["sha256"]) as response:
            assert response.read()==body
    finally:
        server.shutdown();server.server_close();thread.join(3)
