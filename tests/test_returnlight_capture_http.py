"""Intake GET observes provider facts without opening or changing a case."""
import json
import threading
import urllib.error
import urllib.request
import pytest
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer
from returnlight_app.paypal import PayPalError

CAPTURE = "INTAKECAPTURE1234"

@pytest.fixture
def intake_http(tmp_path):
    class Gateway:
        def __init__(self):
            self.reads = []
            self.failure = None
        def capture(self, capture_id):
            self.reads.append(capture_id)
            if self.failure:
                raise self.failure
            return {"id": capture_id, "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "31.25"}}
        def refund(self, *args):
            raise AssertionError("Intake must never send a refund")
    gateway = Gateway()
    cases = LocalCases(tmp_path, gateway_factory=lambda: gateway)
    server = LocalReturnlightServer(("127.0.0.1", 0), cases)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    def request(path, **kwargs):
        req = urllib.request.Request(base + path, **kwargs)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)
        with response:
            return response.status, json.load(response)
    yield request, gateway, cases, server, base
    server.shutdown()
    worker.join()
    server.server_close()

def test_capture_intake_reads_facts_without_saving_case(intake_http):
    request, gateway, cases, _, _ = intake_http
    status, payload = request("/api/captures/" + CAPTURE)
    assert status == 200
    assert payload["capture"] == {"capture_id": CAPTURE, "status": "COMPLETED",
                                 "original_cents": 3125, "remaining_balance_verified": False}
    assert gateway.reads == [CAPTURE]
    assert cases.list_cases() == []
    assert list(cases.directory.iterdir()) == []

def test_capture_intake_rejects_bad_identity_before_provider(intake_http):
    request, gateway, cases, _, _ = intake_http
    status, _ = request("/api/captures/BAD")
    assert status in (404, 422)
    assert gateway.reads == []
    assert cases.list_cases() == []

def test_capture_intake_host_guard_prevents_provider_read(intake_http):
    request, gateway, _, _, _ = intake_http
    status, _ = request("/api/captures/" + CAPTURE, headers={"Host": "untrusted.example"})
    assert status == 421
    assert gateway.reads == []

def test_capture_intake_post_cannot_create_or_send(intake_http):
    request, gateway, cases, server, base = intake_http
    status, _ = request("/api/captures/" + CAPTURE, data=b"{}", headers={
        "Origin": base, "Content-Type": "application/json", "X-Returnlight-Token": server.token})
    assert status in (404, 405)
    assert gateway.reads == []
    assert cases.list_cases() == []

def test_capture_intake_provider_timeout_is_recoverable(intake_http):
    request, gateway, cases, _, _ = intake_http
    gateway.failure = TimeoutError("private upstream diagnostic")
    status, payload = request("/api/captures/" + CAPTURE)
    assert status == 502
    assert payload["code"] == "SANDBOX_UNAVAILABLE"
    assert "private upstream diagnostic" not in json.dumps(payload)
    assert cases.list_cases() == []
