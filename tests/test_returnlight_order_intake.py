"""Order lookup offers provider-attached captures without creating a case."""
import copy
import pytest
from test_returnlight_capture_http import intake_http

ORDER = "ORDERREFERENCE123"
def order():
    return {"id": ORDER, "purchase_units": [
        {"description": "Blue bowl", "payments": {"captures": [
            {"id": "CAPTUREBLUE123", "status": "COMPLETED",
             "amount": {"currency_code": "USD", "value": "13.47"}}]}},
        {"description": "Green bowl", "payments": {"captures": [
            {"id": "CAPTUREGREEN12", "status": "PARTIALLY_REFUNDED",
             "amount": {"currency_code": "USD", "value": "22.30"}}]}}]}

def test_order_choices_preserve_exact_payment_and_cents(intake_http):
    request, gateway, cases, _, _ = intake_http
    reads = []
    def read(reference):
        reads.append(reference)
        return order()
    gateway.order_details = read
    status, result = request("/api/orders/" + ORDER + "/captures")
    assert status == 200
    assert result["order_id"] == ORDER
    assert [(x["capture_id"], x["original_cents"]) for x in result["captures"]] == [
        ("CAPTUREBLUE123", 1347), ("CAPTUREGREEN12", 2230)]
    assert [x["description"] for x in result["captures"]] == ["Blue bowl", "Green bowl"]
    assert reads == [ORDER] and gateway.reads == []
    assert cases.list_cases() == [] and list(cases.directory.iterdir()) == []

@pytest.mark.parametrize("bad", ["identity", "duplicate", "currency", "amount", "missing", "invalid_capture"])
def test_unreliable_order_cannot_offer_payment(intake_http, bad):
    request, gateway, cases, _, _ = intake_http
    data = order()
    first = data["purchase_units"][0]["payments"]["captures"][0]
    if bad == "identity":
        data["id"] = "OTHERORDER123"
    elif bad == "duplicate":
        data["purchase_units"][1]["payments"]["captures"][0]["id"] = first["id"]
    elif bad == "currency":
        first["amount"]["currency_code"] = "EUR"
    elif bad == "amount":
        first["amount"]["value"] = "12.345"
    elif bad == "missing":
        data["purchase_units"] = []
    else:
        first["id"] = "BAD"
    gateway.order_details = lambda _: copy.deepcopy(data)
    status, result = request("/api/orders/" + ORDER + "/captures")
    assert status == 422
    assert "captures" not in result and gateway.reads == []
    assert cases.list_cases() == []

def test_order_host_and_bad_reference_do_not_reach_provider(intake_http):
    request, gateway, _, _, _ = intake_http
    gateway.order_details = lambda _: pytest.fail("Invalid request reached provider")
    assert request("/api/orders/" + ORDER + "/captures", headers={"Host":"untrusted.example"})[0] == 421
    assert request("/api/orders/BAD/captures")[0] in (404, 422)

