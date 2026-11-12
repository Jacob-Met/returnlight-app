"""Unicode buyer text survives the real HTTP desk and bounded atomic storage."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from returnlight_app import returnlight as engine
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer

WORDS = "The parcel arrived damaged. " + "🧾" * 4962

class EvidenceModel:
    # Deliberately synthetic: these tests concern storage, not model accuracy.
    def review(self, complaint):
        return {"category": "damaged", "summary": "🧾" * 300, "quote": "🧾" * 300}

class CaseCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "case.json"

    def test_review_cannot_make_valid_unicode_case_unreadable(self):
        case = engine.new_case(self.path, "CAPTURE0123456789", "25.00", WORDS)
        case = engine.review_case(self.path, EvidenceModel())
        for _ in range(2):
            case = engine.add_buyer_followup(self.path, WORDS, len(case["history"]))
        case = engine.review_case(self.path, EvidenceModel())
        self.assertGreater(self.path.stat().st_size, 65536)
        self.assertEqual(engine.case_snapshot(self.path), case)
        self.assertEqual(case["buyer_followups"][0]["superseded_review"]["quote"], "🧾"*300)

    def test_storage_limit_refuses_before_replacing_existing_case(self):
        case = engine.new_case(self.path, "CAPTURE0123456789", "25.00", WORDS)
        before = self.path.read_bytes()
        with patch.object(engine, "_MAX_CASE_BYTES", len(before) + 100):
            with self.assertRaisesRegex(engine.CaseError, "existing record has not changed"):
                engine.review_case(self.path, EvidenceModel())
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(engine.case_snapshot(self.path), case)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

class UnicodeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cases = LocalCases(Path(self.temp.name), reviewer_factory=EvidenceModel)
        self.server = LocalReturnlightServer(("127.0.0.1", 0), self.cases)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, escaped=True):
        data = None if body is None else json.dumps(body, ensure_ascii=escaped).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={
            "Content-Type": "application/json", "X-Returnlight-Token": self.server.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_unicode_create_followup_review_reload_and_record(self):
        for escaped in (False, True):
            with self.subTest(escaped_json=escaped):
                code, data = self.request("/api/cases", {
                    "capture_id": "CAPTURE0123456789", "ceiling": "25.00", "complaint": WORDS}, escaped)
                self.assertEqual(code, 201, data)
                base = "/api/cases/" + data["key"]
                code, data = self.request(base + "/review", {})
                self.assertEqual(code, 200, data)
                for _ in range(2):
                    code, data = self.request(base + "/followup",
                        {"text": WORDS, "case_revision": len(data["case"]["history"])}, escaped)
                    self.assertEqual(code, 200, data)
                code, data = self.request(base + "/review", {})
                self.assertEqual(code, 200, data)
                self.assertEqual(self.request(base)[1]["case"], data["case"])
                self.assertIn(WORDS, self.request(base + "/record")[1]["html"])
                rows = self.request("/api/cases")[1]["cases"]
                self.assertTrue(all(row["state"] != "UNREADABLE" for row in rows))

    def test_excessive_http_body_stays_bounded(self):
        code, _ = self.request("/api/cases", {"complaint": "a" * (384 * 1024)})
        self.assertEqual(code, 413)
        self.assertEqual(self.request("/api/cases")[1]["cases"], [])

