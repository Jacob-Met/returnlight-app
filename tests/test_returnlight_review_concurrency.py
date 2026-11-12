"""Real local HTTP concurrency while a controlled reviewer waits."""
from __future__ import annotations

import concurrent.futures
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer


ORIGINAL = "The binocular focus wheel arrived jammed. Please help with a replacement."
CORRECTION = "I removed the shipping lock and the focus wheel works. I no longer need a replacement or refund."


class WaitingReviewer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = False

    def review(self, evidence):
        self.calls += 1
        self.started.set()
        if not self.release.wait(15):
            raise RuntimeError("Controlled review did not finish.")
        if self.fail:
            raise RuntimeError("Controlled model failure.")
        return {"kind": "source_selection", "quote": ORIGINAL.split(". ")[0] + "."}


class ImmediateReviewer:
    def review(self, evidence):
        return {"kind": "source_selection", "quote": "Please help with a replacement."}


class ReviewConcurrencyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="returnlight-review-concurrency-")
        self.model = WaitingReviewer()
        self.app = LocalCases(Path(self.folder.name), reviewer_factory=lambda: self.model)
        self.server = LocalReturnlightServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        status, body = self.api("/api/cases", {
            "capture_id": "CONCURRENT001", "ceiling": "89.00", "complaint": ORIGINAL,
        })
        self.assertEqual(status, 201)
        self.initial = body["case"]
        self.route = "/api/cases/" + body["key"]

    def tearDown(self):
        self.model.release.set()
        self.pool.shutdown(wait=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.folder.cleanup()

    def api(self, path, payload=None):
        request = urllib.request.Request(
            "http://127.0.0.1:" + str(self.server.server_port) + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Returnlight-Token": self.server.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def start_review(self):
        result = self.pool.submit(self.api, self.route + "/review", {})
        self.assertTrue(self.model.started.wait(2), "The controlled reviewer was not entered.")
        return result

    def before_review_finishes(self, path, payload=None):
        result = self.pool.submit(self.api, path, payload)
        status, body = result.result(timeout=2)
        self.assertFalse(self.model.release.is_set())
        return status, body

    def test_inbox_case_and_offline_record_remain_readable_during_review(self):
        review = self.start_review()
        for path in ("/api/cases", self.route, self.route + "/record"):
            with self.subTest(path=path):
                status, body = self.before_review_finishes(path)
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
        self.assertFalse(review.done())
        self.model.release.set()
        status, body = review.result(timeout=3)
        self.assertEqual(status, 200)
        self.assertEqual(body["case"]["state"], "OPEN")

    def test_buyer_correction_is_saved_and_stale_review_cannot_overwrite_it(self):
        review = self.start_review()
        status, newer = self.before_review_finishes(self.route + "/followup", {
            "text": CORRECTION, "case_revision": len(self.initial["history"]),
        })
        self.assertEqual(status, 200)
        self.assertEqual(newer["case"]["buyer_followups"][-1]["text"], CORRECTION)
        self.assertFalse(review.done())
        self.model.release.set()
        status, error = review.result(timeout=3)
        self.assertEqual((status, error["code"]), (409, "CASE_CHANGED"))
        _, current = self.api(self.route)
        self.assertEqual(current["case"], newer["case"])
        self.assertEqual(current["case"]["state"], "OPEN")
        self.assertIsNone(current["case"]["review"])
        self.assertEqual(current["case"]["approved_cents"], 0)
        self.assertIsNone(current["case"]["request_id"])

    def test_no_refund_closure_survives_a_later_review_response(self):
        review = self.start_review()
        status, closed = self.before_review_finishes(self.route + "/close", {
            "reason": "Buyer confirmed the shipping lock was removed and wants no refund.",
            "confirmation": "CLOSE " + self.initial["id"][:8],
            "case_revision": len(self.initial["history"]),
        })
        self.assertEqual(status, 200)
        self.model.release.set()
        status, error = review.result(timeout=3)
        self.assertEqual((status, error["code"]), (409, "CASE_CHANGED"))
        _, current = self.api(self.route)
        self.assertEqual(current["case"], closed["case"])
        self.assertEqual(current["case"]["state"], "CLOSED")
        self.assertIsNone(current["case"]["request_id"])

    def test_another_tab_does_not_queue_duplicate_model_inference(self):
        review = self.start_review()
        status, error = self.before_review_finishes(self.route + "/review", {})
        self.assertEqual((status, error["code"]), (409, "REVIEW_IN_PROGRESS"))
        self.assertEqual(self.model.calls, 1)
        self.assertFalse(review.done())
        self.model.release.set()
        self.assertEqual(review.result(timeout=3)[0], 200)
        self.app.reviewer_factory = ImmediateReviewer
        self.assertEqual(self.api(self.route + "/review", {})[0], 200)

    def test_failed_review_releases_its_slot_without_changing_the_case(self):
        self.model.fail = True
        review = self.start_review()
        self.model.release.set()
        status, error = review.result(timeout=3)
        self.assertEqual((status, error["code"]), (502, "MODEL_UNAVAILABLE"))
        _, current = self.api(self.route)
        self.assertEqual(current["case"], self.initial)
        self.app.reviewer_factory = ImmediateReviewer
        self.assertEqual(self.api(self.route + "/review", {})[0], 200)


if __name__ == "__main__":
    unittest.main()


class ObsoleteRecoveryTests(unittest.TestCase):
    def test_invalid_copy_does_not_start_recovery_after_case_changes(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from returnlight_app.returnlight import (
            CaseChanged, OllamaReviewer, add_buyer_followup, case_snapshot,
            human_review_case, new_case, review_case,
        )
        words = ('The glaze is cracked beside the handle. ' * 30 +
                 'Please replace only this bowl; ask me before any refund.')
        for change in ('human', 'buyer', 'unchanged'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'case.json'
                new_case(path, 'RECOVERYCASE123', '24.40', words)
                requests = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass
                    def do_POST(self):
                        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        requests.append(request)
                        if len(requests) == 1:
                            current = case_snapshot(path)
                            if change == 'human':
                                human_review_case(path, words, True,
                                                  expected_case_revision=len(current['history']))
                            elif change == 'buyer':
                                add_buyer_followup(path, CORRECTION, len(current['history']))
                            # Deliberately invalid copy; only the unchanged case needs recovery.
                            reply = {'quote': 'Words the buyer never supplied.'}
                        else:
                            reply = {'evidence_id': 0}
                        body = json.dumps({'message': {'content': json.dumps(reply)}}).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers(); self.wfile.write(body)
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    model = OllamaReviewer(endpoint=f'http://127.0.0.1:{server.server_port}/api/chat')
                    if change == 'unchanged':
                        result = review_case(path, model)
                        self.assertEqual(result['state'], 'OPEN')
                        self.assertEqual(len(requests), 2)
                    else:
                        with self.assertRaises(CaseChanged):
                            review_case(path, model)
                        saved = case_snapshot(path)
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(saved['state'], 'REVIEWED' if change == 'human' else 'OPEN')
                        if change == 'human':
                            self.assertEqual(saved['review']['kind'], 'human_selection')
                        else:
                            self.assertEqual(saved['buyer_followups'][-1]['text'], CORRECTION)
                        self.assertEqual(saved['approved_cents'], 0)
                        self.assertIsNone(saved['request_id'])
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=5)
