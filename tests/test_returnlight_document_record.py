"""Private selected-document records preserve exact source and original-byte identity."""
import base64
import copy
import hashlib
import re

import pytest

from test_returnlight_support_review import fixture, answer
from returnlight_app.returnlight_document_sources import EXTRACTOR
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_support_review import support_sources, validate_support_review

PDF = b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
WORDS = "Marrow Ceramics · 📦 item details"

def document_case(tmp_path):
    _, case = fixture(tmp_path)
    page_text = "LOCAL UNSELECTED HEADER\n" + WORDS + "\nLOCAL UNSELECTED FOOTER"
    start = page_text.index(WORDS)
    reference = {"id": "b"*32, "name": "authored-receipt.pdf", "mime": "application/pdf",
                 "bytes": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest(), "width": 0, "height": 0}
    passage = {"id": "a"*32, "file": reference, "page": 1, "page_text": page_text,
               "start": start, "end": start + len(WORDS), "text": WORDS,
               "selected_at": "2026-11-12T02:00:00+00:00", "extractor": EXTRACTOR}
    case["document_passages"] = [passage]
    return case

def cite_last_document(case):
    value = answer(case)
    bank = support_sources(case)
    number = max(i for i, source in enumerate(bank, 1) if source["kind"] == "document")
    value["source_quotes"].append({"source_number": number, "quote": WORDS})
    case["support_review"] = validate_support_review(value, case)
    return case["support_review"]["sources"][-1]

def test_selected_words_remain_without_a_reading_and_original_bytes_are_exact(tmp_path):
    case = document_case(tmp_path)
    html = render_case_record(case, photo_loader=lambda ref: PDF)
    assert "Selected document passages" in html and WORDS in html
    assert "authored-receipt.pdf · Page 1" in html
    assert "Selected 12 Nov 2026, 02:00:00 UTC" in html
    assert "LOCAL UNSELECTED HEADER" not in html and "LOCAL UNSELECTED FOOTER" not in html
    encoded = re.findall(r'href="data:application/pdf;base64,([^"]+)"', html)
    assert len(encoded) == 1 and base64.b64decode(encoded[0]) == PDF

def test_identical_selected_words_link_only_the_cited_passage_and_embed_one_original(tmp_path):
    case = document_case(tmp_path)
    second = copy.deepcopy(case["document_passages"][0]);second["id"] = "d"*32
    case["document_passages"].append(second)
    cite_last_document(case)
    html = render_case_record(case, photo_loader=lambda ref: PDF)
    passages = dict(re.findall(r'<p class="verbatim" data-document-passage="([^"]+)"[^>]*>(.*?)</p>', html, re.S))
    assert 'id="ai-source-' not in passages["a"*32]
    assert 'id="ai-source-' in passages["d"*32] and "<mark" in passages["d"*32]
    assert html.count('href="data:application/pdf;base64,') == 1
    target = re.search(r'id="(ai-source-\d+)"', passages["d"*32]).group(1)
    assert 'href="#' + target + '"' in html

@pytest.mark.parametrize("loader", [None, lambda ref: b"changed original"])
def test_unavailable_or_changed_original_keeps_selected_text_but_no_false_download(tmp_path, loader):
    case = document_case(tmp_path)
    cite_last_document(case)
    html = render_case_record(case, photo_loader=loader)
    assert WORDS in html and "original document unavailable in this copy" in html
    assert "data:application/pdf;base64," not in html
    assert 'data-document-passage="' + "a"*32 + '"' in html

@pytest.mark.parametrize("field,value", [("passage_id","e"*32),("file_sha256","f"*64),("page",2),("page",True),("quote_start",1)])
def test_wrong_document_identity_or_range_cannot_link_another_passage(tmp_path, field, value):
    case = document_case(tmp_path)
    source = cite_last_document(case)
    source[field] = value
    html = render_case_record(case, photo_loader=lambda ref: PDF)
    selected = re.search(r'<p class="verbatim" data-document-passage="[^"]+"[^>]*>(.*?)</p>', html, re.S).group(1)
    assert 'id="ai-source-' not in selected

def test_removed_selection_does_not_leave_a_current_document_citation(tmp_path):
    case = document_case(tmp_path)
    cite_last_document(case)
    case["document_passages"] = []
    html = render_case_record(case, photo_loader=lambda ref: PDF)
    assert "Selected document passages" not in html
    assert "AI reading · current support" not in html
