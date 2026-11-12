"""Simulated demo mode (`--demo`): no PayPal call, no local model, no credentials.

Everything here is switched on only by `returnlight_web --demo`. The fake PayPal
gateway answers in-process with the same response shapes the Sandbox returns, and
the fake reviewer picks a sentence from the buyer's message after a short delay.
"""
import re
import threading
import time
from datetime import datetime, timezone

from .paypal import PayPalError

ACTIVE = False  # set by returnlight_web.main() when --demo is given

SAMPLE_CAPTURE_ID = "SIMCAPTURE68USD01"
SAMPLE_ORDER_ID = "SIMORDER68USD0001"
SAMPLE_MESSAGE = (
    "Hi, my order arrived yesterday. The two cups are lovely and I would like to keep them, but the "
    "carafe has a crack running down from the rim and it leaks when I pour. I do not need a "
    "replacement before the holidays. Could you refund the carafe, which was $40 of the $68 I paid? "
    "Thank you, Priya")
BADGE_TEXT = "Simulated demo: no real PayPal call"
_API = "https://api-m.sandbox.paypal.com"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePayPalGateway:
    """Same methods as PayPalRefundGateway, answered from memory. One capture: $68.00 USD."""

    def __init__(self):
        self._lock = threading.Lock()
        self._refunds = {}
        self._created = "2026-11-09T10:15:00Z"
        self._updated = self._created

    def oauth_token(self) -> dict:  # shape of POST /v1/oauth2/token
        return {"scope": "https://uri.paypal.com/services/payments/refund", "access_token": "SIM-ACCESS-TOKEN",
                "token_type": "Bearer", "app_id": "SIM-APP", "expires_in": 32400, "nonce": "sim"}

    def _capture_body(self) -> dict:
        refunded = sum(int(round(float(r["amount"]["value"]) * 100)) for r in self._refunds.values())
        status = "COMPLETED" if not refunded else ("REFUNDED" if refunded >= 6800 else "PARTIALLY_REFUNDED")
        return {"id": SAMPLE_CAPTURE_ID, "status": status,
                "amount": {"currency_code": "USD", "value": "68.00"}, "final_capture": True,
                "create_time": self._created, "update_time": self._updated,
                "supplementary_data": {"related_ids": {"order_id": SAMPLE_ORDER_ID}},
                "links": [{"href": f"{_API}/v2/payments/captures/{SAMPLE_CAPTURE_ID}", "rel": "self", "method": "GET"}]}

    def capture(self, capture_id: str) -> dict:
        self.oauth_token()
        if capture_id != SAMPLE_CAPTURE_ID:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID",
                              "Simulated demo: use the sample capture ID " + SAMPLE_CAPTURE_ID + ".")
        with self._lock:
            return self._capture_body()

    def order_details(self, order_id: str) -> dict:
        self.oauth_token()
        if order_id != SAMPLE_ORDER_ID:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
        with self._lock:
            cap = self._capture_body()
            payments = {"captures": [cap]}
            if self._refunds:
                payments["refunds"] = [{"id": rid, "status": "COMPLETED"} for rid in self._refunds]
            return {"id": SAMPLE_ORDER_ID, "status": "COMPLETED", "intent": "CAPTURE",
                    "purchase_units": [{"reference_id": "default", "description": "Ceramic carafe and two cups",
                                        "amount": {"currency_code": "USD", "value": "68.00"}, "payments": payments}]}

    def refund(self, capture_id: str, cents: int, request_id: str) -> dict:
        self.oauth_token()
        time.sleep(0.6)
        if capture_id != SAMPLE_CAPTURE_ID:
            raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
        value = f"{cents // 100}.{cents % 100:02d}"
        with self._lock:
            rid = "SIMREFUND" + request_id.replace("-", "")[:12].upper()
            self._updated = _now()
            body = {"id": rid, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": value},
                    "note_to_payer": "Merchant-approved Returnlight refund",
                    "seller_payable_breakdown": {"gross_amount": {"currency_code": "USD", "value": value},
                                                 "net_amount": {"currency_code": "USD", "value": value},
                                                 "total_refunded_amount": {"currency_code": "USD", "value": value}},
                    "create_time": self._updated, "update_time": self._updated,
                    "links": [{"href": f"{_API}/v2/payments/refunds/{rid}", "rel": "self", "method": "GET"},
                              {"href": f"{_API}/v2/payments/captures/{capture_id}", "rel": "up", "method": "GET"}]}
            self._refunds[rid] = body
            return body

    def refund_details(self, refund_id: str) -> dict:
        self.oauth_token()
        with self._lock:
            if refund_id not in self._refunds:
                raise PayPalError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
            return self._refunds[refund_id]


class FakeReviewer:
    """Returns the 'source_selection' shape the real model call returns, with no network call."""

    def _review(self, complaint: str, earlier_messages=None, *, before_request=None) -> dict:
        if before_request is not None:
            before_request()
        time.sleep(1.2)
        passages = [p for p in re.split(r"(?<=[.!?])\s+", complaint) if p]
        if not passages:
            raise ValueError("Empty message")
        # Fixed answer for the sample: the sentence that asks for the refund.
        index = next((i for i, p in enumerate(passages) if re.search(r"\brefund\b", p, re.I)), len(passages) - 1)
        quote, cursor = passages[index], 0
        for p in passages[:index]:
            cursor = complaint.index(p, cursor) + len(p)
        start = complaint.index(quote, cursor)
        review = {"kind": "source_selection", "quote": quote, "context": complaint, "quote_start": start}
        if len(complaint) > 900:
            review["context"], review["context_start"] = complaint[start:start + len(quote)], start
        return review

    def review(self, complaint: str) -> dict:
        return self._review(complaint)

    def review_messages(self, messages, *, before_request=None) -> dict:
        return self._review(messages[-1], before_request=before_request)

    def review_current_messages(self, messages, require_current) -> dict:
        return self.review_messages(messages, before_request=require_current)


MODEL_STATUS = {"mode": "local", "model": "simulated", "state": "available", "commands": [],
                "message": "Simulated demo: a fixed answer is used instead of a local model."}

_CSS = """
.rl-sim-badge{position:fixed;left:12px;bottom:12px;z-index:2147483000;max-width:min(340px,calc(100vw - 24px));
font:600 13px/1.4 system-ui,sans-serif;color:#1d3a2e;background:#fff7d6;border:1px solid #c9a227;
border-radius:10px;padding:8px 12px;box-shadow:0 2px 10px rgba(0,0,0,.18)}
.rl-sim-badge small{display:block;font-weight:400;margin-top:4px}
.rl-sim-badge button{font:inherit;font-weight:600;margin:6px 6px 0 0;padding:3px 9px;border-radius:6px;
border:1px solid #8a6d00;background:#fff;color:#1d3a2e;cursor:pointer}
@media (prefers-color-scheme:dark){.rl-sim-badge{background:#3a3210;color:#f5e9b8;border-color:#c9a227}
.rl-sim-badge button{background:#1f1b08;color:#f5e9b8}}
"""


def stylesheet() -> str:
    return _CSS


def badge_html(nonce: str) -> str:
    """Badge, sample-data buttons and the stylesheet link, appended before </body>."""
    import json
    data = json.dumps({"msg": SAMPLE_MESSAGE, "cap": SAMPLE_CAPTURE_ID})
    return ('<link rel="stylesheet" href="/assets/returnlight.demo.css">'
            '<div class="rl-sim-badge" id="rl-sim-badge" role="status">' + BADGE_TEXT +
            '<small>Sample data for this demo: capture ID ' + SAMPLE_CAPTURE_ID + '.</small>'
            '<button type="button" id="rl-sim-fill-msg">Fill sample message</button>'
            '<button type="button" id="rl-sim-fill-cap">Fill sample capture ID</button></div>'
            '<script nonce="' + nonce + '">(function(){var d=' + data.replace("<", "\\u003c") + ';'
            'function fill(id,v){var e=document.getElementById(id);if(!e)return;var s=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e),"value").set;s.call(e,v);'
            'e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}))}'
            'document.getElementById("rl-sim-fill-msg").onclick=function(){fill("complaint",d.msg)};'
            'document.getElementById("rl-sim-fill-cap").onclick=function(){fill("capture-id",d.cap)};})();</script>')
