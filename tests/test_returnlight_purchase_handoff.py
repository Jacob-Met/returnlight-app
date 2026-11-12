"""Server-owned lookup observations cross case creation without trusting browser prices."""
import json
import pytest
from test_returnlight_capture_http import intake_http, CAPTURE

def create(request, server, base, **extra):
    return request("/api/cases", data=json.dumps({
        "capture_id": CAPTURE, "ceiling": "12.00",
        "complaint": "The kettle lid arrived dented; please help.", **extra}).encode(),
        headers={"Content-Type": "application/json", "Origin": base,
                 "X-Returnlight-Token": server.token})

def test_checked_facts_survive_creation_and_reload(intake_http):
    request, gateway, cases, server, base = intake_http
    _, observed = request("/api/captures/" + CAPTURE)
    token = observed["purchase_observation_id"]
    status, result = create(request, server, base, purchase_observation_id=token,
                            purchase_observation={"capture":{"original_cents":999999}},
                            purchase_context={"original_cents":999999})
    assert status == 201
    context = result["case"]["purchase_context"]
    assert context["original_cents"] == 3125
    assert context["capture_id"] == CAPTURE
    assert context["observed_at"]
    assert not {"status", "remaining_balance_verified", "can_attempt"} & context.keys()
    assert result["case"]["ceiling_cents"] == 1200
    assert result["case"]["approved_cents"] == 0
    assert gateway.reads == [CAPTURE]
    assert cases.detail(result["key"])["case"]["purchase_context"] == context

@pytest.mark.parametrize("token", ["unknown", [], {}, False])
def test_unusable_token_preserves_manual_creation_with_notice(intake_http, token):
    request, gateway, _, server, base = intake_http
    status, result = create(request, server, base, purchase_observation_id=token)
    assert status == 201
    assert not result["case"].get("purchase_context")
    assert result["purchase_context_notice"]
    assert gateway.reads == []

def test_expired_and_capture_changed_observations_are_not_saved(intake_http):
    request, gateway, cases, server, base = intake_http
    _, observed = request("/api/captures/" + CAPTURE)
    token = observed["purchase_observation_id"]
    status, result = create(request, server, base, capture_id="OTHERCAPTURE123",
                            purchase_observation_id=token)
    assert status == 201 and result["purchase_context_notice"]
    assert not result["case"].get("purchase_context")
    cases._purchase_observations[token]["stored_at"] -= 601
    status, result = create(request, server, base, purchase_observation_id=token)
    assert status == 201 and result["purchase_context_notice"]
    assert not result["case"].get("purchase_context")
    assert gateway.reads == [CAPTURE]

def test_no_token_keeps_offline_intake(intake_http):
    request, gateway, _, server, base = intake_http
    status, result = create(request, server, base)
    assert status == 201
    assert "purchase_context_notice" not in result
    assert not result["case"].get("purchase_context")
    assert gateway.reads == []

def test_observation_cache_bounded_and_response_cannot_change_cached_prices(intake_http):
    _, gateway, cases, _, _ = intake_http
    first = cases.inspect_payment(CAPTURE)
    token = first["purchase_observation_id"]
    first["capture"]["original_cents"] = 1
    assert cases._purchase_observations[token]["capture"]["original_cents"] == 3125
    for _ in range(128):
        cases.inspect_payment(CAPTURE)
    assert len(cases._purchase_observations) == 128
    assert token not in cases._purchase_observations

def keep(request, server, base, key, token, revision):
    return request("/api/cases/" + key + "/purchase", data=json.dumps({
        "purchase_observation_id": token, "case_revision": revision,
        "purchase_context": {"original_cents": 999999}}).encode(),
        headers={"Content-Type": "application/json", "Origin": base,
                 "X-Returnlight-Token": server.token})

def test_existing_case_keeps_server_purchase_without_refund_change(intake_http):
    request, gateway, cases, server, base = intake_http
    _, opened = create(request, server, base)
    key = opened["key"]
    before = cases.detail(key)["case"]
    _, observed = request("/api/cases/" + key + "/capture")
    assert cases.detail(key)["case"] == before
    status, saved = keep(request, server, base, key, observed["purchase_observation_id"], len(before["history"]))
    assert status == 200
    case = saved["case"]
    assert case["purchase_context"]["original_cents"] == 3125
    for field in ("state", "ceiling_cents", "approved_cents", "refund_id", "request_id", "review"):
        assert case[field] == before[field]
    assert cases.detail(key)["case"]["purchase_context"] == case["purchase_context"]
    assert gateway.reads == [CAPTURE]

@pytest.mark.parametrize("failure", ["expired", "wrong_capture", "stale_revision", "missing"])
def test_existing_case_rejects_unusable_observation(intake_http, failure):
    request, gateway, cases, server, base = intake_http
    _, opened = create(request, server, base)
    key = opened["key"]
    before = cases.detail(key)["case"]
    _, observed = request("/api/cases/" + key + "/capture")
    token = observed["purchase_observation_id"]
    revision = len(before["history"])
    if failure == "expired":
        cases._purchase_observations[token]["stored_at"] -= 601
    elif failure == "wrong_capture":
        cases._purchase_observations[token]["capture"]["capture_id"] = "OTHERCAPTURE123"
    elif failure == "stale_revision":
        revision += 1
    else:
        token = None
    status, _ = keep(request, server, base, key, token, revision)
    assert status == (409 if failure == "stale_revision" else 422)
    assert cases.detail(key)["case"] == before
