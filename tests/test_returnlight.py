import json
from pathlib import Path
import pytest
from returnlight_app.returnlight import (
    CaseError, OutcomeUnknown, OllamaReviewer, PayPalRefundGateway, approval_phrase,
    approve_case, case_snapshot, execute_case, new_case, refresh_case, review_case, human_review_case, usd_cents,
)
COMPLAINT="I ordered a mug and the handle arrived cracked on delivery."
CAPTURE="CAPTURE0123456789"
class Model:
    def review(self, complaint):
        return {"category":"damaged","summary":"Broken handle","quote":"handle arrived cracked",
                "recommended_percent":50,"reason":"Buyer states physical damage"}
class Gateway:
    def __init__(self, lost=False, pending=False, wrong=False):
        self.lost,self.pending,self.wrong=lost,pending,wrong
        self.posts=[]
    def capture(self, cid):
        return {"id":cid,"status":"COMPLETED","amount":{"currency_code":"USD","value":"42.00"}}
    def refund(self, cid, cents, req):
        self.posts.append((cid,cents,req))
        if self.lost: raise TimeoutError("lost response")
        return {"id":"REFUND0123456789","status":"PENDING" if self.pending else "COMPLETED",
                "amount":{"currency_code":"USD","value":"22.00" if self.wrong else "21.00"}}
    def refund_details(self, rid):
        return {"id":rid,"status":"COMPLETED","amount":{"currency_code":"USD","value":"21.00"},
                "supplementary_data":{"related_ids":{"capture_id":CAPTURE}}}
@pytest.fixture
def case(tmp_path):
    path=tmp_path/"case.json"
    new_case(path,CAPTURE,"42.00",COMPLAINT)
    suggested = review_case(path,Model())
    assert suggested["state"] == "OPEN"
    human_review_case(path, COMPLAINT, True, expected_case_revision=len(suggested["history"]))
    return path
def approve(path):
    original=case_snapshot(path)
    return approve_case(path,"21.00",approval_phrase(original,"21.00"))
def test_review_is_not_approval_and_phrase_is_exact(case):
    assert case_snapshot(case)["state"]=="REVIEWED"
    with pytest.raises(CaseError,match="phrase"): approve_case(case,"21.00","yes")
    assert case_snapshot(case)["approved_cents"]==0
    assert approve(case)["approved_cents"]==2100
def test_completed_is_single_post(case):
    approve(case); gateway=Gateway()
    assert execute_case(case,gateway)["state"]=="COMPLETED"
    assert len(gateway.posts)==1
    with pytest.raises(CaseError,match="never retry"): execute_case(case,gateway)
    assert len(gateway.posts)==1
def test_timeout_unknown_persists_and_get_recovers(case):
    approve(case); gateway=Gateway(lost=True)
    with pytest.raises(OutcomeUnknown): execute_case(case,gateway)
    assert case_snapshot(case)["state"]=="UNKNOWN"
    with pytest.raises(CaseError): execute_case(case,gateway)
    assert refresh_case(case,gateway,refund_id="REFUND0123456789")["state"]=="COMPLETED"
    assert len(gateway.posts)==1
def test_mismatch_retains_unknown(case):
    approve(case); gateway=Gateway(wrong=True)
    with pytest.raises(OutcomeUnknown): execute_case(case,gateway)
    assert case_snapshot(case)["state"]=="UNKNOWN"
def test_pending_reconcile_get_only(case):
    approve(case); gateway=Gateway(pending=True)
    assert execute_case(case,gateway)["state"]=="PENDING"
    assert refresh_case(case,gateway)["state"]=="COMPLETED"
    assert len(gateway.posts)==1
def test_unlinked_refund_requires_manual_confirmation(case):
    approve(case); gateway=Gateway(lost=True)
    with pytest.raises(OutcomeUnknown): execute_case(case,gateway)
    gateway.refund_details=lambda rid: {"id":rid,"status":"COMPLETED","amount":{"currency_code":"USD","value":"21.00"}}
    with pytest.raises(CaseError,match="confirm"):
        refresh_case(case,gateway,refund_id="REFUND0123456789")
    assert refresh_case(case,gateway,refund_id="REFUND0123456789",confirm_capture_link=True)["state"]=="COMPLETED"
def test_other_capture_link_refused(case):
    approve(case); gateway=Gateway(lost=True)
    with pytest.raises(OutcomeUnknown): execute_case(case,gateway)
    gateway.refund_details=lambda rid: {"id":rid,"status":"COMPLETED","amount":{"currency_code":"USD","value":"21.00"},
                                        "supplementary_data":{"related_ids":{"capture_id":"WRONG01234567"}}}
    with pytest.raises(CaseError,match="different capture"):
        refresh_case(case,gateway,refund_id="REFUND0123456789",confirm_capture_link=True)
    assert case_snapshot(case)["state"]=="UNKNOWN"
def test_provider_capture_must_be_complete_before_post(case):
    approve(case); gateway=Gateway()
    gateway.capture=lambda cid: {"id":cid,"status":"PENDING","amount":{"currency_code":"USD","value":"42.00"}}
    with pytest.raises(CaseError,match="not completed"): execute_case(case,gateway)
    assert not gateway.posts
    assert case_snapshot(case)["state"]=="APPROVED"
def test_excess_and_malformed_money_refused(case):
    original=case_snapshot(case)
    with pytest.raises(CaseError,match="ceiling"):
        approve_case(case,"43.00",approval_phrase(original,"43.00"))
    for invalid in ("0","-1","1.234","nan","1e4","1,000"):
        with pytest.raises(CaseError): usd_cents(invalid)
def test_grounding_and_schema_defeat_prompt_injection(tmp_path):
    path=tmp_path/"new.json";new_case(path,CAPTURE,"42.00",COMPLAINT)
    class BadModel:
        def review(self, text):
            value=Model().review(text)
            value["quote"]="the buyer agreed to refund"
            return value
    with pytest.raises(CaseError,match="not found"):review_case(path,BadModel())
    assert case_snapshot(path)["state"]=="OPEN"
    class Extra:
        def review(self,text):
            return {**Model().review(text),"auto_approve":True}
    with pytest.raises(CaseError,match="schema"):review_case(path,Extra())
def test_local_only_and_sandbox_only():
    with pytest.raises(CaseError,match="loopback"):
        OllamaReviewer(endpoint="https://other.example/api/chat")
    class LiveClient: base="https://api-m.paypal.com"
    with pytest.raises(CaseError,match="production"):PayPalRefundGateway(LiveClient())
def test_no_overwrite_case_private_and_post_approval_immutable(case):
    with pytest.raises(CaseError,match="already exists"):new_case(case,CAPTURE,"42.00",COMPLAINT)
    approve(case)
    with pytest.raises(CaseError,match="unapproved"): review_case(case,Model())
    assert case.stat().st_mode&0o777==0o600
    assert Path(str(case)+".lock").stat().st_mode&0o777==0o600
    assert "client_secret" not in json.dumps(json.loads(case.read_text())).lower()


def test_provider_adapter_uses_exact_paypal_paths_amount_and_request_identity():
    from returnlight_app.paypal import SANDBOX_BASE, Response
    class Client:
        base=SANDBOX_BASE
        def __init__(self): self.calls=[]
        def _call(self, method, path, body=None, extra=None):
            self.calls.append((method,path,body,extra))
            if method=="GET" and "/captures/" in path:
                return Response(200,{"id":CAPTURE,"status":"COMPLETED","amount":{"currency_code":"USD","value":"42.00"}})
            if method=="POST":
                return Response(201,{"id":"REFUND0123456789","status":"COMPLETED",
                                     "amount":{"currency_code":"USD","value":"21.00"}})
            return Response(200,{"id":"REFUND0123456789","status":"COMPLETED",
                                 "amount":{"currency_code":"USD","value":"21.00"}})
    client=Client(); gateway=PayPalRefundGateway(client)
    assert gateway.capture(CAPTURE)["id"]==CAPTURE
    assert gateway.refund(CAPTURE,2100,"fixed-attempt-uuid")["id"]=="REFUND0123456789"
    assert gateway.refund_details("REFUND0123456789")["status"]=="COMPLETED"
    assert client.calls[0][:2]==("GET",f"/v2/payments/captures/{CAPTURE}")
    assert client.calls[1][:2]==("POST",f"/v2/payments/captures/{CAPTURE}/refund")
    assert client.calls[1][2]["amount"]=={"currency_code":"USD","value":"21.00"}
    assert client.calls[1][3]["PayPal-Request-Id"]=="fixed-attempt-uuid"
    assert client.calls[1][3]["Prefer"]=="return=representation"
    assert client.calls[2][:2]==("GET","/v2/payments/refunds/REFUND0123456789")


def test_ollama_actual_loopback_http_transport_with_grounded_reply():
    import http.server
    import threading
    class Handler(http.server.BaseHTTPRequestHandler):
        body=None
        def do_POST(self):
            size=int(self.headers.get("Content-Length","0"))
            Handler.body=json.loads(self.rfile.read(size))
            payload=json.dumps({"message":{"content":json.dumps({"evidence_id": 0})}}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self,*args):pass
    server=http.server.HTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},daemon=True)
    thread.start()
    try:
        reviewer=OllamaReviewer(endpoint=f"http://127.0.0.1:{server.server_port}/api/chat")
        outcome=reviewer.review(COMPLAINT)
        assert outcome["quote"] in COMPLAINT
        assert Handler.body["model"]=="qwen2.5:3b"
        assert Handler.body["stream"] is False
        assert "buyer" in Handler.body["messages"][0]["content"].lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_abrupt_process_exit_still_preserves_unknown_without_replay(case):
    approve(case)
    gateway=Gateway()
    def abrupt(*args):
        gateway.posts.append(args)
        raise SystemExit(91)
    gateway.refund=abrupt
    with pytest.raises(SystemExit):
        execute_case(case,gateway)
    assert case_snapshot(case)["state"]=="UNKNOWN"
    with pytest.raises(CaseError):
        execute_case(case,gateway)
    assert len(gateway.posts)==1


def test_concurrent_requests_cannot_double_refund_same_case(case):
    from concurrent.futures import ThreadPoolExecutor
    approve(case);gateway=Gateway()
    def call():
        try: return execute_case(case,gateway)["state"]
        except CaseError: return "REFUSED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:call(),range(2)))
    assert sorted(results)==["COMPLETED","REFUSED"]
    assert len(gateway.posts)==1


def test_distinct_case_files_cannot_double_refund_same_capture(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for path in paths:
        new_case(path, CAPTURE, "42.00", COMPLAINT)
        suggested = review_case(path, Model())
        human_review_case(path, COMPLAINT, True, expected_case_revision=len(suggested["history"]))
        approve(path)
    gateway = Gateway()

    def attempt(path):
        try:
            return execute_case(path, gateway)["state"]
        except CaseError:
            return "REFUSED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, paths))
    assert outcomes == ["COMPLETED", "REFUSED"]
    assert len(gateway.posts) == 1
    assert sorted(case_snapshot(path)["state"] for path in paths) == ["APPROVED", "COMPLETED"]


def test_separate_directories_share_capture_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RETURNLIGHT_CAPTURE_STORE", str(tmp_path / "shared-reservations"))
    paths = [tmp_path / "merchant-a" / "case.json", tmp_path / "merchant-b" / "case.json"]
    for path in paths:
        new_case(path, CAPTURE, "42.00", COMPLAINT)
        suggested = review_case(path, Model())
        human_review_case(path, COMPLAINT, True, expected_case_revision=len(suggested["history"]))
        approve(path)
    gateway = Gateway()
    assert execute_case(paths[0], gateway)["state"] == "COMPLETED"
    with pytest.raises(CaseError, match="Another case"):
        execute_case(paths[1], gateway)
    assert len(gateway.posts) == 1
    root = tmp_path / "shared-reservations"
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.iterdir())


def test_interrupted_checkpoint_after_shared_reservation_never_reposts(case, monkeypatch):
    import returnlight_app.returnlight as module
    approve(case)
    gateway = Gateway()
    original_save = module._save
    def interrupted_save(path, record):
        if path == case and record["state"] == "UNKNOWN":
            raise OSError("simulated process interruption before case checkpoint")
        return original_save(path, record)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_save", interrupted_save)
        with pytest.raises(OSError, match="interruption"):
            execute_case(case, gateway)
    assert gateway.posts == []
    assert case_snapshot(case)["state"] == "APPROVED"
    with pytest.raises(OutcomeUnknown, match="Previous capture reservation"):
        execute_case(case, gateway)
    assert case_snapshot(case)["state"] == "UNKNOWN"
    assert gateway.posts == []
    with pytest.raises(CaseError, match="never retry"):
        execute_case(case, gateway)


def test_distinct_os_processes_share_durable_capture_lock(tmp_path):
    import subprocess
    import sys
    paths = [tmp_path / "process-a.json", tmp_path / "process-b.json"]
    for path in paths:
        new_case(path, CAPTURE, "42.00", COMPLAINT)
        suggested = review_case(path, Model())
        human_review_case(path, COMPLAINT, True, expected_case_revision=len(suggested["history"]))
        approve(path)
    log = tmp_path / "actual-post-attempts.txt"
    child = r"""
import os, sys, time
from returnlight_app.returnlight import execute_case, CaseError
class Gateway:
    def capture(self, cid):
        return {"id": cid, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "42.00"}}
    def refund(self, cid, cents, request_id):
        fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b"POST\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        time.sleep(0.1)
        return {"id": "REFUND0123456789", "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "21.00"}}
try:
    execute_case(sys.argv[1], Gateway())
except CaseError:
    sys.exit(2)
"""
    processes = [subprocess.Popen([sys.executable, "-c", child, str(path), str(log)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for path in paths]
    reports = [process.communicate(timeout=10) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 2], reports
    assert log.read_text().splitlines() == ["POST"]



def test_auth_failure_before_refund_post_releases_shared_claim(case, monkeypatch):
    import io
    import urllib.request
    from returnlight_app.paypal import HttpPayPalClient

    approve(case)
    monkeypatch.setenv("RETURNLIGHT_CAPTURE_STORE", str(case.parent / "captures"))
    monkeypatch.setenv("RETURNLIGHT_ALLOW_NETWORK", "1")
    client = HttpPayPalClient("synthetic-id", "synthetic-secret")
    auth_calls = 0
    requests = []

    def auth():
        nonlocal auth_calls
        auth_calls += 1
        if auth_calls == 2:
            raise ValueError("synthetic OAuth refresh rejection")
        return "synthetic-token"

    class Reply(io.BytesIO):
        status = 200

    def receive(req, timeout):
        requests.append((req.get_method(), req.full_url))
        if req.get_method() == "GET":
            return Reply(json.dumps({
                "id": CAPTURE, "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "42.00"},
            }).encode())
        assert req.get_method() == "POST"
        return Reply(json.dumps({
            "id": "REFUND0123456789", "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "21.00"},
        }).encode())

    monkeypatch.setattr(client, "_auth", auth)
    monkeypatch.setattr(urllib.request, "urlopen", receive)
    gateway = PayPalRefundGateway(client)

    with pytest.raises(CaseError, match="was not sent"):
        execute_case(case, gateway)
    restored = case_snapshot(case)
    assert restored["state"] == "APPROVED"
    assert restored["request_id"] is None
    assert restored["approved_cents"] == 2100
    assert restored["history"][-1]["event"] == "refund.not_sent"
    assert [method for method, _ in requests] == ["GET"]
    assert list((case.parent / "captures").glob("*.json")) == []

    # Successful subsequent authentication allows one refund POST after a
    # fresh capture reservation; its identity differs from the failed attempt.
    monkeypatch.setattr(client, "_auth", lambda: "recovered-token")
    assert execute_case(case, gateway)["state"] == "COMPLETED"
    assert [method for method, _ in requests] == ["GET", "GET", "POST"]
    assert len(list((case.parent / "captures").glob("*.json"))) == 1


def test_http_refusal_after_post_still_retains_shared_claim_and_unknown(case, monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from returnlight_app.paypal import HttpPayPalClient

    approve(case)
    monkeypatch.setenv("RETURNLIGHT_CAPTURE_STORE", str(case.parent / "captures"))
    monkeypatch.setenv("RETURNLIGHT_ALLOW_NETWORK", "1")
    client = HttpPayPalClient("synthetic-id", "synthetic-secret")
    monkeypatch.setattr(client, "_auth", lambda: "synthetic-token")
    requests = []

    class Reply(io.BytesIO):
        status = 200

    def receive(req, timeout):
        requests.append(req.get_method())
        if req.get_method() == "GET":
            return Reply(json.dumps({
                "id": CAPTURE, "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "42.00"},
            }).encode())
        body = io.BytesIO(json.dumps({
            "name": "UNPROCESSABLE_ENTITY", "message": "Refund cannot be accepted",
            "details": [{"issue": "REFUND_AMOUNT_EXCEEDED", "description": "private provider detail"}],
        }).encode())
        raise urllib.error.HTTPError(req.full_url, 422, "Rejected", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", receive)
    gateway = PayPalRefundGateway(client)
    with pytest.raises(OutcomeUnknown):
        execute_case(case, gateway)
    assert requests == ["GET", "POST"]
    assert case_snapshot(case)["state"] == "UNKNOWN"
    observed = case_snapshot(case)
    assert observed["refund_provider_error"]["http_status"] == 422
    assert observed["refund_provider_error"]["issue"] == "REFUND_AMOUNT_EXCEEDED"
    assert "remaining refundable amount" in observed["refund_provider_error"]["guidance"]
    assert "private provider detail" not in json.dumps(observed)
    assert observed["history"][-1]["event"] == "refund.provider_error"
    assert len(list((case.parent / "captures").glob("*.json"))) == 1
    with pytest.raises(CaseError, match="never retry"):
        execute_case(case, gateway)
    assert requests == ["GET", "POST"]


def test_descriptive_review_preserves_human_exact_amount_decision(tmp_path):
    path = tmp_path / "evidence.json"
    complaint = "The shade is cracked. Please refund $24.95, not the whole order."
    new_case(path, CAPTURE, "89.95", complaint)
    class EvidenceModel:
        def review(self, text):
            return {"category": "damaged", "summary": "Buyer reports a cracked shade.",
                    "quote": "The shade is cracked."}
    reviewed = review_case(path, EvidenceModel())
    assert reviewed["state"] == "OPEN"
    assert "recommended_percent" not in reviewed["review"]
    assert reviewed["approved_cents"] == 0
    human_review_case(path, complaint, True, expected_case_revision=len(reviewed["history"]))
    approved = approve_case(path, "24.95", approval_phrase(reviewed, "24.95"))
    assert approved["approved_cents"] == 2495


def test_model_selects_exact_original_evidence_without_rewriting(monkeypatch):
    import io
    import urllib.request
    complaint = "The blue shirt arrived in red. Please exchange it; I do not want a refund."
    requests = []
    def respond(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": json.dumps({
            "evidence_id": 1
        })}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", respond)
    review = OllamaReviewer().review(complaint)
    assert review["quote"] == "Please exchange it; I do not want a refund."
    assert review["kind"] == "source_selection"
    assert set(review) == {"kind", "quote", "context", "quote_start"}
    assert complaint[review["quote_start"]:].startswith(review["quote"])
    passages = json.loads(requests[0]["messages"][1]["content"])["buyer_passages"]
    assert all(p["text"] in complaint for p in passages)


@pytest.mark.parametrize("bad_index", [-1, 99, True, "1"])
def test_model_cannot_select_nonexistent_evidence(monkeypatch, bad_index):
    import io
    import urllib.request
    def respond(request, timeout):
        return io.BytesIO(json.dumps({"message": {"content": json.dumps({
            "evidence_id": bad_index
        })}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(CaseError, match="invalid buyer evidence"):
        OllamaReviewer().review(COMPLAINT)


def test_future_event_times_survive_reload_without_backdating_legacy(tmp_path):
    import json
    from datetime import datetime, timezone
    from returnlight_app.returnlight import close_case
    from returnlight_app.returnlight_record import render_case_record

    path = tmp_path / "case.json"
    before = datetime.now(timezone.utc)
    created = new_case(path, "CAPTURE0123456789", "42.00",
                       "The buyer reports a cracked lid and asks for a replacement.")
    after = datetime.now(timezone.utc)
    recorded = datetime.fromisoformat(created["history"][0]["recorded_at"])
    assert before <= recorded <= after
    assert recorded.utcoffset().total_seconds() == 0
    assert case_snapshot(path)["history"] == created["history"]
    # Read a genuine old-format event with no clock data; never derive a time from file mtime.
    legacy = json.loads(path.read_text())
    legacy["history"][0].pop("recorded_at")
    path.write_text(json.dumps(legacy))
    old_event = dict(legacy["history"][0])
    closed = close_case(path, "Buyer accepted a replacement without a refund.",
                        "CLOSE " + legacy["id"][:8])
    assert closed["history"][0] == old_event
    assert "recorded_at" not in case_snapshot(path)["history"][0]
    assert datetime.fromisoformat(closed["history"][1]["recorded_at"]) >= recorded
    assert [e["seq"] for e in closed["history"]] == [1, 2]
    html = render_case_record(closed)
    assert "Time not recorded" in html
    assert "UTC" in html
    assert "Case activity" in html
    assert "does not store event timestamps" not in html


def test_inconsistent_response_retains_lookup_reference_after_reload(case):
    approve(case)
    gateway = Gateway(wrong=True)
    with pytest.raises(OutcomeUnknown, match="Inconsistent"):
        execute_case(case, gateway)
    saved = case_snapshot(case)
    assert saved["state"] == "UNKNOWN"
    assert saved["refund_id"] is None
    assert saved["refund_candidate_id"] == "REFUND0123456789"
    assert saved["history"][-1]["event"] == "refund.reference_observed"
    with pytest.raises(CaseError):
        execute_case(case, gateway)
    assert refresh_case(case, gateway)["state"] == "COMPLETED"
    saved = case_snapshot(case)
    assert saved["refund_id"] == "REFUND0123456789"
    assert "refund_candidate_id" not in saved
    assert len(gateway.posts) == 1


def test_unverified_reference_can_be_corrected_but_cannot_bypass_checks(case):
    approve(case)
    gateway = Gateway(wrong=True)
    with pytest.raises(OutcomeUnknown):
        execute_case(case, gateway)
    details = gateway.refund_details
    gateway.refund_details = lambda rid: {**details(rid), "amount": {"currency_code": "USD", "value": "22.00"}}
    with pytest.raises(CaseError, match="differs"):
        refresh_case(case, gateway)
    assert case_snapshot(case)["refund_candidate_id"] == "REFUND0123456789"
    gateway.refund_details = lambda rid: {**details(rid), "supplementary_data": {}}
    with pytest.raises(CaseError, match="confirm"):
        refresh_case(case, gateway)
    gateway.refund_details = details
    assert refresh_case(case, gateway, "CORRECTREFUND1234")["refund_id"] == "CORRECTREFUND1234"
    assert len(gateway.posts) == 1


def test_crash_after_response_reference_checkpoint_still_recovers(case, monkeypatch):
    import returnlight_app.returnlight as module
    approve(case)
    gateway = Gateway()
    validate = module._validate_refund
    def crash(*args, **kwargs):
        raise SystemExit(91)
    monkeypatch.setattr(module, "_validate_refund", crash)
    with pytest.raises(SystemExit):
        execute_case(case, gateway)
    assert case_snapshot(case)["state"] == "UNKNOWN"
    assert case_snapshot(case)["refund_candidate_id"] == "REFUND0123456789"
    monkeypatch.setattr(module, "_validate_refund", validate)
    assert refresh_case(case, gateway)["state"] == "COMPLETED"
    assert len(gateway.posts) == 1


@pytest.mark.parametrize("provider_condition", ["REFUNDED", "unavailable"])
def test_existing_reservation_recovers_without_capture_preflight(case, monkeypatch, provider_condition):
    import returnlight_app.returnlight as module
    approve(case)
    gateway = Gateway()
    save = module._save
    def interrupted_save(path, record):
        if path == case and record["state"] == "UNKNOWN":
            raise OSError("interrupted before case checkpoint")
        return save(path, record)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_save", interrupted_save)
        with pytest.raises(OSError):
            execute_case(case, gateway)
    assert case_snapshot(case)["state"] == "APPROVED"
    def changed_capture(capture_id):
        if provider_condition == "unavailable":
            raise TimeoutError("capture lookup unavailable")
        return {"id": capture_id, "status": provider_condition,
                "amount": {"currency_code": "USD", "value": "42.00"}}
    gateway.capture = changed_capture
    with pytest.raises(OutcomeUnknown, match="Previous capture reservation"):
        execute_case(case, gateway)
    recovered = case_snapshot(case)
    assert recovered["state"] == "UNKNOWN"
    assert recovered["request_id"]
    assert recovered["history"][-1]["event"] == "refund.claim_recovered"
    assert gateway.posts == []


@pytest.mark.parametrize("http_status, issue", [(422, "REFUND_AMOUNT_EXCEEDED"), (409, "PREVIOUS_REQUEST_IN_PROGRESS"), (500, "INTERNAL_SERVER_ERROR")])
def test_provider_error_is_durable_guidance_without_replay(case, http_status, issue):
    from returnlight_app.paypal import PayPalError
    approve(case)
    gateway = Gateway()
    def rejected(*args):
        gateway.posts.append(args)
        raise PayPalError(http_status, "PRIVATE_NAME", "private buyer detail", issue, "private description")
    gateway.refund = rejected
    with pytest.raises(OutcomeUnknown, match="Original attempt remains reserved"):
        execute_case(case, gateway)
    saved = case_snapshot(case)
    assert saved["state"] == "UNKNOWN"
    assert saved["refund_id"] is None
    assert saved["refund_provider_error"]["http_status"] == http_status
    assert saved["refund_provider_error"]["issue"] == (None if http_status == 500 else issue)
    assert saved["history"][-1]["event"] == "refund.provider_error"
    assert "private" not in json.dumps(saved).lower()
    with pytest.raises(CaseError):
        execute_case(case, gateway)
    assert len(gateway.posts) == 1
    recovered = refresh_case(case, gateway, "REFUND0123456789")
    assert recovered["state"] == "COMPLETED"
    assert "refund_provider_error" not in recovered
    assert len(gateway.posts) == 1


@pytest.mark.parametrize("initial_pending", [False, True])
def test_cancelled_refund_keeps_provider_status_in_history(case, initial_pending):
    approve(case)
    gateway = Gateway(pending=initial_pending)
    refund = gateway.refund
    details = gateway.refund_details
    if initial_pending:
        assert execute_case(case, gateway)["state"] == "PENDING"
        gateway.refund_details = lambda rid: {**details(rid), "status": "CANCELLED"}
        result = refresh_case(case, gateway)
    else:
        gateway.refund = lambda *args: {**refund(*args), "status": "CANCELLED"}
        result = execute_case(case, gateway)
    assert result["state"] == "FAILED"
    assert "CANCELLED" in result["history"][-1]["detail"]
    assert "FAILED" not in result["history"][-1]["detail"]
    assert case_snapshot(case)["refund_id"] == "REFUND0123456789"
    with pytest.raises(CaseError):
        execute_case(case, gateway)
    assert len(gateway.posts) == 1
