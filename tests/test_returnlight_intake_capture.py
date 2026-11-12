"""The actual intake GET reads provider facts without creating a local case."""
import unittest
from returnlight_app.paypal import PayPalError
from test_returnlight_web import ReturnlightWebTests as _ServerFixture

class IntakeCaptureTests(unittest.TestCase):
    setUp = _ServerFixture.setUp
    shutdown = _ServerFixture.shutdown
    request = _ServerFixture.request
    create_case = _ServerFixture.create_case

    def test_original_payment_lookup_keeps_existing_case_and_storage_unchanged(self):
        key, _ = self.create_case()
        before = self.cases.path(key).read_bytes()
        status, headers, result = self.request("/api/captures/CAPTURE0123456789")
        self.assertEqual(status, 200)
        self.assertEqual(result["capture"], {
            "capture_id":"CAPTURE0123456789", "status":"COMPLETED",
            "original_cents":4200, "remaining_balance_verified":False})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.cases.path(key).read_bytes(), before)
        self.assertEqual(len(self.cases.list_cases()), 1)
        self.assertEqual(self.gateway.posts, [])

    def test_refunded_payment_is_shown_as_original_amount(self):
        self.gateway.capture = lambda capture_id: {"id":capture_id,"status":"REFUNDED",
            "amount":{"currency_code":"USD","value":"7.25"}}
        status, _, result = self.request("/api/captures/REFUNDEDCAP001")
        self.assertEqual(status, 200)
        self.assertEqual(result["capture"]["status"], "REFUNDED")
        self.assertEqual(result["capture"]["original_cents"], 725)
        self.assertFalse(result["capture"]["remaining_balance_verified"])
        self.assertEqual(self.cases.list_cases(), [])
        self.assertEqual(self.gateway.posts, [])

    def test_bad_reference_is_refused_before_provider_call(self):
        calls = []
        self.gateway.capture = lambda capture_id: calls.append(capture_id)
        for ref in ("bad", "CAPTURE-12345", "CAPTURE%2f12345", "A"*41):
            with self.subTest(reference=ref):
                self.assertEqual(self.request("/api/captures/"+ref)[0], 404)
        self.assertEqual(calls, [])
        self.assertEqual(self.cases.list_cases(), [])

    def test_provider_rejection_is_safe_and_can_be_retried_as_a_read(self):
        original = self.gateway.capture
        def refused(_):
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "private provider detail")
        self.gateway.capture = refused
        status, _, result = self.request("/api/captures/CAPTURE0123456789")
        self.assertEqual((status, result["code"]), (502, "SANDBOX_REJECTED"))
        self.assertNotIn("private provider detail", str(result))
        self.assertEqual(self.cases.list_cases(), [])
        self.gateway.capture = original
        self.assertEqual(self.request("/api/captures/CAPTURE0123456789")[0], 200)
        self.assertEqual(self.gateway.posts, [])

if __name__ == "__main__":
    unittest.main()
