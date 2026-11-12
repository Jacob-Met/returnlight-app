"""Actual HTTP journey checks for Returnlight's loopback merchant UI."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from returnlight_app.returnlight import approval_revision
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer


COMPLAINT = "I ordered a glazed mug, and the handle arrived cracked on delivery."
CAPTURE = "CAPTURE0123456789"
REFUND = "REFUND0123456789"


class FakeModel:
    def __init__(self, bad=False):
        self.bad = bad

    def review(self, complaint):
        return {
            "category": "damaged", "summary": "Cracked handle on delivery",
            "quote": "not in buyer text" if self.bad else "handle arrived cracked",
            "recommended_percent": 50, "reason": "Physical shipping damage is reported",
        }


class FakeSandbox:
    def __init__(self, lost=False):
        self.lost = lost
        self.posts = []

    def capture(self, capture_id):
        return {"id": capture_id, "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "42.00"}}

    def refund(self, capture_id, cents, request_id):
        self.posts.append((capture_id, cents, request_id))
        if self.lost:
            raise TimeoutError("response lost after request")
        return {"id": REFUND, "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "21.00"}}

    def refund_details(self, refund_id):
        return {"id": refund_id, "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "21.00"},
                "supplementary_data": {"related_ids": {"capture_id": CAPTURE}}}


class ReturnlightWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = FakeSandbox()
        self.bad_model = False
        self.cases = LocalCases(Path(self.temp.name) / "private",
                                reviewer_factory=lambda: FakeModel(self.bad_model),
                                gateway_factory=lambda: self.gateway)
        self.server = LocalReturnlightServer(("127.0.0.1", 0), self.cases)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, body=None, auth=True, headers=None, method=None):
        h = dict(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
            if auth:
                h["X-Returnlight-Token"] = self.server.token
            payload = json.dumps(body).encode()
        else:
            payload = None
        req = urllib.request.Request(self.base + path, data=payload,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                return response.status, dict(response.headers), (
                    json.loads(raw) if path.startswith("/api/") else raw.decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, dict(exc.headers), json.loads(raw)

    def test_case_summary_distinguishes_recorded_attempt_without_exposing_request_id(self):
        key, _ = self.create_case()
        summary = self.cases.summary(key)
        self.assertFalse(summary["has_refund_attempt"])
        path = self.cases.path(key)
        case = json.loads(path.read_text())
        case.update(state="UNKNOWN", request_id="synthetic-attempt-reference", approved_cents=2100)
        path.write_text(json.dumps(case))
        summary = self.cases.summary(key)
        self.assertTrue(summary["has_refund_attempt"])
        self.assertEqual(summary["state"], "UNKNOWN")
        self.assertNotIn("request_id", summary)
        self.assertEqual(self.gateway.posts, [])

    def test_large_email_import_has_its_own_bounded_transport(self):
        import base64
        from email.message import EmailMessage
        message = EmailMessage()
        message["From"] = "Buyer <buyer@example.com>"
        message["Subject"] = "Replacement lid"
        body = "Please replace the broken lid, not the kettle."
        message.set_content(body)
        message.add_attachment(b"Receipt details.\n" * 20000,
                               maintype="text", subtype="plain", filename="receipt.txt")
        payload = {"email_base64": base64.b64encode(message.as_bytes()).decode()}
        self.assertGreater(len(json.dumps(payload).encode()), 384 * 1024)
        code, _, result = self.request("/api/intake/email", payload)
        self.assertEqual(code, 200)
        self.assertEqual(result["complaint"], body)
        self.assertEqual(result["attachments_omitted"], 1)
        self.assertEqual(self.cases.list_cases(), [])
        code, _, _ = self.request("/api/cases", {"email_base64": ""},
                                  headers={"Content-Length": str(len(json.dumps(payload).encode()))})
        self.assertEqual(code, 413)
        code, _, _ = self.request("/api/intake/email", {"email_base64": ""}, headers={"Content-Length": "20000000"})
        self.assertEqual(code, 413)

    def test_complete_unicode_message_fits_bounded_json_transport(self):
        body = "🌿" * 14980 + " Please hold."
        payload = {"capture_id": CAPTURE, "ceiling": "36.00", "complaint": body}
        self.assertGreater(len(json.dumps(payload).encode()), 64 * 1024)
        code, _, result = self.request("/api/cases", payload)
        self.assertEqual(code, 201)
        self.assertEqual(result["case"]["complaint"], body)
        self.assertEqual(result["case"]["approved_cents"], 0)
        payload["complaint"] = "🌿" * 15001
        code, _, _ = self.request("/api/cases", payload)
        self.assertEqual(code, 422)
        self.assertEqual(len(self.cases.list_cases()), 1)
        self.assertEqual(self.gateway.posts, [])

    def test_full_unicode_review_keeps_eight_source_conditions(self):
        clauses = ["🌿" * 999 + str(i) for i in range(8)]
        body = "".join(clauses) + "🪴" * 6987 + " Please hold."
        self.assertEqual(len(body), 15000)
        code, _, created = self.request("/api/cases", {"capture_id": CAPTURE, "ceiling": "36.00", "complaint": body})
        self.assertEqual(code, 201)
        selections = [{"message_index": 0, "source_start": i * 1000, "quote": quote} for i, quote in enumerate(clauses)]
        payload = {"quote": body, "confirmed": True, "case_revision": len(created["case"]["history"]), "selected_conditions": selections}
        self.assertGreater(len(json.dumps(payload).encode()), 256 * 1024)
        code, _, reviewed = self.request("/api/cases/" + created["key"] + "/human-review", payload)
        self.assertEqual(code, 200)
        self.assertEqual(reviewed["case"]["review"]["selected_conditions"], selections)
        self.assertEqual(reviewed["case"]["complaint"], body)
        self.assertEqual(reviewed["case"]["approved_cents"], 0)
        self.assertEqual(self.gateway.posts, [])

    def create_case(self):
        code, _, data = self.request("/api/cases",
            {"capture_id": CAPTURE, "ceiling": "42.00", "complaint": COMPLAINT})
        self.assertEqual(code, 201)
        self.assertEqual(data["case"]["state"], "OPEN")
        return data["key"], data["case"]["id"]


    def test_stale_approval_does_not_authorize_later_buyer_evidence(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.request(base + "/review", {})
        self.confirm_current(key)
        draft = self.decision_body(key, amount="21.00",
                                   confirmation="REFUND " + cid[:8] + " 21.00")
        self.assertEqual(self.request(base + "/followup", {
            "text": "The replacement handle is enough. Please refund only twelve dollars.",
            "case_revision": draft["case_revision"]})[0], 200)
        self.assertEqual(self.request(base + "/review", {})[0], 200)
        saved = self.cases.path(key).read_bytes()
        status, _, refused = self.request(base + "/approve", draft)
        self.assertEqual((status, refused["code"]), (409, "CASE_CHANGED"))
        self.assertEqual(self.cases.path(key).read_bytes(), saved)
        self.assertEqual(self.gateway.posts, [])
        self.confirm_current(key)
        current = self.decision_body(key, amount="12.00",
                                     confirmation="REFUND " + cid[:8] + " 12.00")
        status, _, approved = self.request(base + "/approve", current)
        self.assertEqual(status, 200)
        self.assertEqual(approved["case"]["approved_cents"], 1200)

    def test_stale_no_refund_closure_does_not_discard_new_buyer_problem(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        draft = self.decision_body(key, reason="Buyer accepted the replacement without a refund.",
                                   confirmation="CLOSE " + cid[:8])
        self.assertEqual(self.request(base + "/followup", {
            "text": "The replacement handle is also cracked. I am asking for a refund again.",
            "case_revision": draft["case_revision"]})[0], 200)
        saved = self.cases.path(key).read_bytes()
        status, _, refused = self.request(base + "/close", draft)
        self.assertEqual((status, refused["code"]), (409, "CASE_CHANGED"))
        self.assertEqual(self.cases.path(key).read_bytes(), saved)
        self.assertEqual(self.request(base)[2]["case"]["state"], "OPEN")
        self.assertEqual(self.gateway.posts, [])
        # A new, deliberate decision on the actual current evidence still works.
        fresh = self.decision_body(key, reason="Merchant arranged a new replacement after reading the later problem.",
                                   confirmation="CLOSE " + cid[:8])
        self.assertEqual(self.request(base + "/close", fresh)[2]["case"]["state"], "CLOSED")

    def test_old_approval_stays_stale_when_the_amount_is_unchanged(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.request(base + "/review", {})
        self.confirm_current(key)
        draft = self.decision_body(key, amount="21.00",
                                   confirmation="REFUND " + cid[:8] + " 21.00")
        approved = self.request(base + "/approve", draft)[2]["case"]
        self.assertEqual(self.request(base + "/revise", {
            "confirmation": "CHANGE " + cid[:8] + " 21.00",
            "approval_revision": approval_revision(approved)})[0], 200)
        saved = self.cases.path(key).read_bytes()
        self.assertEqual(self.request(base + "/approve", draft)[0], 409)
        self.assertEqual(self.cases.path(key).read_bytes(), saved)

    def test_decisions_require_an_explicit_integer_case_revision(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.request(base + "/review", {})
        saved = self.cases.path(key).read_bytes()
        for action, data in (
            ("approve", {"amount": "21.00", "confirmation": "REFUND " + cid[:8] + " 21.00"}),
            ("close", {"reason": "Buyer accepted a replacement.", "confirmation": "CLOSE " + cid[:8]}),
        ):
            for revision in (None, True, 0, -1, "2"):
                with self.subTest(action=action, revision=revision):
                    payload = dict(data)
                    if revision is not None:
                        payload["case_revision"] = revision
                    self.assertEqual(self.request(base + "/" + action, payload)[0], 422)
                    self.assertEqual(self.cases.path(key).read_bytes(), saved)
        self.assertEqual(self.gateway.posts, [])


    def test_provider_rejection_returns_safe_json_without_changing_case(self):
        from returnlight_app.paypal import PayPalError
        key, case_id = self.create_case()
        self.review_and_approve(key, case_id)
        base = "/api/cases/" + key
        before = self.request(base)[2]["case"]

        def rejected(*args):
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "private provider content",
                              "INVALID_RESOURCE_ID", "secret diagnostic")

        self.gateway.capture = rejected
        for path, body in ((base + "/capture", None),
                           (base + "/execute", self.send_body(key))):
            code, _, data = self.request(path, body)
            self.assertEqual(code, 502)
            self.assertEqual(data["code"], "SANDBOX_REJECTED")
            self.assertNotIn("private provider content", json.dumps(data))
            self.assertNotIn("secret diagnostic", json.dumps(data))
            self.assertEqual(self.request(base)[2]["case"], before)
            self.assertEqual(self.gateway.posts, [])

    def test_reconciliation_provider_rejection_keeps_unknown_case(self):
        from returnlight_app.paypal import PayPalError
        key, case_id = self.create_case()
        self.review_and_approve(key, case_id)
        base = "/api/cases/" + key
        self.gateway.lost = True
        self.assertEqual(self.request(base + "/execute", self.send_body(key))[0], 409)
        before = self.request(base)[2]["case"]

        def rejected(*args):
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "private provider content")

        self.gateway.refund_details = rejected
        code, _, data = self.request(base + "/refresh", {"refund_id": REFUND})
        self.assertEqual(code, 502)
        self.assertEqual(data["code"], "SANDBOX_REJECTED")
        self.assertEqual(self.request(base)[2]["case"], before)
        self.assertEqual(len(self.gateway.posts), 1)

    def send_body(self, key):
        case = self.request("/api/cases/" + key)[2]["case"]
        return {"confirmation": "SEND " + case["id"][:8] + " " + f'{case["approved_cents"]/100:.2f}',
                "approval_revision": approval_revision(case)}

    def decision_body(self, key, **values):
        case = self.request("/api/cases/" + key)[2]["case"]
        return dict(values, case_revision=len(case["history"]))

    def confirm_current(self, key):
        base = "/api/cases/" + key
        case = self.request(base)[2]["case"]
        latest = (case.get("buyer_followups") or [{"text": case["complaint"]}])[-1]["text"]
        status, _, result = self.request(base + "/human-review",
            {"quote": latest, "confirmed": True, "case_revision": len(case["history"])})
        self.assertEqual(status, 200)
        return result["case"]

    def review_and_approve(self, key, id):
        base = "/api/cases/" + key
        self.assertEqual(self.request(base + "/review", {})[2]["case"]["state"], "OPEN")
        self.confirm_current(key)
        phrase = "REFUND " + id[:8] + " 21.00"
        self.assertEqual(self.request(base + "/approve",
            self.decision_body(key, amount="21.00", confirmation=phrase))[2]["case"]["state"], "APPROVED")


    def test_download_keeps_only_explicitly_saved_unsent_reply_and_discard(self):
        key, _ = self.create_case()
        base = "/api/cases/" + key
        before = self.request(base)[2]["case"]
        words = 'Please keep the original reference.\n<buyer> & "bank" remain pending.'
        payload = {"case_revision": len(before["history"]), "draft_version": None,
                   "draft": {"decision": None, "reply": {"capture": before["capture_id"],
                             "version": json.dumps([before["capture_id"], before["state"], before["approved_cents"], before.get("refund_id"), len(before["history"])]), "text": words, "review_required": True}}}
        self.assertNotIn("Saved unsent reply", self.request(base + "/record")[2]["html"])
        code, _, result = self.request(base + "/working-draft", payload)
        self.assertEqual(code, 200)
        record = self.request(base + "/record")[2]["html"]
        self.assertIn("Saved unsent reply", record)
        self.assertIn("Needs your check before sending", record)
        self.assertIn('&lt;buyer&gt; &amp; &quot;bank&quot; remain pending.', record)
        self.assertNotIn("<buyer>", record)
        self.assertLess(record.index("Saved unsent reply"), record.index("Buyer conversation"))
        self.assertEqual(self.request(base)[2]["case"], before)
        payload["draft_version"] = result["working_draft"]["version"]
        payload["draft"]["reply"] = None
        self.assertEqual(self.request(base + "/working-draft", payload)[0], 200)
        self.assertNotIn("Saved unsent reply", self.request(base + "/record")[2]["html"])
        self.assertEqual(self.request(base)[2]["case"], before)
        self.assertEqual(self.gateway.posts, [])

    def test_buyer_followup_http_resets_unsent_approval_and_updates_record_search(self):
        key, cid = self.create_case()
        self.review_and_approve(key, cid)
        base = "/api/cases/" + key
        before = self.request(base)[2]["case"]
        stale_send = self.send_body(key)
        words = "I can keep the mug. Please refund only the replacement violet handle."
        code, _, result = self.request(base + "/followup",
            {"text": words, "case_revision": len(before["history"])})
        self.assertEqual(code, 200)
        self.assertEqual(result["case"]["complaint"], COMPLAINT)
        self.assertEqual(result["case"]["state"], "OPEN")
        self.assertIsNone(result["case"]["review"])
        self.assertEqual(result["case"]["approved_cents"], 0)
        self.assertEqual(self.request(base + "/execute", stale_send)[0], 422)
        self.assertIn(words, self.request(base + "/record")[2]["html"])
        summary = next(c for c in self.request("/api/cases")[2]["cases"] if c["key"] == key)
        self.assertIn(words, summary["complaint"])
        self.assertEqual(summary["queue"], "decide")
        self.assertEqual(self.gateway.posts, [])

    def test_payment_correction_preserves_one_case_and_downloaded_provenance(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        before = self.request(base)[2]["case"]
        reason = "The original capture and decimal place were copied incorrectly."
        payload = {"capture_id": "CAPTURE0000000099", "ceiling": "99.50", "reason": reason,
                   "case_revision": len(before["history"])}
        code, _, result = self.request(base + "/payment", payload)
        self.assertEqual(code, 200)
        changed = result["case"]
        self.assertEqual(changed["id"], cid)
        self.assertEqual(changed["complaint"], before["complaint"])
        self.assertEqual(changed["ceiling_cents"], 9950)
        self.assertEqual(changed["state"], "OPEN")
        self.assertEqual(changed["history"][:-1], before["history"])
        self.assertEqual(self.request(base)[2]["case"], changed)
        record = self.request(base + "/record")[2]["html"]
        for value in (CAPTURE, "CAPTURE0000000099", "USD 42.00", "USD 99.50", reason):
            self.assertIn(value, record)
        self.assertEqual(len(self.request("/api/cases")[2]["cases"]), 1)
        self.assertEqual(self.request(base + "/payment", dict(payload, ceiling="49.50"))[0], 409)
        self.assertEqual(self.request(base)[2]["case"], changed)
        self.assertEqual(self.gateway.posts, [])

    def test_payment_correction_retires_approval_and_rejects_stale_approval_and_send(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.request(base + "/review", {})
        reviewed = self.confirm_current(key)
        approval = {"amount": "21.00", "confirmation": "REFUND " + cid[:8] + " 21.00",
                    "case_revision": len(reviewed["history"])}
        self.assertEqual(self.request(base + "/approve", approval)[0], 200)
        old_send = self.send_body(key)
        before = self.request(base)[2]["case"]
        code, _, response = self.request(base + "/payment", {
            "capture_id": "CAPTURE0000000098", "ceiling": "38.50",
            "reason": "The capture reference belongs to the buyer's other purchase.",
            "case_revision": len(before["history"])})
        self.assertEqual(code, 200)
        changed = response["case"]
        self.assertEqual(changed["state"], "REVIEWED")
        self.assertEqual(changed["review"], reviewed["review"])
        self.assertEqual(changed["approved_cents"], 0)
        self.assertEqual(self.request(base + "/execute", old_send)[0], 422)
        self.assertEqual(self.request(base + "/approve", approval)[0], 409)
        missing_revision = {k: v for k, v in approval.items() if k != "case_revision"}
        self.assertEqual(self.request(base + "/approve", missing_revision)[0], 422)
        self.assertEqual(self.request(base + "/approve",
            dict(approval, case_revision=len(changed["history"])))[0], 200)
        self.assertEqual(self.request(base + "/execute", old_send)[0], 422)
        self.assertEqual(self.gateway.posts, [])

    def test_buyer_followup_then_new_review_cannot_revive_old_tab_approval(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.request(base + "/review", {})
        reviewed = self.request(base)[2]["case"]
        stale = {"amount": "42.00", "confirmation": "REFUND " + cid[:8] + " 42.00",
                 "case_revision": len(reviewed["history"])}
        followup = "I will keep the mug. Please refund only 19.50 for its replacement handle."
        self.assertEqual(self.request(base + "/followup", {
            "text": followup, "case_revision": len(reviewed["history"])})[0], 200)
        self.cases.reviewer_factory = lambda: type("FollowupEvidenceFixture", (), {
            "review": lambda self, words: {"category": "damaged",
                "summary": "Buyer now wants to keep the mug and requests 19.50 for the handle.",
                "quote": "Please refund only 19.50 for its replacement handle."}})()
        fresh = self.request(base + "/review", {})[2]["case"]
        self.assertEqual(self.request(base + "/approve", stale)[0], 409)
        self.assertEqual(self.request(base)[2]["case"], fresh)
        fresh = self.confirm_current(key)
        self.assertEqual(self.request(base + "/approve", {
            "amount": "19.50", "confirmation": "REFUND " + cid[:8] + " 19.50",
            "case_revision": len(fresh["history"])})[0], 200)
        self.assertEqual(self.gateway.posts, [])

    def test_download_record_reads_current_case_without_changing_it(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        before = self.request(base)[2]["case"]
        code, _, record = self.request(base + "/record")
        self.assertEqual(code, 200)
        self.assertIn(COMPLAINT, record["html"])
        self.assertIn("No current refund amount approved", record["html"])
        self.assertEqual(record["filename"], "returnlight-case-" + cid[:8] + ".html")
        self.assertEqual(self.request(base)[2]["case"], before)
        self.assertEqual(self.gateway.posts, [])
        self.request(base + "/close", self.decision_body(key, reason="Replacement accepted; no refund requested.",
                                      confirmation="CLOSE " + cid[:8]))
        closed = self.request(base + "/record")[2]["html"]
        self.assertIn("Closed · no refund", closed)
        self.assertIn("Replacement accepted; no refund requested.", closed)
        self.assertIn("Private case record", closed)
        self.assertIn("Replacement accepted; no refund requested.", closed.split("</header>")[0])

    def test_download_record_distinguishes_unverified_refund_reference(self):
        key, _ = self.create_case()
        case = self.cases.detail(key)["case"]
        case.update(state="UNKNOWN", approved_cents=1875,
                    refund_candidate_id="CANDIDATE77889900")
        self.cases.path(key).write_text(json.dumps(case))
        before = self.cases.path(key).read_bytes()
        record = self.request("/api/cases/" + key + "/record")[2]["html"]
        self.assertIn("Unverified refund reference", record)
        self.assertIn("CANDIDATE77889900", record)
        self.assertIn("<dt>Refund ID</dt><dd>None recorded</dd>", record)
        self.assertIn("Payment outcome unknown", record)
        self.assertIn("Approved amount", record.split("</header>")[0])
        self.assertNotIn("<h1>Refund completed</h1>", record)
        self.assertEqual(self.cases.path(key).read_bytes(), before)
        case.pop("refund_candidate_id")
        case.update(state="COMPLETED", refund_id="VERIFIED77889900")
        self.cases.path(key).write_text(json.dumps(case))
        completed = self.request("/api/cases/" + key + "/record")[2]["html"]
        self.assertNotIn("Unverified refund reference", completed)
        self.assertIn("VERIFIED77889900", completed)

    def test_download_record_preserves_literal_buyer_text_and_hides_legacy_advice(self):
        from returnlight_app.returnlight_record import render_case_record
        key, cid = self.create_case()
        self.review_and_approve(key, cid)
        case = self.request("/api/cases/" + key)[2]["case"]
        case["complaint"] = 'The label says <script>alert("buyer")</script> & arrived cracked.'
        case["review"]["reason"] = "UNSUPPORTED_LEGACY_REMEDY"
        record = render_case_record(case, "2026-11-10T18:00:00+00:00")
        self.assertNotIn("<script>", record)
        self.assertIn("&lt;script&gt;", record)
        self.assertNotIn("UNSUPPORTED_LEGACY_REMEDY", record)
        self.assertNotIn("recommended_percent", record)
        self.assertIn("Approved · not sent", record)
        self.assertIn("USD 21.00", record)
        self.assertNotIn("https://", record)
        case["state"] = "UNKNOWN"
        self.assertIn("Payment outcome unknown", render_case_record(case))
        self.assertNotIn("<h1>Refund completed</h1>", render_case_record(case))

    def test_case_finding_keeps_full_buyer_words_and_failed_refunds_out_of_finished(self):
        original = ("My parcel contains the wrong glaze, although the packing slip names the right mug. "
                    "I checked the carton and photographed the item before contacting you. "
                    "The identifying detail is an ultraviolet swallow on the handle.")
        code, _, created = self.request("/api/cases",
            {"capture_id": CAPTURE, "ceiling": "42.00", "complaint": original})
        self.assertEqual(code, 201)
        key, cid = created["key"], created["case"]["id"]
        summary = self.request("/api/cases")[2]["cases"][0]
        self.assertEqual(summary["complaint"], original)
        self.assertNotIn("ultraviolet", summary["complaint_preview"])
        self.assertEqual(summary["queue"], "decide")
        # A real state-machine transition through a synthetic provider failure:
        # the buyer outcome is unresolved even though this refund cannot be resent.
        self.cases.reviewer_factory = lambda: type("Review", (), {
            "review": lambda self, words: {"category": "wrong_item",
                "summary": "Wrong glaze reported", "quote": "wrong glaze"}})()
        self.review_and_approve(key, cid)
        self.gateway.refund = lambda capture_id, cents, request_id: {
            "id": REFUND, "status": "FAILED",
            "amount": {"currency_code": "USD", "value": "21.00"}}
        code, _, failed = self.request("/api/cases/" + key + "/execute", self.send_body(key))
        self.assertEqual(code, 200)
        self.assertEqual(failed["case"]["state"], "FAILED")
        self.assertEqual(self.request("/api/cases")[2]["cases"][0]["queue"], "followup")
        # An explicit human no-refund closure is finished, without any provider call.
        new_key, new_id = self.create_case()
        self.request("/api/cases/" + new_key + "/close", self.decision_body(new_key,
            reason="Buyer accepted a replacement and withdrew the refund request.",
            confirmation="CLOSE " + new_id[:8]))
        summaries = {c["key"]: c for c in self.request("/api/cases")[2]["cases"]}
        self.assertEqual(summaries[new_key]["queue"], "finished")
        self.assertEqual(summaries[key]["queue"], "followup")

    def test_revise_unsent_preserves_evidence_and_requires_fresh_approval(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.review_and_approve(key, cid)
        original = self.request(base)[2]["case"]
        old_send = self.send_body(key)
        body = {"confirmation": "CHANGE " + cid[:8] + " 21.00",
                "approval_revision": approval_revision(original)}
        self.assertEqual(self.request(base + "/revise", dict(body, confirmation="CHANGE wrong"))[0], 422)
        self.assertEqual(self.request(base)[2]["case"], original)
        code, _, response = self.request(base + "/revise", body)
        self.assertEqual(code, 200)
        changed = response["case"]
        self.assertEqual(changed["state"], "REVIEWED")
        self.assertEqual(changed["approved_cents"], 0)
        self.assertEqual(changed["review"], original["review"])
        self.assertEqual(changed["complaint"], original["complaint"])
        self.assertEqual(changed["history"][:-1], original["history"])
        self.assertEqual(self.request(base)[2]["case"], changed)
        self.assertEqual(self.request(base + "/execute", old_send)[0], 422)
        # Even reapproving the SAME amount is a new decision that old tabs cannot send or withdraw.
        self.assertEqual(self.request(base + "/approve",
            self.decision_body(key, amount="21.00", confirmation="REFUND " + cid[:8] + " 21.00"))[0], 200)

        self.assertEqual(self.request(base + "/execute", old_send)[0], 422)
        self.assertEqual(self.request(base + "/revise", body)[0], 422)
        self.assertEqual(self.gateway.posts, [])
        self.assertEqual(self.request(base + "/execute", self.send_body(key))[0], 200)
        final = self.request(base)[2]["case"]
        self.assertEqual(self.request(base + "/revise", body)[0], 422)
        self.assertEqual(self.request(base)[2]["case"], final)

    def test_revised_amount_rejects_stale_send_and_unknown_cannot_be_changed(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.review_and_approve(key, cid)
        old_send = self.send_body(key)
        self.assertEqual(self.request(base + "/revise",
            {"confirmation":"CHANGE " + cid[:8] + " 21.00", "approval_revision":old_send["approval_revision"]})[0], 200)
        self.assertEqual(self.request(base + "/approve",
            self.decision_body(key, amount="12.00", confirmation="REFUND " + cid[:8] + " 12.00"))[0], 200)

        current_send = self.send_body(key)
        self.assertEqual(self.request(base + "/execute", old_send)[0], 422)
        self.assertEqual(self.request(base + "/execute", dict(current_send, confirmation=old_send["confirmation"]))[0], 422)
        self.assertEqual(self.gateway.posts, [])
        self.gateway.lost = True
        self.assertEqual(self.request(base + "/execute", current_send)[0], 409)
        unknown = self.request(base)[2]["case"]
        self.assertEqual(self.request(base + "/revise",
            {"confirmation":"CHANGE " + cid[:8] + " 12.00", "approval_revision":current_send["approval_revision"]})[0], 422)
        self.assertEqual(self.request(base)[2]["case"], unknown)
        self.assertEqual(len(self.gateway.posts), 1)

    def test_close_without_model_or_payment_survives_reload(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        reason = "Buyer found the mug and withdrew the refund request."
        code, _, data = self.request(base + "/close",
            self.decision_body(key, reason=reason, confirmation="CLOSE " + cid[:8]))
        self.assertEqual(code, 200)
        self.assertEqual(data["case"]["state"], "CLOSED")
        self.assertEqual(data["case"]["resolution"]["reason"], reason)
        self.assertIsNone(data["case"]["review"])
        self.assertEqual(self.request(base)[2]["case"], data["case"])
        for action, body in [("review", {}), ("approve", self.decision_body(key, amount="21.00", confirmation="REFUND " + cid[:8] + " 21.00")),
                             ("execute", {"confirmation": "SEND " + cid[:8]})]:
            self.assertEqual(self.request(base + "/" + action, body)[0], 422)
        self.assertEqual(self.gateway.posts, [])

    def test_close_cancels_unsent_approval_but_never_unknown_refund(self):
        key, cid = self.create_case()
        base = "/api/cases/" + key
        self.review_and_approve(key, cid)
        body = self.decision_body(key, reason="Buyer accepted a replacement instead.", confirmation="CLOSE " + cid[:8])
        self.assertEqual(self.request(base + "/close", dict(body, confirmation="CLOSE wrongcase"))[0], 422)
        self.assertEqual(self.request(base + "/close", dict(body, reason=""))[0], 422)
        self.assertEqual(self.request(base)[2]["case"]["state"], "APPROVED")
        data = self.request(base + "/close", body)[2]["case"]
        self.assertEqual(data["approved_cents"], 0)
        self.assertTrue(any(h["event"] == "merchant.approved" for h in data["history"]))
        key2, cid2 = self.create_case()
        base2 = "/api/cases/" + key2
        self.review_and_approve(key2, cid2)
        self.gateway.lost = True
        self.assertEqual(self.request(base2 + "/execute", self.send_body(key2))[0], 409)
        self.assertEqual(self.request(base2 + "/close", self.decision_body(key2, reason=body["reason"], confirmation="CLOSE " + cid2[:8]))[0], 422)
        self.assertEqual(self.request(base2)[2]["case"]["state"], "UNKNOWN")
        self.assertEqual(len(self.gateway.posts), 1)

    def test_model_connection_failure_identifies_model_and_preserves_open_case(self):
        class OfflineModel:
            def review(self, complaint):
                raise ConnectionRefusedError("fixture local model offline")
        self.cases.reviewer_factory = OfflineModel
        key, _ = self.create_case()
        status, _, result = self.request("/api/cases/" + key + "/review", {})
        self.assertEqual(status, 502)
        self.assertEqual(result["code"], "MODEL_UNAVAILABLE")
        self.assertIn("Check the local model status", result["error"])
        self.assertNotIn("Sandbox", result["error"])
        saved = self.request("/api/cases/" + key)[2]["case"]
        self.assertEqual(saved["state"], "OPEN")
        self.assertIsNone(saved["review"])
        self.assertEqual(self.gateway.posts, [])

    def test_full_real_http_journey_and_reload(self):
        code, headers, html = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("<title>Returnlight", html)
        self.assertIn(self.server.token, html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("no-store", headers["Cache-Control"])
        # Explicit provider navigation is allowed; no external resource is loaded.
        activity = 'href="https://www.sandbox.paypal.com/myaccount/activities/"'
        self.assertIn(activity, html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn("https://", html.replace(activity, 'href=""'))
        key, id = self.create_case()
        base = "/api/cases/" + key
        self.assertEqual(self.request(base + "/execute",
            self.send_body(key))[0], 422)
        self.assertEqual(len(self.gateway.posts), 0)
        self.review_and_approve(key, id)
        self.assertEqual(self.request(base + "/approve",
            self.decision_body(key, amount="1.00", confirmation="yes"))[0], 422)

        self.assertEqual(self.request(base + "/execute",
            {"confirmation": "send " + id[:8]})[0], 422)
        self.assertEqual(len(self.gateway.posts), 0)
        code, _, data = self.request(base + "/execute",
            self.send_body(key))
        self.assertEqual(code, 200)
        self.assertEqual(data["case"]["state"], "COMPLETED")
        self.assertEqual(data["case"]["approved_cents"], 2100)
        self.assertEqual(len(self.gateway.posts), 1)
        self.assertEqual(self.request(base + "/execute",
            self.send_body(key))[0], 422)
        self.assertEqual(len(self.gateway.posts), 1)
        status, _, reload = self.request(base)
        self.assertEqual(status, 200)
        self.assertEqual(reload["case"]["state"], "COMPLETED")
        self.assertEqual(len(reload["case"]["history"]), 7)
        self.assertEqual(self.request("/api/cases")[2]["cases"][0]["state"], "COMPLETED")
        self.assertEqual(os.stat(self.cases.directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.cases.path(key)).st_mode & 0o777, 0o600)

    def test_provider_capture_inspection_is_read_only_and_not_balance(self):
        key, case_id = self.create_case()
        path = self.cases.path(key)
        original = path.read_bytes()
        before = len(self.gateway.posts)
        code, headers, data = self.request("/api/cases/" + key + "/capture")
        self.assertEqual(code, 200)
        self.assertEqual(data["capture"], {
            "capture_id": CAPTURE, "status": "COMPLETED",
            "original_cents": 4200, "ceiling_within_original": True,
            "approved_within_original": True, "can_attempt": True,
            "remaining_balance_verified": False,
        })
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.gateway.posts), before)
        # Inspection never grants approval, changes a review, or dispatches money.
        self.assertEqual(self.request("/api/cases/" + key)[2]["case"]["state"], "OPEN")
        self.assertEqual(self.request("/api/cases/" + key + "/capture", {})[0], 405)

    def test_preflight_exposes_noncompleted_or_excess_ceiling_without_authorizing(self):
        key, _ = self.create_case()
        path = self.cases.path(key)
        original = path.read_bytes()
        self.gateway.capture = lambda capture_id: {
            "id": capture_id, "status": "PENDING",
            "amount": {"currency_code": "USD", "value": "21.00"},
        }
        status, _, data = self.request("/api/cases/" + key + "/capture")
        self.assertEqual(status, 200)
        observation = data["capture"]
        self.assertEqual(observation["status"], "PENDING")
        self.assertEqual(observation["original_cents"], 2100)
        self.assertFalse(observation["ceiling_within_original"])
        self.assertFalse(observation["can_attempt"])
        self.assertFalse(observation["remaining_balance_verified"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.gateway.posts, [])

    def test_preflight_refuses_mismatched_provider_identity(self):
        key, _ = self.create_case()
        path = self.cases.path(key)
        original = path.read_bytes()
        self.gateway.capture = lambda capture_id: {
            "id": "WRONGCAPTURE999", "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "42.00"},
        }
        status, _, data = self.request("/api/cases/" + key + "/capture")
        self.assertEqual(status, 422)
        self.assertIn("does not match", data["error"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.gateway.posts, [])

    def test_uncertain_response_is_durable_and_never_replayed(self):
        self.gateway.lost = True
        key, id = self.create_case()
        self.review_and_approve(key, id)
        url = "/api/cases/" + key
        code, _, result = self.request(url + "/execute",
            self.send_body(key))
        self.assertEqual(code, 409)
        self.assertEqual(result["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.request(url)[2]["case"]["state"], "UNKNOWN")
        self.assertEqual(self.request(url + "/execute",
            self.send_body(key))[0], 422)
        self.assertEqual(len(self.gateway.posts), 1)
        code, _, result = self.request(url + "/refresh",
            {"refund_id": REFUND, "confirm_capture_link": False})
        self.assertEqual(code, 200)
        self.assertEqual(result["case"]["state"], "COMPLETED")
        self.assertEqual(len(self.gateway.posts), 1)

    def test_wrong_origin_missing_token_and_wrong_host_are_refused(self):
        self.assertEqual(self.request("/api/cases", {"capture_id": CAPTURE},
            auth=False)[0], 403)
        self.assertEqual(self.request("/api/cases", {"capture_id": CAPTURE},
            headers={"Origin": "https://malicious.example"})[0], 403)
        self.assertEqual(self.request("/api/cases", {"capture_id": CAPTURE},
            headers={"Host": "localhost.attacker.test"})[0], 421)
        self.assertEqual(self.request("/api/cases")[2]["cases"], [])
        with self.assertRaises(ValueError):
            LocalReturnlightServer(("0.0.0.0", 0), self.cases)

    def test_bad_model_quote_fails_closed(self):
        key, _ = self.create_case()
        self.bad_model = True
        self.assertEqual(self.request("/api/cases/" + key + "/review", {})[0], 422)
        self.assertEqual(self.request("/api/cases/" + key)[2]["case"]["state"], "OPEN")
        self.assertEqual(len(self.gateway.posts), 0)

    def test_unknown_case_and_get_mutation_refused(self):
        key, _ = self.create_case()
        self.assertEqual(self.request("/api/cases/" + key + "/execute")[0], 404)
        self.assertEqual(self.request("/api/cases/" + ("b" * 32))[0], 422)
        self.assertEqual(self.request("/api/cases/../cases", {})[0], 404)

    def test_no_untrusted_case_symlink_is_loaded(self):
        secret = Path(self.temp.name) / "outside.json"
        secret.write_text('{"sensitive":"do not load"}')
        link = self.cases.directory / ("a" * 32 + ".json")
        link.symlink_to(secret)
        self.assertEqual(self.request("/api/cases")[2]["cases"], [])
        self.assertEqual(self.request("/api/cases/" + "a" * 32)[0], 422)


if __name__ == "__main__":
    unittest.main()
