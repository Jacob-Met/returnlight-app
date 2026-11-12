import copy
import pytest
from returnlight_app.returnlight import _refund_balance_facts, CaseError

CAPTURE = "CAPTURE123456789"
REFUND = "REFUND123456789"
def facts():
    capture = {"id":CAPTURE,"status":"PARTIALLY_REFUNDED","amount":{"currency_code":"USD","value":"24.40"},"update_time":"2026-11-11T00:41:53Z"}
    refund = {"id":REFUND,"status":"COMPLETED","amount":{"currency_code":"USD","value":"9.60"},
      "update_time":"2026-11-10T16:41:53-08:00","seller_payable_breakdown":{"total_refunded_amount":{"currency_code":"USD","value":"9.60"}},
      "links":[{"rel":"up","method":"GET","href":"https://api.sandbox.paypal.com/v2/payments/captures/"+CAPTURE}]}
    order = {"purchase_units":[{"payments":{"captures":[{"id":CAPTURE}],"refunds":[{"id":REFUND}]}}]}
    class Gateway:
        def capture(self, key):
            assert key == CAPTURE
            return copy.deepcopy(capture)
        def refund_details(self, key):
            assert key == REFUND
            return copy.deepcopy(refund)
    return capture,order,refund,Gateway()

def test_provider_aggregate_and_collection_establish_observed_unrefunded_amount():
    c,o,r,g = facts()
    result = _refund_balance_facts(c,o,g)
    assert result["refunded_cents"] == 960
    assert result["unrefunded_cents"] == 1480
    assert result["refund_balance_checked_at"]

@pytest.mark.parametrize("change", ["aggregate","pending","currency","reference","duplicate","missing_time"])
def test_incomplete_or_inconsistent_refunds_do_not_produce_balance(change):
    c,o,r,g = facts()
    if change=="aggregate":r["seller_payable_breakdown"]["total_refunded_amount"]["value"]="15.00"
    if change=="pending":r["status"]="PENDING"
    if change=="currency":r["amount"]["currency_code"]="EUR"
    if change=="reference":r["links"][0]["href"] = "https://api.sandbox.paypal.com/v2/payments/captures/OTHER123456789"
    if change=="duplicate":o["purchase_units"][0]["payments"]["refunds"].append({"id":REFUND})
    if change=="missing_time":del c["update_time"]
    with pytest.raises(CaseError):_refund_balance_facts(c,o,g)

def test_capture_changed_during_reads_does_not_produce_balance():
    c,o,r,g = facts()
    original=copy.deepcopy(c)
    c["update_time"]="2026-11-11T01:00:00Z"
    with pytest.raises(CaseError,match="changed"):_refund_balance_facts(original,o,g)

def test_partial_status_cannot_claim_fully_refunded_total():
    c,o,r,g = facts()
    r["amount"]["value"]="24.40"
    r["seller_payable_breakdown"]["total_refunded_amount"]["value"]="24.40"
    with pytest.raises(CaseError,match="status"):_refund_balance_facts(c,o,g)


def test_stale_order_refunds_do_not_hide_newer_capture_activity():
    c,o,r,g = facts()
    c["update_time"]="2026-11-11T01:00:00Z"
    with pytest.raises(CaseError,match="current"):
        _refund_balance_facts(c,o,g)
