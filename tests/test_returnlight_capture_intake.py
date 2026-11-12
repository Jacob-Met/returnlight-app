"""Payment facts can be read before intake without creating a refund case."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from returnlight_app.returnlight import CaseError, capture_preview, inspect_capture, new_case


CAPTURE = "INTAKECAPTURE001"


class CaptureReader:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def capture(self, capture_id):
        self.calls.append(capture_id)
        return self.response

    def refund(self, *args):
        raise AssertionError("Read-only intake must never issue a refund.")


def payment(status="COMPLETED"):
    return {"id": CAPTURE, "status": status,
            "amount": {"currency_code": "USD", "value": "7.25"}}


class CaptureIntakeTests(unittest.TestCase):
    def test_original_amount_is_inspectable_before_a_case_exists(self):
        gateway = CaptureReader(payment())
        result = inspect_capture(CAPTURE, gateway)
        self.assertEqual(result, {
            "capture_id": CAPTURE, "status": "COMPLETED", "original_cents": 725,
            "remaining_balance_verified": False,
        })
        self.assertEqual(gateway.calls, [CAPTURE])
        self.assertNotIn("can_attempt", result)

    def test_refunded_capture_never_represents_original_amount_as_available_balance(self):
        for status in ("REFUNDED", "PARTIALLY_REFUNDED", "PENDING"):
            with self.subTest(status=status):
                result = inspect_capture(CAPTURE, CaptureReader(payment(status)))
                self.assertEqual((result["status"], result["original_cents"]), (status, 725))
                self.assertFalse(result["remaining_balance_verified"])
                self.assertNotIn("can_attempt", result)

    def test_bad_input_is_refused_before_provider_read(self):
        gateway = CaptureReader(payment())
        for invalid in (None, "", "../private", "A" * 41, "HAS SPACE1", ["CAPTURE"]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CaseError):
                    inspect_capture(invalid, gateway)
        self.assertEqual(gateway.calls, [])

    def test_untrusted_payment_facts_cannot_supply_an_intake_limit(self):
        variants = [
            None,
            {"id": "OTHERPAYMENT001", "status": "COMPLETED",
             "amount": {"currency_code": "USD", "value": "7.25"}},
            {"id": CAPTURE, "status": "COMPLETED",
             "amount": {"currency_code": "EUR", "value": "7.25"}},
            {"id": CAPTURE, "status": "COMPLETED", "amount": {"currency_code": "USD"}},
            {"id": CAPTURE, "status": "COMPLETED",
             "amount": {"currency_code": "USD", "value": "-7.25"}},
            {"id": CAPTURE, "status": ["COMPLETED"],
             "amount": {"currency_code": "USD", "value": "7.25"}},
        ]
        for response in variants:
            with self.subTest(response=response):
                with self.assertRaises(CaseError):
                    inspect_capture(CAPTURE, CaptureReader(response))

    def test_saved_case_preview_preserves_the_case_and_its_existing_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "case.json"
            new_case(path, CAPTURE, "9.00", "The tea strainer arrived with a damaged handle.")
            before = path.read_bytes()
            original = payment()
            expected_original = copy.deepcopy(original)
            result = capture_preview(path, CaptureReader(original))
            self.assertEqual(result["original_cents"], 725)
            self.assertFalse(result["ceiling_within_original"])
            self.assertTrue(result["approved_within_original"])
            self.assertFalse(result["can_attempt"])
            self.assertFalse(result["remaining_balance_verified"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(original, expected_original)
            self.assertIsNone(json.loads(before)["request_id"])


ORDER = "INTAKEORDER0001"


def contextual_payment():
    result = payment()
    result["amount"]["value"] = "24.40"
    result["supplementary_data"] = {"related_ids": {"order_id": ORDER}}
    return result


def purchase():
    return {"id": ORDER, "purchase_units": [{
        "reference_id": "fictional-two-item-purchase",
        "description": "A spoon rest and a tea towel",
        "amount": {"currency_code": "USD", "value": "24.40"},
        "payments": {"captures": [{"id": CAPTURE}]},
        "items": [
            {"name": "Ceramic spoon rest", "quantity": "1", "sku": "SPOON",
             "unit_amount": {"currency_code": "USD", "value": "9.60"}},
            {"name": "Linen tea towel", "quantity": "1",
             "unit_amount": {"currency_code": "USD", "value": "14.80"}},
        ],
    }]}


class PurchaseReader(CaptureReader):
    def __init__(self, order):
        super().__init__(contextual_payment())
        self.order = order
        self.order_calls = []

    def order_details(self, order_id):
        self.order_calls.append(order_id)
        if isinstance(self.order, Exception):
            raise self.order
        return self.order


class PurchaseContextTests(unittest.TestCase):
    def test_two_item_purchase_is_read_without_turning_prices_into_refund_authority(self):
        order = purchase()
        before = copy.deepcopy(order)
        gateway = PurchaseReader(order)
        result = inspect_capture(CAPTURE, gateway)
        self.assertEqual(result["original_cents"], 2440)
        self.assertFalse(result["remaining_balance_verified"])
        self.assertNotIn("can_attempt", result)
        self.assertEqual(result["order_id"], ORDER)
        self.assertEqual(result["order"]["total_cents"], 2440)
        self.assertEqual(result["order"]["items"], [
            {"name": "Ceramic spoon rest", "quantity": 1, "unit_cents": 960,
             "line_cents": 960, "sku": "SPOON"},
            {"name": "Linen tea towel", "quantity": 1, "unit_cents": 1480,
             "line_cents": 1480},
        ])
        self.assertEqual(gateway.calls, [CAPTURE])
        self.assertEqual(gateway.order_calls, [ORDER])
        self.assertEqual(order, before)

    def test_context_comes_from_matching_capture_not_the_first_purchase_unit(self):
        order = purchase()
        unrelated = copy.deepcopy(order["purchase_units"][0])
        unrelated["payments"]["captures"][0]["id"] = "OTHERPAYMENT001"
        unrelated["items"][0]["name"] = "A different buyer's purchase"
        order["purchase_units"].insert(0, unrelated)
        result = inspect_capture(CAPTURE, PurchaseReader(order))
        self.assertEqual(result["order"]["items"][0]["name"], "Ceramic spoon rest")

    def test_wrong_order_or_capture_does_not_display_another_purchases_items(self):
        wrong_order = purchase()
        wrong_order["id"] = "OTHERORDER0001"
        wrong_capture = purchase()
        wrong_capture["purchase_units"][0]["payments"]["captures"][0]["id"] = "OTHERPAYMENT001"
        repeated = purchase()
        repeated["purchase_units"].append(copy.deepcopy(repeated["purchase_units"][0]))
        for order in (wrong_order, wrong_capture, repeated):
            with self.subTest(order=order):
                result = inspect_capture(CAPTURE, PurchaseReader(order))
                self.assertNotIn("order", result)
                self.assertIn("order_issue", result)
                self.assertEqual(result["original_cents"], 2440)

    def test_missing_item_lines_preserves_the_real_order_description(self):
        order = purchase()
        del order["purchase_units"][0]["items"]
        result = inspect_capture(CAPTURE, PurchaseReader(order))
        self.assertEqual(result["order"]["items"], [])
        self.assertEqual(result["order"]["description"], "A spoon rest and a tea towel")

    def test_unavailable_optional_order_keeps_valid_capture_facts_and_does_not_retry(self):
        gateway = PurchaseReader(TimeoutError("order response unavailable"))
        result = inspect_capture(CAPTURE, gateway)
        self.assertEqual(result["original_cents"], 2440)
        self.assertNotIn("order", result)
        self.assertEqual(result["order_id"], ORDER)
        self.assertIn("order_issue", result)
        self.assertEqual(gateway.calls, [CAPTURE])
        self.assertEqual(gateway.order_calls, [ORDER])

    def test_unicode_names_and_free_items_are_preserved_without_inventing_prices(self):
        order = purchase()
        order["purchase_units"][0]["items"].append({
            "name": "陶瓷の匙 — sample", "quantity": "2",
            "unit_amount": {"currency_code": "USD", "value": "0.00"}})
        result = inspect_capture(CAPTURE, PurchaseReader(order))
        self.assertEqual(result["order"]["items"][-1], {
            "name": "陶瓷の匙 — sample", "quantity": 2, "unit_cents": 0, "line_cents": 0})


if __name__ == "__main__":
    unittest.main()
