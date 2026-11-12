"""Returns desk: start a return from a photo of the item and a photo of the receipt.

A local vision model (Ollama, loopback only) reads the receipt and describes the item; this
module matches the order line, checks the return window and proposes a refund. The refund
itself still runs through Returnlight's own case state machine (review, approve, execute) and
the same PayPal gateway, and only after a deliberate human confirmation. Nothing here moves
money by itself, and nothing here starts agents or other models.
"""
from __future__ import annotations

import base64
import difflib
import json
import re
import secrets
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import demo_mode
from .returnlight import (CaseError, OutcomeUnknown, approval_phrase, approval_revision, approve_case, case_snapshot,
                          dollars, execute_case, human_review_case, inspect_capture, new_case, order_capture_choices,
                          usd_cents)
from .returnlight_photos import MAX_PHOTO_BYTES, photo_format

OLLAMA = "http://127.0.0.1:11434/api/chat"
VISION_MODEL = "qwen2.5vl:3b"
WINDOW_DAYS = 30
_ASSETS = Path(__file__).with_name("returns_assets")
_ID = re.compile(r"^[a-f0-9]{16}$")
CONDITIONS = {"unopened": "Unopened, in its packaging", "opened_unused": "Opened but unused",
              "used": "Used", "damaged": "Damaged"}
REASONS = {"wrong_size": "Wrong size", "defective": "Arrived faulty", "not_as_described": "Not as described",
           "changed_mind": "Changed their mind", "": "No reason given"}


def _today() -> date:
    return datetime.now(timezone.utc).astimezone().date()


# ---------------------------------------------------------------- reading the pictures

def _ollama_json(prompt: str, image: bytes, timeout: float = 120) -> dict:
    body = {"model": VISION_MODEL, "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}]}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # loopback only
        text = json.loads(resp.read())["message"]["content"]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _norm_box(box, width, height):
    try:
        x1, y1, x2, y2 = [float(v) for v in box][:4]
    except (TypeError, ValueError):
        return None
    if max(x1, y1, x2, y2) > 1.5:
        x1, x2, y1, y2 = x1 / width, x2 / width, y1 / height, y2 / height
    x1, x2 = sorted((min(max(x1, 0), 1), min(max(x2, 0), 1)))
    y1, y2 = sorted((min(max(y1, 0), 1), min(max(y2, 0), 1)))
    if x2 - x1 < 0.02 or y2 - y1 < 0.02:
        return None
    return [round(x1, 4), round(y1, 4), round(x2 - x1, 4), round(y2 - y1, 4)]


def _cents(value) -> int | None:
    try:
        return usd_cents(f"{float(str(value).replace('$', '').replace(',', '').strip()):.2f}")
    except (ValueError, CaseError):
        return None


def read_item(image: bytes) -> dict:
    _, w, h = photo_format(image)
    raw = _ollama_json(
        "Look at this photo of an item a customer wants to return. Reply as JSON: "
        '{"item": short product name, "condition": one of "unopened","opened_unused","used","damaged", '
        '"summary": one plain sentence about what you see, '
        '"marks": [{"label": 2-4 words, "kind": "damage" or "note", "box": [x1,y1,x2,y2] in pixels}]}. '
        "Only mark things that are really visible: cracks, stains, tears, missing parts, open packaging, tags.", image)
    cond = raw.get("condition") if raw.get("condition") in CONDITIONS else "opened_unused"
    marks = []
    for m in (raw.get("marks") or [])[:6]:
        if isinstance(m, dict) and (box := _norm_box(m.get("box"), w, h)):
            marks.append({"label": str(m.get("label", ""))[:40] or "Noticed", "kind": "damage" if m.get("kind") == "damage" else "note", "box": box})
    return {"item": str(raw.get("item", "Item"))[:80], "condition": cond, "summary": str(raw.get("summary", ""))[:240], "marks": marks}


def read_receipt(image: bytes) -> dict:
    _, w, h = photo_format(image)
    raw = _ollama_json(
        "Read this receipt or order confirmation. Reply as JSON: "
        '{"order_id": string or null, "date": "YYYY-MM-DD" or null, "buyer": name or null, '
        '"lines": [{"name": string, "qty": number, "price": unit price number, "box": [x1,y1,x2,y2] in pixels around the line}]}.', image)
    lines = []
    for ln in (raw.get("lines") or [])[:20]:
        if not isinstance(ln, dict):
            continue
        price = _cents(ln.get("price"))
        if price is None or not ln.get("name"):
            continue
        qty = ln.get("qty") if isinstance(ln.get("qty"), int) and 1 <= ln["qty"] <= 99 else 1
        lines.append({"name": str(ln["name"])[:80], "qty": qty, "cents": price, "box": _norm_box(ln.get("box"), w, h)})
    d = raw.get("date") if isinstance(raw.get("date"), str) and re.fullmatch(r"\d{4}-\d\d-\d\d", raw["date"]) else None
    oid = raw.get("order_id") if isinstance(raw.get("order_id"), str) and re.fullmatch(r"[A-Za-z0-9]{8,40}", raw["order_id"]) else None
    return {"order_id": oid, "date": d, "buyer": (str(raw["buyer"])[:60] if raw.get("buyer") else None), "lines": lines}


def canned_reading() -> tuple[dict, dict]:
    """Fixed answers for the bundled sample photos (and for the simulated demo)."""
    bought = (_today() - timedelta(days=3)).isoformat()
    item = {"item": "Glazed carafe", "condition": "damaged",
            "summary": "A crack runs down from the rim of the carafe; the glaze is chipped beside it.",
            "marks": [{"label": "Crack from rim", "kind": "damage", "box": [0.49, 0.17, 0.10, 0.32]},
                      {"label": "Chipped glaze", "kind": "damage", "box": [0.54, 0.175, 0.09, 0.06]}]}
    receipt = {"order_id": demo_mode.SAMPLE_ORDER_ID, "date": bought, "buyer": "Priya",
               "lines": [{"name": "Glazed carafe", "qty": 1, "cents": 4000, "box": [0.06, 0.40, 0.88, 0.10]},
                         {"name": "Stoneware cup (set of 2)", "qty": 1, "cents": 2800, "box": [0.06, 0.52, 0.88, 0.10]}]}
    return item, receipt


# ---------------------------------------------------------------- the decision

def match_line(item_name: str, lines: list) -> tuple[int | None, float]:
    best, score = None, 0.0
    toks = set(re.findall(r"[a-z]+", item_name.lower()))
    for i, ln in enumerate(lines):
        lt = set(re.findall(r"[a-z]+", ln["name"].lower()))
        s = max(len(toks & lt) / max(1, len(toks | lt)), difflib.SequenceMatcher(None, item_name.lower(), ln["name"].lower()).ratio() * 0.8)
        if s > score:
            best, score = i, s
    return best, score


def evaluate(r: dict) -> dict:
    """Recompute window, amount and the mark from the facts held on the return."""
    facts = r["facts"]
    line = facts["lines"][facts["line"]] if facts.get("line") is not None and facts["lines"] else None
    window = None
    if facts.get("bought"):
        days = (_today() - date.fromisoformat(facts["bought"])).days
        window = {"days": days, "limit": WINDOW_DAYS, "inside": days <= WINDOW_DAYS}
    cond, reason = facts["condition"], facts.get("reason", "")
    full = line["cents"] * line["qty"] if line else 0
    cents = facts["override_cents"] if facts.get("override_cents") is not None else full
    notes, ok = [], True
    if not line and facts.get("override_cents") is None:
        ok = False; notes.append("I could not find this item on the receipt.")
    if window and not window["inside"]:
        ok = False; notes.append(f"Bought {window['days']} days ago, outside the {WINDOW_DAYS} day window.")
    if cond == "used":
        ok = False; notes.append("The item looks used.")
    if cond == "damaged" and reason != "defective" and not facts.get("not_buyer_fault"):
        ok = False; notes.append("Damaged. Was it faulty on arrival, or damaged after?")
    if cents > full and line:
        cents = full
    if r["parcel"] != "received":
        status = "waiting"
    else:
        status = "decision"
    if (r.get("refund") or {}).get("state") == "COMPLETED":
        status = "refunded"
    return {"line": line, "window": window, "cents": cents, "clear": ok, "notes": notes, "status": status}


_NUM = r"\$?\s*(\d+(?:\.\d{1,2})?)"


def apply_correction(r: dict, text: str) -> None:
    """Deterministic reading of a spoken or typed correction."""
    t = " " + text.lower().strip() + " "
    f = r["facts"]
    neg = re.search(r"\bnot (?:damaged|broken|faulty|defective|cracked)|\bno damage|\bundamaged|\bisn'?t (?:damaged|broken)", t)
    if re.search(r"\b(?:unopened|sealed)\b|never opened|still in (?:the )?(?:box|packag)", t):
        f["condition"] = "unopened"
    elif re.search(r"\bunused\b|\bnot used\b|\bnever used\b|haven'?t used|hasn'?t been used", t) or (
            re.search(r"\bopened\b", t) and not re.search(r"\bused\b", t)):
        f["condition"] = "opened_unused"
    elif re.search(r"\b(?:used|worn|washed)\b", t):
        f["condition"] = "used"
    if neg:
        f["marks_hidden"] = True
        if f["condition"] == "damaged":
            f["condition"] = "opened_unused"
        if f.get("reason") == "defective":
            f["reason"] = ""
        f["not_buyer_fault"] = False
    elif re.search(r"\b(?:damaged|broken|cracked|torn|chipped|faulty|defective|leaks?|leaked)\b", t):
        f["condition"] = "damaged"
        f["marks_hidden"] = False
        if re.search(r"arriv|came|on delivery|in the post|out of the box|faulty|defective", t):
            f["reason"] = "defective"; f["not_buyer_fault"] = True
    if re.search(r"wrong size|too (?:small|big|large|tight|loose)|doesn'?t fit|didn'?t fit", t):
        f["reason"] = "wrong_size"; f["not_buyer_fault"] = False
    elif re.search(r"not as described|looks different|different from", t):
        f["reason"] = "not_as_described"
    elif re.search(r"changed (?:my|their|her|his) mind|no longer (?:want|need)|don'?t want", t):
        f["reason"] = "changed_mind"
    if re.search(r"dropped|their fault|customer'?s fault", t):
        f["not_buyer_fault"] = False; f["reason"] = ""
    if re.search(r"\bgoodwill\b|\bmake an exception\b|\bfine to refund\b", t):
        f["not_buyer_fault"] = True
    if m := re.search(r"(?:refund|give back|only|just|make it)\s*" + _NUM, t):
        f["override_cents"] = _cents(m.group(1))
    if re.search(r"\bfull refund\b|\bfull amount\b", t):
        f["override_cents"] = None
    r["said"].append(text.strip()[:300])


def transcribe(b64) -> str:
    """Local speech to text on loopback; the audio never leaves this machine."""
    try:
        data = base64.b64decode(b64 or "", validate=True)
        if not 100 < len(data) < 8_000_000:
            raise ValueError
        req = urllib.request.Request("http://127.0.0.1:5621/listen", data=data, headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            lines = json.loads(resp.read()).get("lines", [])
        text = " ".join(str(l.get("text", "")) for l in lines).strip()
    except Exception:
        raise CaseError("Could not hear that. Type the correction instead.")
    if len(text) < 2:
        raise CaseError("Could not hear that. Type the correction instead.")
    return text[:300]


# ---------------------------------------------------------------- the store

class ReturnsDesk:
    def __init__(self, cases):
        self.cases = cases
        self.dir = Path(cases.directory) / "returns"
        self.dir.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        if demo_mode.ACTIVE and not any(self.dir.glob("*.json")):
            self._seed()

    # -- files
    def _p(self, rid):
        if not _ID.fullmatch(str(rid)):
            raise CaseError("Unknown return.")
        return self.dir / (rid + ".json")

    def load(self, rid) -> dict:
        try:
            return json.loads(self._p(rid).read_text())
        except (OSError, ValueError):
            raise CaseError("Unknown return.")

    def save(self, r):
        with self.lock:
            tmp = self.dir / (r["id"] + ".tmp")
            tmp.write_text(json.dumps(r))
            tmp.replace(self._p(r["id"]))

    def image(self, rid, kind) -> tuple[bytes, str]:
        if kind not in ("item", "receipt"):
            raise CaseError("Unknown picture.")
        r = self.load(rid)
        name = r["pictures"].get(kind)
        if name and name.startswith("sample-"):
            data = (_ASSETS / name).read_bytes()
            return data, ("image/png" if name.endswith(".png") else "image/jpeg")
        path = self.dir / f"{rid}-{kind}.img"
        data = path.read_bytes()
        return data, photo_format(data)[0]

    # -- views
    def view(self, r: dict) -> dict:
        ev = evaluate(r) if r["phase"] == "ready" else None
        out = {k: r[k] for k in ("id", "created", "phase", "parcel", "source", "pictures", "error", "said", "refund", "buyer", "analysis")}
        out["facts"] = r["facts"]
        out["eval"] = ev
        out["status"] = ev["status"] if ev else "reading"
        out["conditions"] = CONDITIONS
        out["reasons"] = REASONS
        out["simulated"] = demo_mode.ACTIVE
        return out

    def listing(self) -> list:
        rows = []
        for p in self.dir.glob("*.json"):
            try:
                r = json.loads(p.read_text())
                v = self.view(r)
            except (OSError, ValueError, KeyError):
                continue
            ev = v["eval"] or {}
            line = ev.get("line")
            rows.append({"id": r["id"], "created": r["created"], "status": v["status"], "buyer": r.get("buyer") or "Customer",
                         "item": r["facts"].get("item"), "cents": ev.get("cents", 0), "phase": r["phase"],
                         "refund_id": (r.get("refund") or {}).get("refund_id"), "has_photo": bool(r["pictures"].get("item")), "simulated": r.get("seeded", False)})
        rows.sort(key=lambda x: x["created"], reverse=True)
        return rows

    # -- creating and reading
    def start(self, payload: dict) -> dict:
        item = self._decode(payload.get("item"), "the item")
        receipt = self._decode(payload.get("receipt"), "the receipt")
        sample = payload.get("sample") is True
        rid = secrets.token_hex(8)
        r = {"id": rid, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "phase": "reading",
             "parcel": "received" if payload.get("in_hand") is not False else "expected",
             "source": "shop" if payload.get("in_hand") is not False else "customer",
             "pictures": {}, "facts": {}, "said": [], "refund": None, "error": None, "buyer": None, "analysis": None}
        if sample:
            r["pictures"] = {"item": "sample-item.png", "receipt": "sample-receipt.png"}
            img_i = img_r = None
        else:
            for kind, data in (("item", item), ("receipt", receipt)):
                (self.dir / f"{rid}-{kind}.img").write_bytes(data)
                r["pictures"][kind] = kind
            img_i, img_r = item, receipt
        self.save(r)
        threading.Thread(target=self._read, args=(rid, img_i, img_r, sample), daemon=True).start()
        return {"id": rid}

    @staticmethod
    def _decode(value, what):
        if value is None:
            return None
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise CaseError(f"Could not read the picture of {what}.")
        if len(data) > MAX_PHOTO_BYTES or photo_format(data) is None or photo_format(data)[0] == "application/pdf":
            raise CaseError(f"Use a JPEG or PNG picture of {what}.")
        return data

    def _read(self, rid, img_i, img_r, sample):
        r = self.load(rid)
        analysis = "model"
        try:
            if sample or demo_mode.ACTIVE:
                import time
                time.sleep(1.2)
                item, receipt = canned_reading()
                analysis = "sample"
            else:
                if not img_i or not img_r:
                    raise CaseError("Add both pictures.")
                item, receipt = read_item(img_i), read_receipt(img_r)
        except Exception:
            item = {"item": "Item", "condition": "opened_unused", "summary": "", "marks": []}
            receipt = {"order_id": None, "date": None, "buyer": None, "lines": []}
            analysis = "typed"
        idx, score = match_line(item["item"], receipt["lines"])
        if idx is None and len(receipt["lines"]) == 1:
            idx, score = 0, 0.5
        r["buyer"] = receipt.get("buyer")
        r["analysis"] = analysis
        r["facts"] = {"item": item["item"], "condition": item["condition"], "summary": item["summary"], "marks": item["marks"],
                      "lines": receipt["lines"], "line": idx if (idx is not None and score >= 0.25) else None,
                      "match_score": round(score, 2), "bought": receipt["date"], "order_id": receipt["order_id"],
                      "reason": "defective" if item["condition"] == "damaged" else "", "not_buyer_fault": item["condition"] == "damaged" and analysis == "sample",
                      "capture_id": None, "override_cents": None}
        self._link_payment(r)
        r["phase"] = "ready"
        self.save(r)

    def _link_payment(self, r, capture_id=None):
        f = r["facts"]
        f["payment_note"] = ""
        try:
            gw = self.cases.gateway_factory()
            cid = capture_id
            if not cid and f.get("order_id"):
                choices = order_capture_choices(f["order_id"], gw)["captures"]
                cid = choices[0]["capture_id"] if len(choices) == 1 else None
            if cid:
                info = inspect_capture(cid, gw)
                f["capture_id"] = cid
                f["paid_cents"] = info["original_cents"]
                f["bought"] = f.get("bought") or (info.get("capture_created_at") or "")[:10] or None
            else:
                f["payment_note"] = "Enter the PayPal capture ID for this order."
        except Exception as exc:  # no credentials, offline, wrong ID: say so, never guess
            f["capture_id"] = None
            f["payment_note"] = "PayPal could not confirm this payment" + (": " + str(exc)[:120] if isinstance(exc, CaseError) else ". Enter the capture ID to try again.")

    # -- actions
    def post(self, path: str, payload: dict) -> dict:
        if path == "/api/returns":
            return self.start(payload)
        m = re.fullmatch(r"/api/returns/([a-f0-9]{16})/(say|hear|parcel|payment|refund|setline)", path)
        if not m:
            raise CaseError("Not found.")
        rid, act = m.groups()
        r = self.load(rid)
        if r["phase"] != "ready":
            raise CaseError("Still reading the pictures.")
        if r.get("refund") and act != "say":
            raise CaseError("This return is already refunded.")
        if act == "hear":
            heard = transcribe(payload.get("audio"))
            apply_correction(r, heard)
            self.save(r)
            return {"return": self.view(r), "heard": heard}
        if act == "say":
            text = payload.get("text")
            if not isinstance(text, str) or not 2 <= len(text.strip()) <= 300:
                raise CaseError("Say or type a short correction.")
            apply_correction(r, text)
        elif act == "parcel":
            r["parcel"] = "received"
        elif act == "setline":
            i = payload.get("line")
            if not isinstance(i, int) or not 0 <= i < len(r["facts"]["lines"]):
                raise CaseError("Pick a line from the receipt.")
            r["facts"]["line"] = i
            r["facts"]["item"] = r["facts"]["lines"][i]["name"]
        elif act == "payment":
            self._link_payment(r, str(payload.get("capture_id") or "").strip() or None)
        elif act == "refund":
            self._refund(r, payload)
        self.save(r)
        return {"return": self.view(r)}

    def _refund(self, r, payload):
        ev = evaluate(r)
        f = r["facts"]
        if r["parcel"] != "received":
            raise CaseError("Wait for the parcel before refunding.")
        if payload.get("held") is not True or payload.get("cents") != ev["cents"]:
            raise CaseError("Confirm the exact amount shown on the card.")
        if not f.get("capture_id") or ev["cents"] <= 0:
            raise CaseError("Link the PayPal payment first.")
        if not ev["clear"] and payload.get("override") is not True:
            raise CaseError("This return needs your decision: confirm the refund anyway.")
        cents = ev["cents"]
        key = secrets.token_hex(16)
        path = self.cases.path(key)
        who = r.get("buyer") or "The customer"
        text = (f"Return desk: {who} returns {f['item']} ({CONDITIONS[f['condition']].lower()}; {REASONS.get(f.get('reason',''), '').lower()}). "
                f"Photo of the item and the receipt were checked. Refund {dollars(cents)}.")
        info = inspect_capture(f["capture_id"], self.cases.gateway_factory())
        new_case(path, f["capture_id"], dollars(info["original_cents"]), text)
        snap = case_snapshot(path)
        human_review_case(path, snap["complaint"], True, expected_case_revision=len(snap["history"]))
        snap = case_snapshot(path)
        approve_case(path, dollars(cents), approval_phrase(snap, dollars(cents)), expected_case_revision=len(snap["history"]))
        snap = case_snapshot(path)
        r["refund"] = {"case": key, "cents": cents, "state": "SENDING"}
        self.save(r)
        try:
            done = execute_case(path, None, gateway_factory=self.cases.gateway_factory,
                                expected_approval_revision=approval_revision(snap),
                                send_confirmation="SEND " + snap["id"][:8] + " " + dollars(cents))
        except OutcomeUnknown:
            r["refund"]["state"] = "UNKNOWN"
            self.save(r)
            raise
        except Exception:
            r["refund"] = None
            raise
        r["refund"] = {"case": key, "cents": cents, "state": done["state"], "refund_id": done.get("refund_id")}

    # -- demo content
    def _seed(self):
        now = datetime.now(timezone.utc)
        def mk(days_ago, buyer, item, cents, parcel, refunded, cond, bought_days):
            rid = secrets.token_hex(8)
            f = {"item": item, "condition": cond, "summary": "", "marks": [], "lines": [{"name": item, "qty": 1, "cents": cents, "box": None}],
                 "line": 0, "bought": (_today() - timedelta(days=bought_days)).isoformat(), "order_id": None, "reason": "wrong_size",
                 "not_buyer_fault": False, "capture_id": "SIMCAPTURESEED00", "override_cents": None, "paid_cents": cents, "payment_note": ""}
            r = {"id": rid, "created": (now - timedelta(days=days_ago)).isoformat(timespec="seconds"), "phase": "ready", "parcel": parcel,
                 "source": "customer", "pictures": {}, "facts": f, "said": [], "error": None, "buyer": buyer, "analysis": "seed", "seeded": True,
                 "refund": {"case": None, "cents": cents, "state": "COMPLETED", "refund_id": "SIMREFUNDSEED01"} if refunded else None}
            self.save(r)
        mk(5, "Marcus", "Tea strainer", 1800, "received", True, "unopened", 8)
        mk(1, "Elena", "Linen apron", 3400, "expected", False, "opened_unused", 6)


def desk_for(app) -> ReturnsDesk:
    if not hasattr(app, "_returns_desk"):
        app._returns_desk = ReturnsDesk(app)
    return app._returns_desk


# ---------------------------------------------------------------- HTTP glue

def handles(path: str) -> bool:
    return path == "/returns" or path.startswith("/api/returns") or path.startswith("/assets/returns")


def serve_get(handler, path: str, token: str):
    import html as _html
    from urllib.parse import urlsplit
    desk = desk_for(handler.app)
    try:
        if path == "/returns":
            nonce = secrets.token_urlsafe(18)
            page = Path(__file__).with_name("returns_ui.html").read_text(encoding="utf-8")
            page = page.replace("__TOKEN__", _html.escape(token, quote=True)).replace("__NONCE__", nonce)
            page = page.replace("__SIM__", "1" if demo_mode.ACTIVE else "0")
            handler._reply(200, page, "text/html; charset=utf-8", nonce=nonce)
        elif path == "/assets/returns.css":
            handler._reply(200, Path(__file__).with_name("returns_ui.css").read_text(encoding="utf-8"), "text/css; charset=utf-8")
        elif path == "/assets/returns.js":
            handler._reply(200, Path(__file__).with_name("returns_ui.js").read_text(encoding="utf-8"), "text/javascript; charset=utf-8")
        elif path == "/api/returns":
            handler._reply(200, {"ok": True, "returns": desk.listing(), "window_days": WINDOW_DAYS})
        elif m := re.fullmatch(r"/api/returns/([a-f0-9]{16})", path):
            handler._reply(200, {"ok": True, "return": desk.view(desk.load(m.group(1)))})
        elif m := re.fullmatch(r"/api/returns/([a-f0-9]{16})/(item|receipt)\.img", path):
            data, mime = desk.image(m.group(1), m.group(2))
            handler._reply(200, data, mime)
        elif m := re.fullmatch(r"/assets/returns/(sample-item|sample-receipt)\.png", path):
            handler._reply(200, (_ASSETS / (m.group(1) + ".png")).read_bytes(), "image/png")
        else:
            handler._error(404, "Not found.")
    except (CaseError, OSError, TypeError):
        handler._error(404, "Not found.")
