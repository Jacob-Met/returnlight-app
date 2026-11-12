from returnlight_app.returnlight import human_review_case
"""Retained purchase facts survive case visits without becoming payment authority."""
import copy
import json
import pytest

from returnlight_app.returnlight import (
    CaseError, approve_case, capture_preview, case_snapshot, execute_case,
    new_case, review_case,
)

CAPTURE = "RETAINEDCAPTURE01"
COMPLAINT = "Only the ceramic spoon rest arrived broken. Please refund its $9.60 price."
OBSERVED = "2026-11-11T01:40:00+00:00"


def observation():
    return {"observed_at": OBSERVED, "capture": {
        "capture_id": CAPTURE, "status": "COMPLETED", "original_cents": 2440,
        "remaining_balance_verified": False, "can_attempt": True,
        "order_id": "RETAINEDORDER0001", "order": {
            "id": "RETAINEDORDER0001", "total_cents": 2440,
            "items": [
                {"name": "Ceramic spoon rest", "quantity": 1, "unit_cents": 960,
                 "line_cents": 960, "sku": "SPOON"},
                {"name": "Linen tea towel", "quantity": 1, "unit_cents": 1480,
                 "line_cents": 1480},
            ],
        },
    }}


def test_checked_purchase_survives_returning_without_restoring_eligibility(tmp_path):
    path = tmp_path / "case.json"
    source = observation()
    created = new_case(path, CAPTURE, "9.60", COMPLAINT, purchase_observation=source)
    stored = created["purchase_context"]
    assert stored["observed_at"] == OBSERVED
    assert stored["original_cents"] == 2440
    assert [item["line_cents"] for item in stored["order"]["items"]] == [960, 1480]
    assert not {"status", "can_attempt", "remaining_balance_verified"} & set(stored)
    source["capture"]["order"]["items"][0]["name"] = "Changed after opening"
    returned = case_snapshot(path)
    assert returned["purchase_context"]["order"]["items"][0]["name"] == "Ceramic spoon rest"
    assert returned["ceiling_cents"] == 960
    assert returned["approved_cents"] == 0 and returned["request_id"] is None
    assert returned["history"][-1]["event"] == "purchase.observed"


def test_original_capture_without_optional_order_is_still_useful(tmp_path):
    value = observation()
    del value["capture"]["order"]
    value["capture"]["order_issue"] = "Order details could not be loaded."
    case = new_case(tmp_path / "case.json", CAPTURE, "9.60", COMPLAINT,
                    purchase_observation=value)
    assert "order" not in case["purchase_context"]
    assert "order_issue" not in case["purchase_context"]
    assert case["purchase_context"]["order_id"] == "RETAINEDORDER0001"


def test_manual_and_existing_cases_need_no_provider_snapshot(tmp_path):
    path = tmp_path / "manual.json"
    case = new_case(path, CAPTURE, "9.60", COMPLAINT)
    before = path.read_bytes()
    assert "purchase_context" not in case
    assert case_snapshot(path) == case
    assert path.read_bytes() == before


def test_context_cannot_follow_a_changed_capture(tmp_path):
    path = tmp_path / "case.json"
    with pytest.raises(CaseError, match="another payment"):
        new_case(path, "DIFFERENTCAPTURE01", "9.60", COMPLAINT,
                 purchase_observation=observation())
    assert not path.exists()


@pytest.mark.parametrize("change", [
    lambda value: value["capture"].update(original_cents=True),
    lambda value: value.update(observed_at="2026-11-11T01:40:00"),
    lambda value: value["capture"]["order"]["items"][0].update(line_cents=970),
    lambda value: value["capture"]["order"].update(id="OTHERORDER0001"),
])
def test_corrupt_retained_facts_do_not_create_a_case(tmp_path, change):
    value = observation()
    change(value)
    path = tmp_path / "case.json"
    with pytest.raises(CaseError):
        new_case(path, CAPTURE, "9.60", COMPLAINT, purchase_observation=value)
    assert not path.exists()


def test_changed_local_capture_cannot_display_another_purchases_facts(tmp_path):
    path = tmp_path / "case.json"
    new_case(path, CAPTURE, "9.60", COMPLAINT, purchase_observation=observation())
    saved = json.loads(path.read_text())
    saved["capture_id"] = "DIFFERENTCAPTURE01"
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    with pytest.raises(CaseError, match="another payment"):
        case_snapshot(path)
    assert path.read_bytes() == before


def test_fresh_provider_amount_still_controls_refund_preflight(tmp_path):
    class Model:
        def review(self, complaint):
            return {"kind": "source_selection", "quote": complaint}
    class Gateway:
        def __init__(self):
            self.refunds = 0
        def capture(self, capture_id):
            return {"id": capture_id, "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "5.00"}}
        def refund(self, *args):
            self.refunds += 1
            raise AssertionError("Saved original amount must not authorize a refund")
    path = tmp_path / "case.json"
    new_case(path, CAPTURE, "24.40", COMPLAINT, purchase_observation=observation())
    review_case(path, Model())
    case = case_snapshot(path)
    human_review_case(path, COMPLAINT, True, expected_case_revision=len(case["history"]))
    approve_case(path, "9.60", f"REFUND {case['id'][:8]} 9.60")
    gateway = Gateway()
    assert capture_preview(path, gateway)["original_cents"] == 500
    with pytest.raises(CaseError):
        execute_case(path, gateway)
    assert gateway.refunds == 0
    assert case_snapshot(path)["request_id"] is None
    assert case_snapshot(path)["purchase_context"]["original_cents"] == 2440


def test_download_record_retains_purchase_items_with_literal_names(tmp_path):
    from returnlight_app.returnlight_record import render_case_record
    value = observation()
    value["capture"]["order"]["items"][0]["name"] = 'Spoon <display> & "rest"'
    value["capture"]["order"]["description"] = "Kitchen <set>"
    path = tmp_path / "case.json"
    new_case(path, CAPTURE, "9.60", COMPLAINT, purchase_observation=value)
    before = path.read_bytes()
    html = render_case_record(case_snapshot(path), "2026-11-11T02:00:00+00:00")
    assert 'Spoon &lt;display&gt; &amp; &quot;rest&quot;' in html
    assert "Kitchen &lt;set&gt;" in html
    assert "Linen tea towel" in html
    assert "<td>USD 9.60</td>" in html and "<td>USD 14.80</td>" in html
    assert "Original payment <strong>USD 24.40</strong>" in html
    assert "Purchase checked 11 Nov 2026, 01:40:00 UTC" in html
    assert "No current refund amount approved" in html
    assert "<display>" not in html and "Remaining balance" not in html
    assert path.read_bytes() == before


def test_download_record_without_order_keeps_checked_original_amount(tmp_path):
    from returnlight_app.returnlight_record import render_case_record
    value = observation()
    del value["capture"]["order"]
    path = tmp_path / "case.json"
    new_case(path, CAPTURE, "9.60", COMPLAINT, purchase_observation=value)
    html = render_case_record(case_snapshot(path))
    assert "Original payment <strong>USD 24.40</strong>" in html
    assert "Order RETAINEDORDER0001" in html
    assert '<table class="purchase-table"' not in html
