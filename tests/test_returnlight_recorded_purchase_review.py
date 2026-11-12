"""Historical generated summaries remain distinct from current purchase facts."""
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from returnlight_app.returnlight import (
    CaseError, add_post_payment_message, case_snapshot, new_case,
    record_merchant_reply, review_support_case)
from returnlight_app.returnlight_record import freeze_purchase_document, render_case_record, render_purchase_details
from returnlight_app.returnlight_support_review import (
    current_support_review, recorded_purchase_sources, support_content,
    support_sources, validate_support_review)
from returnlight_app.returnlight_web import LocalCases

QUESTION = "What names and prices were on the earlier summary, rather than the current purchase details?"

def historical_case(tmp_path):
    path = tmp_path / ("b" * 32 + ".json")
    c = new_case(path, "RECORDEDCAPTURE01", "30.50", "My mug had a problem.")
    c.update(state="COMPLETED", approved_cents=750, request_id="authored-request", refund_id="AUTHOREDREFUND")
    c["purchase_context"] = {
        "capture_id": c["capture_id"], "observed_at": "2026-11-11T19:00:00+00:00",
        "original_cents": 3050, "order_id": "RECORDEDORDER01",
        "order": {"id": "RECORDEDORDER01", "total_cents": 3050, "items": [
            {"name": "Earlier blue mug", "quantity": 1, "unit_cents": 1250, "line_cents": 1250},
            {"name": "Green bowl", "quantity": 1, "unit_cents": 1800, "line_cents": 1800}]}}
    path.write_text(json.dumps(c))
    document = render_purchase_details(c).encode("utf-8")
    snapshot = freeze_purchase_document(c, hashlib.sha256(document).hexdigest())
    original = {"id": "e"*32, "name": "private-original.pdf", "mime": "application/pdf",
                "bytes": 123, "sha256": "f"*64, "width": 0, "height": 0}
    c = record_merchant_reply(path, "I included the saved summary and original document.",
                             len(c["history"]), True, "a"*32,
                             documents=[{"kind": "original", "file": original},
                                        {"kind": "purchase_summary", "snapshot": snapshot}])
    c["purchase_context"]["order"]["items"][0]["name"] = "Current corrected mug"
    path.write_text(json.dumps(c))
    c = add_post_payment_message(path, QUESTION, len(c["history"]))
    return path, c

def response(c):
    bank = support_sources(c)
    latest = next(i for i, s in enumerate(bank, 1) if s["text"] == QUESTION)
    earlier = next(i for i, s in enumerate(bank, 1)
                   if s["kind"] == "recorded_purchase" and s["purchase_number"] == 2)
    return {"current_request": QUESTION,
            "source_quotes": [{"source_number": latest, "quote": QUESTION},
                              {"source_number": earlier, "quote": bank[earlier-1]["text"]}],
            "open_questions": [1],
            "reply_draft": {"text": "The earlier saved summary lists Earlier blue mug at USD 12.50.",
                            "source_numbers": [2]}}

class Reviewer:
    destination_id = "selected-destination"
    def __init__(self): self.calls = 0
    def review_support_case(self, c, *, before_request):
        before_request()
        self.calls += 1
        return validate_support_review(response(c), c)

def test_historical_sources_keep_exact_snapshot_and_document_position(tmp_path):
    _, c = historical_case(tmp_path)
    historical = recorded_purchase_sources(c)
    assert len(historical) == 3
    assert "Earlier blue mug" in historical[1]["text"]
    assert all("Current corrected mug" not in s["text"] for s in historical)
    assert {s["document_number"] for s in historical} == {2}
    assert {s["reply_id"] for s in historical} == {"a"*32}
    assert {s["document_sha256"] for s in historical} == {
        c["merchant_replies"][0]["documents"][1]["snapshot"]["sha256"]}
    changed = copy.deepcopy(c)
    changed["purchase_context"]["order"]["items"][0]["name"] = "Another current correction"
    assert recorded_purchase_sources(changed) == historical

def test_historical_passages_follow_their_reply_and_do_not_expose_original_file(tmp_path):
    _, c = historical_case(tmp_path)
    bank = support_sources(c)
    position = next(i for i, s in enumerate(bank) if s["kind"] == "merchant")
    assert bank[position+1:position+4] == recorded_purchase_sources(c)
    assert bank[position+4]["text"] == QUESTION
    assert any(s["kind"] == "purchase" and "Current corrected mug" in s["text"] for s in bank)
    content = support_content(c)
    assert "Earlier blue mug" in content and "Current corrected mug" in content
    for private in ("private-original.pdf", "e"*32, "f"*64, c["capture_id"], c["request_id"]):
        assert private not in content

def test_historical_snapshot_mismatch_is_not_replaced_with_current_facts(tmp_path):
    _, c = historical_case(tmp_path)
    c["merchant_replies"][0]["documents"][1]["snapshot"]["source"]["order"]["items"][0]["name"] = "Changed old facts"
    with pytest.raises(CaseError):
        recorded_purchase_sources(c)

def test_historical_facts_cannot_be_marked_as_unanswered_buyer_questions(tmp_path):
    _, c = historical_case(tmp_path)
    answer = response(c)
    answer["open_questions"] = [2]
    with pytest.raises(CaseError, match="unanswered question"):
        validate_support_review(answer, c)

def test_historical_reading_persists_and_offline_target_uses_earlier_document(tmp_path):
    path, before = historical_case(tmp_path)
    reviewer = Reviewer()
    after = review_support_case(path, reviewer, len(before["history"]))
    reading = case_snapshot(path)["support_review"]
    source = reading["sources"][1]
    assert source["kind"] == "recorded_purchase" and source["document_number"] == 2
    assert current_support_review(after) == reading
    assert after["merchant_replies"] == before["merchant_replies"]
    for field in ("state", "request_id", "refund_id", "approved_cents", "purchase_context"):
        assert after[field] == before[field]
    record = render_case_record(after)
    historical = re.search(r'<section class="recorded-purchase-passages">(.*?)</section>', record, re.S).group(1)
    assert "Earlier blue mug" in historical and "Current corrected mug" not in historical
    assert '<mark class="kept-source"' in historical
    targets = re.findall(r'id="(ai-source-\d+)"', historical)
    assert targets and all(f'href="#{target}"' in record for target in targets)

@pytest.mark.parametrize("old_scope", ["current-support-v1", "current-support-purchase-v2", "current-support-refund-check-v3"])
def test_old_consent_cannot_send_new_historical_sources(tmp_path, old_scope):
    path, c = historical_case(tmp_path)
    reviewer = Reviewer()
    app = LocalCases(tmp_path, remote_reviewer=reviewer)
    before = path.read_bytes()
    payload = {"remote_confirmed": True, "destination_id": reviewer.destination_id,
               "case_revision": len(c["history"]), "review_scope": old_scope}
    with pytest.raises(CaseError, match="earlier recorded purchase summaries"):
        app.action(path.stem, "support-review", payload)
    assert reviewer.calls == 0 and path.read_bytes() == before

def test_explicit_historical_scope_saves_source_bound_reading(tmp_path):
    path, c = historical_case(tmp_path)
    reviewer = Reviewer()
    app = LocalCases(tmp_path, remote_reviewer=reviewer)
    result = app.action(path.stem, "support-review", {
        "remote_confirmed": True, "destination_id": reviewer.destination_id,
        "case_revision": len(c["history"]), "review_scope": "current-support-recorded-purchase-v4"})
    assert reviewer.calls == 1
    assert result["case"]["support_review"]["sources"][1]["kind"] == "recorded_purchase"
    assert result["case"]["merchant_replies"] == c["merchant_replies"]

def test_recorded_item_reply_uses_selected_summary_without_current_substitution(tmp_path):
    from returnlight_app.returnlight_record import recorded_purchase_details_reply, purchase_details_reply
    _, c = historical_case(tmp_path)
    before = copy.deepcopy(c)
    snapshot = c["merchant_replies"][0]["documents"][1]["snapshot"]
    result = recorded_purchase_details_reply(c, "a"*32, "1", snapshot["sha256"])
    assert "Earlier blue mug" in result["text"] and "Green bowl" in result["text"]
    assert "USD 12.50" in result["text"] and "USD 18.00" in result["text"]
    assert "Current corrected mug" not in result["text"]
    assert "The earlier purchase summary listed:" in result["text"]
    assert "not the original receipt" not in result["text"]
    assert c["capture_id"] not in result["text"] and "private-original.pdf" not in result["text"]
    assert "Current corrected mug" in purchase_details_reply(c)
    assert result["recorded_purchase"] == {"reply_id": "a"*32, "document_index": 1,
                                           "document_sha256": snapshot["sha256"]}
    assert c == before

@pytest.mark.parametrize("damage", ["other-reply", "original-file", "missing-index", "digest", "source", "other-payment"])
def test_recorded_item_reply_refuses_bad_binding_without_current_fallback(tmp_path, damage):
    from returnlight_app.returnlight_record import recorded_purchase_details_reply
    _, c = historical_case(tmp_path)
    reply_id, index = "a"*32, "1"
    snapshot = c["merchant_replies"][0]["documents"][1]["snapshot"]
    digest = snapshot["sha256"]
    if damage == "other-reply": reply_id = "c"*32
    elif damage == "original-file": index = "0"
    elif damage == "missing-index": index = None
    elif damage == "digest": digest = "0"*64
    elif damage == "source": snapshot["source"]["order"]["items"][0]["name"] = "Tampered old item"
    else: c["capture_id"] = "OTHERCAPTURE01"
    before = copy.deepcopy(c)
    with pytest.raises(CaseError):
        recorded_purchase_details_reply(c, reply_id, index, digest)
    assert c == before

def test_http_recorded_item_reply_is_bound_and_readonly(tmp_path):
    import threading
    import urllib.request
    from urllib.error import HTTPError
    from returnlight_app.returnlight_web import LocalReturnlightServer
    path, c = historical_case(tmp_path)
    before = path.read_bytes()
    app = LocalCases(tmp_path, gateway_factory=lambda: pytest.fail("No provider operation"))
    server = LocalReturnlightServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    digest = c["merchant_replies"][0]["documents"][1]["snapshot"]["sha256"]
    base = f"http://127.0.0.1:{server.server_port}/api/cases/{path.stem}/purchase-reply"
    try:
        with urllib.request.urlopen(base + "?reply=" + "a"*32 + "&document=1&sha=" + digest, timeout=3) as response:
            result = json.load(response)
        assert "Earlier blue mug" in result["text"] and "Current corrected mug" not in result["text"]
        with pytest.raises(HTTPError):
            urllib.request.urlopen(base + "?reply=" + "a"*32 + "&document=1&sha=" + "0"*64, timeout=3)
        with pytest.raises(HTTPError):
            urllib.request.urlopen(base + "?document=1", timeout=3)
        assert path.read_bytes() == before
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def selected_summary_work(c):
    document = c["merchant_replies"][0]["documents"][1]
    reference = {"reply_id": c["merchant_replies"][0]["id"], "document_index": 1,
                 "document_sha256": document["snapshot"]["sha256"]}
    return {"case_revision": len(c["history"]), "draft_version": None, "draft": {
        "decision": None, "reply": {"capture": c["capture_id"], "version": json.dumps([
            c["capture_id"], c["state"], c["approved_cents"], c["refund_id"], len(c["history"])]),
            "text": "My edited words about the earlier blue mug.", "review_required": True,
            "recorded_purchases": [reference]}}}

def test_selected_summary_work_survives_restart_edits_and_current_purchase_change(tmp_path):
    from returnlight_app.returnlight_working_drafts import WorkingDraftStore
    path, c = historical_case(tmp_path)
    before = path.read_bytes()
    store = WorkingDraftStore(tmp_path)
    request = selected_summary_work(c)
    saved = store.save(path, request)
    assert path.read_bytes() == before
    assert WorkingDraftStore(tmp_path).detail(path)[1] == saved
    c["purchase_context"]["order"]["items"][0]["name"] = "A further current change"
    path.write_text(json.dumps(c))
    restored = WorkingDraftStore(tmp_path).detail(path)[1]
    assert restored["draft"]["reply"] == request["draft"]["reply"]
    request["draft_version"] = saved["version"]
    request["draft"]["reply"]["text"] += " I will follow up Thursday."
    request["draft"]["reply"]["review_required"] = False
    revised = store.save(path, request)
    assert revised["draft"]["reply"]["recorded_purchases"] == saved["draft"]["reply"]["recorded_purchases"]
    assert revised["draft"]["reply"]["text"].endswith("Thursday.")

@pytest.mark.parametrize("change", ["wrong_reply", "original", "wrong_digest", "duplicate", "boolean_index", "too_many"])
def test_selected_summary_work_refuses_wrong_source_without_writing(tmp_path, change):
    from returnlight_app.returnlight_working_drafts import WorkingDraftStore
    path, c = historical_case(tmp_path)
    request = selected_summary_work(c)
    refs = request["draft"]["reply"]["recorded_purchases"]
    if change == "wrong_reply": refs[0]["reply_id"] = "f"*32
    if change == "original": refs[0]["document_index"] = 0
    if change == "wrong_digest": refs[0]["document_sha256"] = "f"*64
    if change == "duplicate": refs.append(copy.deepcopy(refs[0]))
    if change == "boolean_index": refs[0]["document_index"] = True
    if change == "too_many": refs.extend(copy.deepcopy(refs[0]) for _ in range(8))
    before = path.read_bytes()
    store = WorkingDraftStore(tmp_path)
    with pytest.raises(CaseError): store.save(path, request)
    assert path.read_bytes() == before and not store.directory.exists()
