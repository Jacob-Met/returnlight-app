import base64
import copy
import hashlib
import re
import pytest
from returnlight_app.returnlight import new_case, record_merchant_reply, case_snapshot
from returnlight_app.returnlight_photos import PhotoStore
from returnlight_app.returnlight_record import render_case_record,render_purchase_details,checked_reply_documents

PDF = b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6cJAAAAAASUVORK5CYII=")

@pytest.fixture
def recorded(tmp_path):
    path=tmp_path/("a"*32+".json")
    case=new_case(path,"RECEIPTCAPTURE01","30.50","Please send the itemized receipt.")
    return path,case,PhotoStore(tmp_path)

def _record(fixture,documents=()):
    path,case,photos=fixture
    record_merchant_reply(path,"I have attached the checked buyer documents.",len(case["history"]),True,"b"*32,documents=list(documents))
    return case_snapshot(path)

@pytest.mark.parametrize("data,name",[(PDF,"original.pdf"),(PNG,"original.png")])
def test_recorded_original_export_is_exact_after_working_replacement(recorded,data,name):
    path,case,photos=recorded;original=photos.put(data,name)
    case=_record(recorded,[{"kind":"original","file":original}])
    replacement=photos.put(PDF+b"\n","replacement.pdf")
    before=copy.deepcopy(case)
    work={"draft":{"decision":None,"reply":None,"reply_document":replacement}}
    html=render_case_record(case,photo_loader=PhotoStore(photos.directory.parent).read,working_draft=work)
    assert case==before
    block=re.search(r'<article class="message merchant-reply">.*?</article>',html,re.S).group()
    assert "I have attached the checked buyer documents." in block
    assert "Recorded as sent outside Returnlight" in block
    link=re.search(r'<a download="[^"]+" href="data:[^;]+;base64,([^"]+)">',block)
    assert base64.b64decode(link.group(1))==data
    assert "replacement.pdf" not in block

@pytest.mark.parametrize("damage",["missing","changed","no_loader","wrong_loader"])
def test_unavailable_original_keeps_words_without_substitution(recorded,damage):
    path,case,photos=recorded;reference=photos.put(PDF,"receipt.pdf")
    case=_record(recorded,[{"kind":"original","file":reference}])
    loader=photos.read
    if damage=="missing":photos.path(reference["id"]).unlink()
    elif damage=="changed":photos.path(reference["id"]).write_bytes(PDF.replace(b"Catalog",b"Changed"))
    elif damage=="no_loader":loader=None
    else:loader=lambda reference:PDF.replace(b"Catalog",b"Changed")
    html=render_case_record(case,photo_loader=loader)
    assert "I have attached the checked buyer documents." in html
    assert "receipt.pdf — recorded document unavailable in this copy" in html
    assert '<a download="receipt.pdf"' not in html

def test_recorded_document_name_is_escaped(recorded):
    path,case,photos=recorded
    case=_record(recorded,[{"kind":"original","file":photos.put(PDF,'receipt"><img src=x>.pdf')}])
    html=render_case_record(case,photo_loader=photos.read)
    assert 'receipt&quot;&gt;&lt;img src=x&gt;.pdf' in html and '<img src=x>' not in html

def _purchase(case):
    case["purchase_context"]={"capture_id":case["capture_id"],"observed_at":"2026-11-11T19:00:00+00:00",
      "original_cents":3050,"order_id":"AUTHOREDITEMORDER",
      "order":{"id":"AUTHOREDITEMORDER","total_cents":3050,"items":[
        {"name":"Blue ceramic mug","quantity":1,"unit_cents":1250,"line_cents":1250},
        {"name":"Green stoneware bowl","quantity":1,"unit_cents":1800,"line_cents":1800}]}}

def test_recorded_summary_uses_frozen_document_not_current_items(recorded):
    path,case,photos=recorded;_purchase(case)
    document=render_purchase_details(case).encode()
    documents=checked_reply_documents(case,{"include_purchase_details":True,"purchase_details_sha256":hashlib.sha256(document).hexdigest()})
    saved=_record(recorded,[value for value,data in documents])
    _purchase(saved);saved["purchase_context"]["order"]["items"][0].update(name="Corrected later purchase",unit_cents=2500,line_cents=2500)
    saved["purchase_context"]["order"]["total_cents"]=4300;saved["purchase_context"]["original_cents"]=4300
    html=render_case_record(saved,photo_loader=photos.read)
    block=re.search(r'<article class="message merchant-reply">.*?</article>',html,re.S).group()
    link=re.search(r'<a download="purchase-details.html" href="data:text/html;base64,([^"]+)">',block)
    assert base64.b64decode(link.group(1))==document
    assert "Saved purchase summary" in block

def test_changed_summary_does_not_export_current_purchase(recorded):
    path,case,photos=recorded;_purchase(case);data=render_purchase_details(case).encode()
    docs=checked_reply_documents(case,{"include_purchase_details":True,"purchase_details_sha256":hashlib.sha256(data).hexdigest()})
    saved=_record(recorded,[value for value,data in docs])
    saved["merchant_replies"][0]["documents"][0]["snapshot"]["sha256"]="f"*64
    html=render_case_record(saved,photo_loader=photos.read)
    assert "Saved purchase summary — recorded document unavailable in this copy" in html
    assert '<a download="purchase-details.html"' not in html

def test_missing_original_does_not_hide_the_recorded_summary(recorded):
    path,case,photos=recorded;_purchase(case);data=render_purchase_details(case).encode()
    docs=checked_reply_documents(case,{"include_purchase_details":True,"purchase_details_sha256":hashlib.sha256(data).hexdigest()})
    original=photos.put(PDF,"original.pdf")
    saved=_record(recorded,[{"kind":"original","file":original},*[value for value,data in docs]])
    photos.path(original["id"]).unlink()
    html=render_case_record(saved,photo_loader=photos.read)
    assert "original.pdf — recorded document unavailable" in html
    assert "Saved purchase summary</a>" in html and base64.b64encode(data).decode() in html
