"""Buyer itemization must not export the merchant's private case or invent a receipt."""
import copy
import json
import threading
import urllib.request

import pytest
from returnlight_app.returnlight import CaseError
from returnlight_app.returnlight_record import render_purchase_details
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer

def source():
    return {"capture_id":"CAPTURE0123456789","complaint":"PRIVATE buyer complaint",
        "merchant_replies":[{"text":"PRIVATE merchant reply"}],"support_review":{"summary":"PRIVATE inference"},
        "refund_id":"PRIVATE_REFUND","request_id":"PRIVATE_REQUEST","subject":"PRIVATE case subject",
        "purchase_context":{"capture_id":"CAPTURE0123456789","original_cents":3050,
          "observed_at":"2026-11-11T19:00:00+00:00","order_id":"ORDER0123456789",
          "order":{"id":"ORDER0123456789","total_cents":3050,"description":"PRIVATE order note","reference_id":"PRIVATE internal reference",
            "items":[{"name":"Blue ceramic mug","quantity":1,"unit_cents":1250,"line_cents":1250,"sku":"PRIVATE_SKU"},
                     {"name":"Green stoneware bowl","quantity":1,"unit_cents":1800,"line_cents":1800}]}}}

def test_buyer_document_only_contains_original_itemization():
    case=source();before=copy.deepcopy(case)
    value=render_purchase_details(case)
    for text in ["Blue ceramic mug","Green stoneware bowl","USD 12.50","USD 18.00","USD 30.50",
                 "ORDER0123456789","11 Nov 2026, 19:00:00 UTC","not the original merchant receipt"]:
        assert text in value
    assert "Sandbox · Original purchase information" in value
    assert "PRIVATE" not in value and "CAPTURE0123456789" not in value
    assert case==before
    assert "<script" not in value and "https://" not in value

def test_original_order_and_payment_are_not_reconciled_into_an_invented_total():
    case=source();case["purchase_context"]["order"]["total_cents"]=3550;case["purchase_context"]["original_cents"]=2000
    value=render_purchase_details(case)
    assert "USD 30.50" in value and "USD 35.50" in value and "USD 20.00" in value
    assert "breakdown of other charges or adjustments is not included" in value
    assert "recorded payment and saved order total are separate" in value
    assert "USD 5.00" not in value
    assert "Amount due" not in value

def test_item_text_is_inert_and_quantities_remain_exact():
    case=source();item=case["purchase_context"]["order"]["items"][0]
    item.update(name='<img src=x onerror="alert(1)">',quantity=2,line_cents=2500)
    value=render_purchase_details(case)
    assert '<img' not in value and '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;' in value
    assert "2 × USD 12.50" in value and "USD 25.00" in value

@pytest.mark.parametrize("damage",["missing","wrong-capture","empty-items","bad-line"])
def test_absent_or_mismatched_purchase_cannot_be_exported(damage):
    case=source()
    if damage=="missing":case.pop("purchase_context")
    elif damage=="wrong-capture":case["purchase_context"]["capture_id"]="OTHERCAPTURE1234"
    elif damage=="empty-items":case["purchase_context"]["order"]["items"]=[]
    else:case["purchase_context"]["order"]["items"][0]["line_cents"]=1
    with pytest.raises(CaseError):render_purchase_details(case)

def test_http_download_is_case_readonly_and_never_uses_provider(tmp_path):
    from returnlight_app.returnlight import new_case
    key="a"*32;cases=LocalCases(tmp_path/"cases",gateway_factory=lambda:pytest.fail("No provider call"))
    case=new_case(cases.path(key),"CAPTURE0123456789","30.50","PRIVATE buyer complaint")
    case["purchase_context"]=source()["purchase_context"]
    cases.path(key).write_text(json.dumps(case));before=cases.path(key).read_bytes()
    server=LocalReturnlightServer(("127.0.0.1",0),cases)
    thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01},daemon=True);thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/cases/{key}/purchase-details",timeout=3) as response:
            result=json.load(response)
        assert result["ok"] and result["filename"].endswith(".html")
        assert "Blue ceramic mug" in result["html"] and "PRIVATE" not in result["html"]
        assert cases.path(key).read_bytes()==before
    finally:server.shutdown();server.server_close();thread.join(3)

def test_explicit_item_reply_uses_same_private_fact_boundary():
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source();before=copy.deepcopy(case)
    text=purchase_details_reply(case)
    assert '1 × Blue ceramic mug — USD 12.50' in text
    assert '1 × Green stoneware bowl — USD 18.00' in text
    assert "Listed item total: USD 30.50." in text
    assert "not the original receipt" in text
    assert "PRIVATE" not in text and "CAPTURE" not in text
    for claim in ["attached", "sent", "return label", "refund completed", "I will"]:
        assert claim not in text
    assert case==before

def test_item_reply_keeps_discrepant_original_values_unreconciled():
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source();case["purchase_context"]["order"]["total_cents"]=3550
    case["purchase_context"]["original_cents"]=2000
    text=purchase_details_reply(case)
    assert "Listed item total: USD 30.50." in text
    assert "saved order total is USD 35.50" in text
    assert "original payment recorded is USD 20.00" in text
    assert "USD 5.00" not in text
    assert "other charges or adjustments" in text

def test_item_reply_does_not_turn_item_name_linebreak_into_a_merchant_assertion():
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source();case["purchase_context"]["order"]["items"][0]["name"]="Mug\nRefund sent"
    text=purchase_details_reply(case)
    assert '"Mug\\nRefund sent" —' in text
    assert "\nRefund sent" not in text

@pytest.mark.parametrize("damage",["missing","wrong-capture","empty-items","bad-line"])
def test_item_reply_refuses_unavailable_or_mismatched_source(damage):
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source()
    if damage=="missing":case.pop("purchase_context")
    elif damage=="wrong-capture":case["purchase_context"]["capture_id"]="OTHERCAPTURE1234"
    elif damage=="empty-items":case["purchase_context"]["order"]["items"]=[]
    else:case["purchase_context"]["order"]["items"][0]["line_cents"]=1
    with pytest.raises(CaseError):purchase_details_reply(case)

def test_long_item_reply_refuses_without_truncating_complete_document():
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source();item=case["purchase_context"]["order"]["items"][0]
    case["purchase_context"]["order"]["items"]=[dict(item,name=("Item "+str(i)+" "+"x"*110)) for i in range(25)]
    with pytest.raises(CaseError,match="complete itemized document"):
        purchase_details_reply(case)
    assert "Item 24" in render_purchase_details(case)

def test_http_item_reply_is_source_bound_readonly_and_never_uses_provider(tmp_path):
    from returnlight_app.returnlight import new_case
    key="a"*32;cases=LocalCases(tmp_path/"cases",gateway_factory=lambda:pytest.fail("No provider call"))
    case=new_case(cases.path(key),"CAPTURE0123456789","30.50","PRIVATE buyer complaint")
    case["purchase_context"]=source()["purchase_context"]
    cases.path(key).write_text(json.dumps(case));before=cases.path(key).read_bytes()
    server=LocalReturnlightServer(("127.0.0.1",0),cases)
    thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01},daemon=True);thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/cases/{key}/purchase-reply",timeout=3) as response:
            result=json.load(response)
        assert result["ok"] and "Blue ceramic mug" in result["text"] and "PRIVATE" not in result["text"]
        assert result["purchase_context"]==case["purchase_context"]
        assert result["case_version"]==[case["capture_id"],case["state"],case["approved_cents"],case.get("refund_id"),len(case["history"])]
        assert cases.path(key).read_bytes()==before
    finally:server.shutdown();server.server_close();thread.join(3)


def test_item_reply_keeps_multiple_quantity_unit_and_line_prices():
    from returnlight_app.returnlight_record import purchase_details_reply
    case=source();case["purchase_context"]["order"]["items"][0].update(quantity=2,line_cents=2500)
    assert "2 × Blue ceramic mug at USD 12.50 each — USD 25.00" in purchase_details_reply(case)

def test_recorded_purchase_document_keeps_checked_bytes_after_purchase_changes():
    import hashlib
    from returnlight_app.returnlight_record import freeze_purchase_document, render_recorded_purchase_details
    case=source(); original=render_purchase_details(case).encode("utf-8")
    recorded=freeze_purchase_document(case,hashlib.sha256(original).hexdigest())
    assert "PRIVATE" not in json.dumps(recorded)
    case["purchase_context"]["order"]["items"][0]["name"]="Replacement item"
    case["purchase_context"]["order"]["items"][0]["unit_cents"]=900
    case["purchase_context"]["order"]["items"][0]["line_cents"]=900
    assert render_purchase_details(case).encode("utf-8")!=original
    assert render_recorded_purchase_details(json.loads(json.dumps(recorded)))==original
    with pytest.raises(CaseError,match="changed"):
        freeze_purchase_document(case,recorded["sha256"])


@pytest.mark.parametrize("change",["version","boolean-version","digest","size","capture","item","extra"])
def test_recorded_purchase_document_refuses_changed_saved_reference(change):
    import hashlib
    from returnlight_app.returnlight_record import freeze_purchase_document, render_recorded_purchase_details
    case=source();recorded=freeze_purchase_document(case,hashlib.sha256(render_purchase_details(case).encode()).hexdigest())
    if change=="version":recorded["version"]=2
    elif change=="boolean-version":recorded["version"]=True
    elif change=="digest":recorded["sha256"]="0"*64
    elif change=="size":recorded["bytes"]+=1
    elif change=="capture":recorded["source"]["capture_id"]="../other"
    elif change=="item":recorded["source"]["order"]["items"][0]["name"]="Different item"
    else:recorded["html"]="<script>untrusted()</script>"
    with pytest.raises(CaseError):render_recorded_purchase_details(recorded)


def test_recorded_document_uses_its_version_not_future_current_renderer(monkeypatch):
    import hashlib
    import returnlight_app.returnlight_record as records
    case=source();original=records.render_purchase_details(case).encode()
    recorded=records.freeze_purchase_document(case,hashlib.sha256(original).hexdigest())
    monkeypatch.setattr(records,"render_purchase_details",lambda case:"A future document layout")
    assert records.render_recorded_purchase_details(recorded)==original
