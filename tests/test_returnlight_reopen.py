"""No-refund reconsideration; local model/provider fixtures are explicit and never external."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from returnlight_app.returnlight import (CaseError, new_case, close_case, reopen_case, case_snapshot,
                                 review_case, human_review_case, approve_case, approval_phrase)
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer

ORIGINAL = "The linen pouch arrived with a mark beside its compass pocket."
REASON = "Buyer confirmed cleaning removed the mark and kept the pouch without a refund."
NEW = "The seam has now split beside the compass pocket. Please review a replacement."

class LocalModel:
    def review(self, text):
        return {"category": "damaged", "summary": "The buyer reported a mark.", "quote": ORIGINAL}

class ReopenCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "case.json"
        c = new_case(self.path, "SYNTHETICPOUCH14", "38.50", ORIGINAL)
        suggested = review_case(self.path, LocalModel())
        human_review_case(self.path, ORIGINAL, True, expected_case_revision=len(suggested["history"]))
        approve_case(self.path, "7.35", approval_phrase(c, "7.35"))
        self.closed = close_case(self.path, REASON, "CLOSE " + c["id"][:8])

    def tearDown(self):
        self.temp.cleanup()

    def test_reopen_retains_identity_closure_and_review_history_but_requires_new_review(self):
        old = self.closed
        c = reopen_case(self.path, NEW, len(old["history"]))
        for key in ("id", "capture_id", "complaint", "ceiling_cents"):
            self.assertEqual(c[key], old[key])
        self.assertEqual(c["history"][:len(old["history"])], old["history"])
        self.assertEqual(c["buyer_followups"][-1]["superseded_resolution"], old["resolution"])
        self.assertEqual(c["buyer_followups"][-1]["superseded_review"], old["review"])
        self.assertEqual(c["state"], "OPEN")
        self.assertIsNone(c["review"])
        self.assertEqual(c["approved_cents"], 0)
        self.assertNotIn("resolution", c)
        self.assertIsNone(c["request_id"])
        self.assertIsNone(c["refund_id"])
        self.assertTrue(c["reopened_at"])
        with self.assertRaises(CaseError):
            approve_case(self.path, "7.35", approval_phrase(c, "7.35"))
        self.assertEqual(case_snapshot(self.path), c)

    def test_stale_reopening_never_changes_saved_case(self):
        before = self.path.read_bytes()
        with self.assertRaises(CaseError):
            reopen_case(self.path, NEW, len(self.closed["history"]) - 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_evidence_never_changes_saved_case(self):
        for value in (" ", "", None, "x" * 15001):
            before = self.path.read_bytes()
            with self.assertRaises(CaseError):
                reopen_case(self.path, value, len(self.closed["history"]))
            self.assertEqual(self.path.read_bytes(), before)

    def test_attempted_or_other_states_cannot_reopen(self):
        for state in ("OPEN", "REVIEWED", "APPROVED", "UNKNOWN", "PENDING", "COMPLETED", "FAILED"):
            c = dict(self.closed, state=state)
            self.path.write_text(json.dumps(c))
            before = self.path.read_bytes()
            with self.assertRaises(CaseError):
                reopen_case(self.path, NEW, len(c["history"]))
            self.assertEqual(self.path.read_bytes(), before)
        for field in ("request_id", "refund_id"):
            c = dict(self.closed)
            c[field] = "EXPLICIT-LOCAL-ATTEMPT-FIXTURE"
            self.path.write_text(json.dumps(c))
            before = self.path.read_bytes()
            with self.assertRaises(CaseError):
                reopen_case(self.path, NEW, len(c["history"]))
            self.assertEqual(self.path.read_bytes(), before)

    def test_only_no_refund_resolution_is_reconsidered(self):
        c = dict(self.closed, resolution={"outcome": "external_refund", "reason": "Fixture"})
        self.path.write_text(json.dumps(c))
        with self.assertRaises(CaseError):
            reopen_case(self.path, NEW, len(c["history"]))

    def test_repeated_closure_keeps_each_prior_resolution(self):
        first = reopen_case(self.path, NEW, len(self.closed["history"]))
        second_reason = "Buyer accepted the replacement pouch without a refund."
        second = close_case(self.path, second_reason, "CLOSE " + first["id"][:8])
        final = reopen_case(self.path, "The replacement has the same seam failure. Please reconsider.", len(second["history"]))
        self.assertEqual([e["superseded_resolution"]["reason"] for e in final["buyer_followups"]], [REASON, second_reason])
        self.assertEqual(sum(e["event"] == "merchant.closed_without_refund" for e in final["history"]), 2)
        self.assertEqual(sum(e["event"] == "merchant.reopened_for_review" for e in final["history"]), 2)

    def test_offline_record_explains_current_review_and_retains_both_evidence_and_closure(self):
        c = reopen_case(self.path, NEW, len(self.closed["history"]))
        html = render_case_record(c)
        for text in ("Reopened · review needed", ORIGINAL, NEW, REASON, "No current refund amount approved"):
            self.assertIn(text, html)

    def test_http_concurrent_reopen_records_one_message_and_never_calls_provider_or_model(self):
        def forbidden():
            raise AssertionError("Reopening must not call a model or provider")
        cases = LocalCases(self.root / "http", reviewer_factory=forbidden, gateway_factory=forbidden)
        created = cases.create({"capture_id": "SYNTHETICHTTP14", "ceiling": "38.50", "complaint": ORIGINAL})
        key = created["key"]
        closed = cases.action(key, "close", {"reason": REASON, "confirmation": "CLOSE " + created["case"]["id"][:8],
                                           "case_revision": len(created["case"]["history"])})["case"]
        server = LocalReturnlightServer(("127.0.0.1", 0), cases)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True); thread.start()
        def send():
            req = urllib.request.Request("http://127.0.0.1:" + str(server.server_port) + "/api/cases/" + key + "/reopen",
                data=json.dumps({"text": NEW, "case_revision": len(closed["history"])}).encode(),
                headers={"Content-Type": "application/json", "X-Returnlight-Token": server.token})
            try:
                with urllib.request.urlopen(req) as r: return r.status
            except urllib.error.HTTPError as e: return e.code
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: send(), range(2))), [200, 422])
            c = cases.detail(key)["case"]
            self.assertEqual(len(c["buyer_followups"]), 1)
            self.assertEqual(c["history"][:len(closed["history"])], closed["history"])
            self.assertEqual(cases.summary(key)["queue"], "decide")
            self.assertTrue(cases.summary(key)["reopened_at"])
        finally:
            server.shutdown(); server.server_close(); thread.join()

if __name__ == "__main__":
    unittest.main()
