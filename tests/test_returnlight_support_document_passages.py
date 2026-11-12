"""Selected document words remain distinct from authenticated facts and buyer questions."""
import copy
import hashlib
import pytest
from test_returnlight_support_review import fixture, answer
from returnlight_app.returnlight import CaseError
from returnlight_app.returnlight_document_sources import EXTRACTOR
from returnlight_app.returnlight_support_review import (
    support_sources, support_content, support_basis, current_support_review,
    document_passage_sources, validate_support_review)

def with_passage(tmp_path):
    _, case = fixture(tmp_path)
    page = "LOCAL UNSELECTED CONTEXT\nAUTHORED TEST RECEIPT\nMarrow Ceramics\n10 November 2026\nLOCAL UNSELECTED FOOTER"
    text = "Marrow Ceramics\n10 November 2026"
    start = page.index(text)
    case["document_passages"] = [{
        "id": "a"*32, "file": {"id":"b"*32,"name":"authored-receipt.pdf",
            "mime":"application/pdf","bytes":17290,"sha256":"c"*64,"width":0,"height":0},
        "page":1,"page_text":page,"start":start,"end":start+len(text),"text":text,
        "selected_at":"2026-11-12T02:00:00+00:00","extractor":EXTRACTOR,
    }]
    return case

def test_only_selected_words_leave_the_local_page_with_exact_source_identity(tmp_path):
    case = with_passage(tmp_path)
    without = copy.deepcopy(case);del without["document_passages"]
    old = support_sources(without)
    bank = support_sources(case)
    assert bank[:len(old)] == old
    source = document_passage_sources(case)[0]
    passage = case["document_passages"][0]
    assert source["text"] == passage["page_text"][passage["start"]:passage["end"]]
    assert source["passage_id"] == passage["id"]
    assert source["file_id"] == passage["file"]["id"]
    assert source["file_sha256"] == passage["file"]["sha256"]
    assert source["page_text_sha256"] == hashlib.sha256(passage["page_text"].encode()).hexdigest()
    assert "page_text" not in source
    content = support_content(case)
    assert passage["text"] in content
    assert "LOCAL UNSELECTED CONTEXT" not in content and "LOCAL UNSELECTED FOOTER" not in content
    assert "not thereby an authenticated receipt" in content
    assert "page 1" in source["label"]

def test_selection_removal_or_identity_change_retires_the_old_reading(tmp_path):
    case = with_passage(tmp_path)
    basis = support_basis(case)
    case["support_review"] = {"source_basis":basis}
    assert current_support_review(case)
    for field,value in [("id","d"*32),("page",2),("selected_at","2026-11-12T03:00:00+00:00"),
                        ("page_text",case["document_passages"][0]["page_text"]+" additional local context")]:
        changed=copy.deepcopy(case);changed["document_passages"][0][field]=value
        assert support_basis(changed)!=basis
        assert current_support_review(changed) is None
    changed=copy.deepcopy(case);changed["document_passages"][0]["file"]["sha256"]="e"*64
    assert current_support_review(changed) is None
    selected=copy.deepcopy(case);selected["document_passages"][0]["text"]="Marrow Ceramics"
    selected["document_passages"][0]["end"]=selected["document_passages"][0]["start"]+len("Marrow Ceramics")
    assert current_support_review(selected) is None
    removed=copy.deepcopy(case);removed["document_passages"]=[]
    assert current_support_review(removed) is None

def test_document_quotes_are_exact_and_cannot_be_unanswered_buyer_questions(tmp_path):
    case=with_passage(tmp_path);value=answer(case)
    bank=support_sources(case);number=next(i for i,s in enumerate(bank,1) if s["kind"]=="document")
    value["source_quotes"].append({"source_number":number,"quote":"Marrow Ceramics"})
    reading=validate_support_review(value,case)
    assert reading["sources"][-1]["passage_id"]==case["document_passages"][0]["id"]
    value["open_questions"]=[len(value["source_quotes"])]
    with pytest.raises(CaseError,match="unanswered question"):validate_support_review(value,case)
    value["open_questions"]=[]
    value["source_quotes"][-1]["quote"]="This receipt is authentic."
    with pytest.raises(CaseError,match="exact"):validate_support_review(value,case)

def test_invalid_local_selection_is_rejected_before_source_projection(tmp_path):
    case=with_passage(tmp_path)
    case["document_passages"][0]["text"]="Text not selected from the page."
    with pytest.raises(CaseError,match="quotation"):support_content(case)

def test_selected_document_requires_new_explicit_scope_before_dispatch(tmp_path):
    import json
    from test_returnlight_support_review import Reviewer
    from returnlight_app.returnlight import case_snapshot
    from returnlight_app.returnlight_web import LocalCases
    case=with_passage(tmp_path);path=tmp_path/("b"*32+".json")
    path.write_text(json.dumps(case))
    class Remote(Reviewer):
        destination_id="controlled-document-destination"
        calls=0
        def review_support_case(self,case,*,before_request):
            self.calls+=1
            before_request()
            assert "LOCAL UNSELECTED CONTEXT" not in support_content(case)
            return validate_support_review(answer(case),case)
    reader=Remote();app=LocalCases(tmp_path,remote_reviewer=reader)
    payload={"remote_confirmed":True,"destination_id":reader.destination_id,"case_revision":len(case["history"])}
    before=path.read_bytes()
    for scope in ["current-support-v1","current-support-purchase-v2",
                  "current-support-refund-check-v3","current-support-recorded-purchase-v4","current-support-work-v5"]:
        with pytest.raises(CaseError,match="selected document passages"):
            app.action(path.stem,"support-review",{**payload,"review_scope":scope})
        assert reader.calls==0 and path.read_bytes()==before
    received=app.action(path.stem,"support-review",{**payload,"review_scope":"current-support-document-passages-v6"})["case"]
    assert reader.calls==1 and current_support_review(received)
    assert case_snapshot(path)["document_passages"]==case["document_passages"]
