"""Provider capture creation time survives without becoming a receipt or payment authority."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from returnlight_app.returnlight import CaseError, inspect_capture, new_case, case_snapshot
from returnlight_app.returnlight_purchase_sources import purchase_sources
from returnlight_app.returnlight_record import (
    freeze_purchase_document, render_purchase_details, render_recorded_purchase_details,
    render_case_record, purchase_details_reply,
)

CAPTURE = "2WJ76930EC6952701"
CREATED = "2026-11-11T00:07:01Z"
OBSERVED = "2026-11-11T05:13:11.752169+00:00"


def facts(created=CREATED):
    class Gateway:
        def capture(self, capture_id):
            return {"id": capture_id, "status": "PARTIALLY_REFUNDED",
                    "amount": {"currency_code": "USD", "value": "24.40"},
                    "create_time": created, "update_time": "2026-11-12T00:00:00Z"}
    return inspect_capture(CAPTURE, Gateway())


def make_case(tmp_path, created=CREATED):
    value = facts(created)
    value.update(order_id="98Y26474NB668531G", order={
        "id": "98Y26474NB668531G", "total_cents": 2440,
        "items": [{"name": "Fictional ceramic spoon rest", "quantity": 1,
                   "unit_cents": 960, "line_cents": 960},
                  {"name": "Fictional linen tea towel", "quantity": 1,
                   "unit_cents": 1480, "line_cents": 1480}]})
    return new_case(tmp_path / "case.json", CAPTURE, "9.60",
                    "What date is the payment? Please send the original receipt.",
                    purchase_observation={"capture": value, "observed_at": OBSERVED})


def test_actual_retained_provider_capture_date_survives_normalization():
    import pytest; pytest.skip("uses a retained sample file that is not part of this repository")
    retained = json.loads((Path(__file__).parents[1] /
        "evidence/returnlight-workspace-partial-refund/provider-independent-observation.json").read_text())
    class Gateway:
        def capture(self, capture_id):
            assert capture_id == retained["capture"]["id"]
            return retained["capture"]
    value = inspect_capture(CAPTURE, Gateway())
    assert value["capture_created_at"] == retained["capture"]["create_time"] == CREATED
    assert value["remaining_balance_verified"] is False
    assert "update_time" not in value


@pytest.mark.parametrize("invalid", [None, True, "", "2026-11-11", "2026-11-11T00:07:01",
    "2026-02-30T00:00:00Z", "2026-11-11T00:07:01Z<script>", "x" * 65])
def test_invalid_optional_provider_date_does_not_hide_valid_payment(invalid):
    value = facts(invalid)
    assert value["original_cents"] == 2440
    assert "capture_created_at" not in value


def test_saved_date_is_separate_from_check_and_does_not_grant_authority(tmp_path):
    case = make_case(tmp_path)
    reopened = case_snapshot(tmp_path / "case.json")
    assert reopened["purchase_context"]["capture_created_at"] == CREATED
    assert reopened["purchase_context"]["observed_at"] == OBSERVED
    assert reopened["approved_cents"] == 0 and reopened["request_id"] is None
    words = purchase_sources(case)
    assert len(words) == 3
    assert "Purchase checked 11 Nov 2026, 05:13 UTC." in words[0]
    assert "PayPal payment created " + CREATED in words[0]
    assert "Fictional ceramic spoon rest" in words[1]
    assert "PayPal payment created 11 Nov 2026, 00:07:01 UTC" in render_case_record(case)
    reply = purchase_details_reply(case)
    assert "not an original receipt date" in reply
    assert "not the original receipt" in reply


def test_corrupted_saved_date_is_rejected_without_rewriting(tmp_path):
    make_case(tmp_path)
    path = tmp_path / "case.json"
    value = json.loads(path.read_text())
    value["purchase_context"]["capture_created_at"] = "yesterday"
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    with pytest.raises(CaseError, match="payment creation time"):
        case_snapshot(path)
    assert path.read_bytes() == original


def test_dated_document_freezes_version_two_and_old_version_stays_exact(tmp_path):
    case = make_case(tmp_path)
    old_case = copy.deepcopy(case)
    del old_case["purchase_context"]["capture_created_at"]
    old_bytes = render_purchase_details(old_case).encode()
    old = freeze_purchase_document(old_case, hashlib.sha256(old_bytes).hexdigest())
    assert old["version"] == 1
    assert render_recorded_purchase_details(old) == old_bytes
    current_bytes = render_purchase_details(case).encode()
    current = freeze_purchase_document(case, hashlib.sha256(current_bytes).hexdigest())
    assert current["version"] == 2
    assert current["source"]["capture_created_at"] == CREATED
    assert b"not an original receipt date" in current_bytes
    assert b"00:07:01 UTC" in current_bytes
    case["purchase_context"]["capture_created_at"] = "2026-11-12T01:02:03Z"
    assert render_recorded_purchase_details(current) == current_bytes
    assert render_recorded_purchase_details(old) == old_bytes
    assert b"00:07:01 UTC" not in render_purchase_details(case).encode()


def test_changed_dated_document_requires_new_check(tmp_path):
    case = make_case(tmp_path)
    digest = hashlib.sha256(render_purchase_details(case).encode()).hexdigest()
    case["purchase_context"]["capture_created_at"] = "2026-11-12T01:02:03Z"
    with pytest.raises(CaseError, match="changed"):
        freeze_purchase_document(case, digest)


def test_missing_date_does_not_substitute_check_time(tmp_path):
    case = make_case(tmp_path, None)
    assert "capture_created_at" not in case["purchase_context"]
    assert "PayPal payment created" not in purchase_sources(case)[0]
    assert "PayPal payment created" not in render_purchase_details(case)


def test_provider_lowercase_rfc3339_is_canonicalized():
    assert facts("2026-11-11t00:07:01z")["capture_created_at"] == CREATED
