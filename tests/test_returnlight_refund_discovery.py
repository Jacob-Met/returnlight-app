"""Refund discovery reads real provider structure without guessing request identity."""
import copy
import json
import pytest
from returnlight_app.returnlight import (CaseError, OutcomeUnknown, new_case, approve_case,
    approval_phrase, case_snapshot, review_case, human_review_case, execute_case, refresh_case,
    discover_case_refunds, _refund_capture_reference)

CAPTURE="CAPTURE0123456789"
ORDER="ORDER0123456789"
REFUND="REFUND0123456789"

class Model:
    def review(self, text):
        return {"kind":"source_selection","quote":"refund the damaged mug"}
class Gateway:
    def __init__(self):
        self.reads=[]; self.posts=[]
        self.refunds=[REFUND]
        self.detail={"id":REFUND,"status":"COMPLETED","amount":{"currency_code":"USD","value":"9.60"},
            "create_time":"2026-11-11T00:41:53Z",
            "links":[{"rel":"up","method":"GET","href":"https://api.sandbox.paypal.com/v2/payments/captures/"+CAPTURE}]}
        self.order_id=ORDER; self.order_capture=CAPTURE
    def capture(self, cid):
        self.reads.append(("capture",cid))
        return {"id":cid,"status":"COMPLETED","amount":{"currency_code":"USD","value":"24.40"},
            "supplementary_data":{"related_ids":{"order_id":ORDER}}}
    def order_details(self, oid):
        self.reads.append(("order",oid))
        return {"id":self.order_id,"purchase_units":[{"payments":{
            "captures":[{"id":self.order_capture}],"refunds":[{"id":r} for r in self.refunds]}}]}
    def refund(self,cid,cents,request):
        self.posts.append((cid,cents,request))
        raise TimeoutError("controlled response loss")
    def refund_details(self,rid):
        self.reads.append(("refund",rid))
        return {**copy.deepcopy(self.detail),"id":rid}

@pytest.fixture
def uncertain(tmp_path):
    path=tmp_path/"case.json"
    new_case(path,CAPTURE,"24.40","Please refund the damaged mug and keep the tea towel.")
    review_case(path,Model())
    case=case_snapshot(path)
    human_review_case(path,case["complaint"],True,expected_case_revision=len(case["history"]))
    approve_case(path,"9.60",approval_phrase(case,"9.60"))
    g=Gateway()
    with pytest.raises(OutcomeUnknown): execute_case(path,g)
    return path,g

def test_discovery_is_read_only_and_explicit_refresh_uses_real_up_link(uncertain):
    path,g=uncertain; before=path.read_bytes()
    found=discover_case_refunds(path,g)
    assert found["refunds"]==[{"id":REFUND,"status":"COMPLETED","amount_cents":960,"created_at":"2026-11-11T00:41:53Z"}]
    assert path.read_bytes()==before
    assert case_snapshot(path)["state"]=="UNKNOWN" and len(g.posts)==1
    assert refresh_case(path,g,REFUND)["state"]=="COMPLETED"
    assert len(g.posts)==1

@pytest.mark.parametrize("change",["capture","currency","amount"])
def test_other_capture_currency_or_amount_is_not_a_candidate(uncertain,change):
    path,g=uncertain
    if change=="capture": g.detail["links"][0]["href"]=g.detail["links"][0]["href"].replace(CAPTURE,"OTHER0123456789")
    if change=="currency": g.detail["amount"]["currency_code"]="EUR"
    if change=="amount": g.detail["amount"]["value"]="9.61"
    assert discover_case_refunds(path,g)["refunds"]==[]
    assert case_snapshot(path)["state"]=="UNKNOWN" and len(g.posts)==1

@pytest.mark.parametrize("href",[
    "https://evil.example/v2/payments/captures/"+CAPTURE,
    "https://api.paypal.com/v2/payments/captures/"+CAPTURE,
    "https://api.sandbox.paypal.com@evil.example/v2/payments/captures/"+CAPTURE,
    "https://api.sandbox.paypal.com/v2/payments/captures/"+CAPTURE+"?x=1",
    "http://api.sandbox.paypal.com/v2/payments/captures/"+CAPTURE])
def test_untrusted_links_never_authorize_recovery(uncertain,href):
    path,g=uncertain;g.detail["links"][0]["href"]=href
    with pytest.raises(CaseError): discover_case_refunds(path,g)
    with pytest.raises(CaseError): refresh_case(path,g,REFUND,True)
    assert len(g.posts)==1 and case_snapshot(path)["state"]=="UNKNOWN"

def test_conflicting_provider_references_are_refused(uncertain):
    path,g=uncertain
    g.detail["supplementary_data"]={"related_ids":{"capture_id":"OTHER0123456789"}}
    with pytest.raises(CaseError,match="conflict"): discover_case_refunds(path,g)

@pytest.mark.parametrize("field",["order_id","order_capture"])
def test_exact_order_and_capture_required(uncertain,field):
    path,g=uncertain;setattr(g,field,"OTHER0123456789")
    with pytest.raises(CaseError):discover_case_refunds(path,g)

def test_all_matching_candidates_retained_and_duplicates_removed(uncertain):
    path,g=uncertain;g.refunds=[REFUND,REFUND,"REFUND9876543210"]
    found=discover_case_refunds(path,g)
    assert [r["id"] for r in found["refunds"]]==[REFUND,"REFUND9876543210"]
    assert case_snapshot(path)["state"]=="UNKNOWN"
    assert g.reads.count(("refund",REFUND))==1

def test_missing_parent_link_is_not_automatically_assumed(uncertain):
    path,g=uncertain;g.detail.pop("links")
    assert discover_case_refunds(path,g)["refunds"]==[]
    with pytest.raises(CaseError,match="confirm"):refresh_case(path,g,REFUND)

def test_recorded_refund_uses_one_direct_read_without_order_and_preserves_case(uncertain):
    path,g=uncertain
    refresh_case(path,g,REFUND)
    before=path.read_bytes();g.reads.clear()
    g.capture=lambda cid: pytest.fail("A recorded refund does not need capture discovery")
    g.order_details=lambda oid: pytest.fail("A recorded refund does not need order discovery")
    found=discover_case_refunds(path,g)
    assert found["scope"]=="recorded_refund"
    assert found["refunds"][0]["id"]==REFUND
    assert g.reads==[("refund",REFUND)]
    assert path.read_bytes()==before and len(g.posts)==1

@pytest.mark.parametrize("change",["id","capture","currency","amount","link","status"])
def test_recorded_refund_rejects_unbound_observation(uncertain,change):
    path,g=uncertain
    refresh_case(path,g,REFUND);before=path.read_bytes()
    if change=="id":
        g.refund_details=lambda rid:{**g.detail,"id":"OTHER0123456789"}
    if change=="capture":g.detail["links"][0]["href"]=g.detail["links"][0]["href"].replace(CAPTURE,"OTHER0123456789")
    if change=="currency":g.detail["amount"]["currency_code"]="EUR"
    if change=="amount":g.detail["amount"]["value"]="9.61"
    if change=="link":g.detail.pop("links")
    if change=="status":g.detail["status"]="OTHER"
    with pytest.raises(CaseError):discover_case_refunds(path,g)
    assert path.read_bytes()==before and len(g.posts)==1

def test_reading_changed_provider_status_does_not_rewrite_terminal_history(uncertain):
    path,g=uncertain
    refresh_case(path,g,REFUND);before=path.read_bytes()
    g.detail["status"]="FAILED"
    found=discover_case_refunds(path,g)
    assert found["refunds"][0]["status"]=="FAILED"
    assert case_snapshot(path)["state"]=="COMPLETED" and path.read_bytes()==before
    with pytest.raises(CaseError):refresh_case(path,g)

@pytest.mark.parametrize("invalid",[42,"bad/reference","short"])
def test_invalid_recorded_reference_is_refused_before_provider_read(uncertain,invalid):
    path,g=uncertain
    refresh_case(path,g,REFUND)
    case=json.loads(path.read_text());case["refund_id"]=invalid
    path.write_text(json.dumps(case));before=path.read_bytes();g.reads.clear()
    with pytest.raises(CaseError,match="identifier"):discover_case_refunds(path,g)
    assert g.reads==[] and path.read_bytes()==before
