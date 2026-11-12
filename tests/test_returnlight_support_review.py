"""Authored refund histories; no payment or genuine model requests."""
import json
from pathlib import Path
import pytest
from returnlight_app.returnlight import (
    new_case, case_snapshot, add_post_payment_message, record_merchant_reply,
    resolve_post_payment_support, review_support_case, dismiss_support_review,
    CaseError, CaseChanged)
from returnlight_app.returnlight_support_review import (
    support_sources, support_content, support_basis, current_support_review,
    validate_support_review)
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_record import render_case_record

LATEST = "It is still absent. Please check that original refund and keep the Friday update. Do not send another refund."
REPLY = "I will check the original refund reference and send an update on Friday."
NOTE = "Explained the recorded pending status; bank receipt timing remains unknown."

def fixture(tmp_path, state="PENDING"):
    p=tmp_path/("b"*32+".json")
    c=new_case(p,"SUPPORTCAPTURE2122","28.50","Please refund only the damaged pouch.")
    c.update(state=state,approved_cents=2850,request_id="authored-request",refund_id="AUTHOREDREFUND")
    p.write_text(json.dumps(c))
    c=add_post_payment_message(p,"The refund is missing. Please check the original reference.",len(c["history"]))
    c=record_merchant_reply(p,REPLY,len(c["history"]),True,"d"*32)
    c=resolve_post_payment_support(p,NOTE,len(c["history"]))
    c=add_post_payment_message(p,LATEST,len(c["history"]))
    return p,c

def answer(case):
    sources=support_sources(case)
    return {"current_request":"Check the original refund and keep the Friday update; the buyer asks not to send another refund.",
            "source_quotes":[{"source_number":i+1,"quote":s["text"]} for i,s in enumerate(sources) if s["text"] in (case["post_payment_messages"][-1]["text"],REPLY)]}

class Reviewer:
    def review_support_case(self,case,*,before_request):
        before_request()
        return validate_support_review(answer(case),case)

@pytest.mark.parametrize("state",["UNKNOWN","PENDING","COMPLETED","FAILED"])
def test_reading_preserves_original_payment_and_support_work(tmp_path,state):
    path,before=fixture(tmp_path,state)
    after=review_support_case(path,Reviewer(),len(before["history"]))
    for field in ("state","approved_cents","request_id","refund_id","capture_id","review","support_followup_open","post_payment_messages","support_resolutions","merchant_replies"):
        assert after[field]==before[field]
    assert current_support_review(after)["summary"].startswith("Check the original")
    assert case_snapshot(path)==after
    record=render_case_record(after)
    assert "AI reading · current support" in record and LATEST in record and REPLY in record
    dismissed=dismiss_support_review(path,len(after["history"]))
    assert current_support_review(dismissed) is None
    assert dismissed["previous_support_review"]==after["support_review"]
    assert dismissed["state"]==state and dismissed["request_id"]==before["request_id"]

def test_every_recorded_source_is_in_chronology_and_identifiers_not_sent(tmp_path):
    path,c=fixture(tmp_path)
    sources=support_sources(c)
    assert [s["kind"] for s in sources]==["buyer","payment","buyer","merchant","handling","buyer"]
    content=support_content(c)
    for s in sources:assert s["text"] in content
    for value in (c["capture_id"],c["request_id"],c["refund_id"]):assert value not in content
    assert "PENDING" in content and "28.50" in content and "not a fresh provider check" in content

def test_same_boundary_handling_and_reply_preserve_history_order(tmp_path):
    path,c=fixture(tmp_path)
    c=resolve_post_payment_support(path,"Merchant checked the original reference; will update the buyer.",len(c["history"]))
    c=record_merchant_reply(path,"I checked the original reference and will update you tomorrow.",len(c["history"]),True,"e"*32)
    assert [s["kind"] for s in support_sources(c)][-2:]==["handling","merchant"]

def test_postrefund_reply_before_first_later_message_stays_after_payment(tmp_path):
    path,c=fixture(tmp_path)
    # This authored case isolates a reply recorded after the attempt but before
    # the first later buyer message.
    c["post_payment_messages"]=[];c["support_resolutions"]=[]
    c["merchant_replies"]=[];c["history"]=c["history"][:1];path.write_text(json.dumps(c))
    c=record_merchant_reply(path,REPLY,len(c["history"]),True,"c"*32)
    c=add_post_payment_message(path,LATEST,len(c["history"]))
    assert [s["kind"] for s in support_sources(c)]==["buyer","payment","merchant","buyer"]

def test_stale_before_request_and_late_result_are_rejected(tmp_path):
    path,c=fixture(tmp_path)
    class Never:
        def review_support_case(self,*a,**k):raise AssertionError("must not dispatch")
    add_post_payment_message(path,"New information arrived.",len(c["history"]))
    with pytest.raises(CaseChanged):review_support_case(path,Never(),len(c["history"]))
    current=case_snapshot(path)
    class Racing:
        def review_support_case(self,case,*,before_request):
            before_request()
            add_post_payment_message(path,"Another correction arrived.",len(case["history"]))
            return validate_support_review(answer(case),case)
    with pytest.raises(CaseChanged):review_support_case(path,Racing(),len(current["history"]))
    assert "support_review" not in case_snapshot(path)

def test_changed_context_hides_earlier_reading_and_export(tmp_path):
    path,c=fixture(tmp_path)
    c=review_support_case(path,Reviewer(),len(c["history"]))
    c=add_post_payment_message(path,"The refund arrived; now please send the warranty information.",len(c["history"]))
    assert current_support_review(c) is None
    assert "AI reading · current support" not in render_case_record(c)
    assert LocalCases(tmp_path).detail(path.stem)["case"].get("support_review") is None

def test_source_origin_and_unsupported_claims_require_manual_fallback(tmp_path):
    _,c=fixture(tmp_path)
    a=answer(c);a["source_quotes"][0]["source_number"]=1
    with pytest.raises(CaseError):validate_support_review(a,c)
    a=answer(c);a["source_quotes"][0]["quote"]="The refund definitely arrived."
    with pytest.raises(CaseError):validate_support_review(a,c)
    # Literal quotations cannot prove semantic correctness; a merchant can
    # discard a source-valid but mistaken interpretation through the UI.
    a=answer(c);a["current_request"]="The refund has arrived."
    assert validate_support_review(a,c)["summary"]=="The refund has arrived."

def test_endpoint_requires_support_specific_consent(tmp_path):
    path,c=fixture(tmp_path)
    class Remote(Reviewer):
        destination_id="chosen-destination"
    app=LocalCases(tmp_path,remote_reviewer=Remote())
    for payload in ({},{"remote_confirmed":True,"destination_id":"chosen-destination","case_revision":len(c["history"])}):
        with pytest.raises(CaseError):app.action(path.stem,"support-review",payload)
    assert case_snapshot(path)==c
    result=app.action(path.stem,"support-review",{"remote_confirmed":True,"destination_id":"chosen-destination","case_revision":len(c["history"]),"review_scope":"current-support-v1"})
    assert result["case"]["state"]=="PENDING"


def test_reading_cannot_drop_the_latest_buyer_source(tmp_path):
    _,c=fixture(tmp_path)
    a=answer(c);a["source_quotes"]=a["source_quotes"][:1]
    with pytest.raises(CaseError,match="latest buyer message"):
        validate_support_review(a,c)


@pytest.mark.parametrize("questions",[None,{},[0],[3],[True],[1,1],["1"]])
def test_support_question_indices_must_identify_existing_exact_sources(tmp_path,questions):
    _,c=fixture(tmp_path)
    a=answer(c);a["open_questions"]=questions
    with pytest.raises(CaseError,match="unanswered question"):
        validate_support_review(a,c)

def test_support_question_survives_saved_reading_and_record_without_payment_change(tmp_path):
    path,c=fixture(tmp_path)
    question="Do I need to contact my bank, or will your Friday update include the original refund reference?"
    c=add_post_payment_message(path,question,len(c["history"]))
    class Questions(Reviewer):
        def review_support_case(self,case,*,before_request):
            before_request()
            a=answer(case);a["open_questions"]=[2]
            return validate_support_review(a,case)
    after=review_support_case(path,Questions(),len(c["history"]))
    assert case_snapshot(path)["support_review"]["open_questions"]==[2]
    assert after["support_review"]["sources"][1]["quote"]==question
    for field in ("state","approved_cents","request_id","refund_id","capture_id","support_followup_open"):
        assert after[field]==c[field]
    record=render_case_record(after)
    assert "Questions to resolve · AI reading" in record and question in record
    assert "open_questions" in support_content(c)
    later=add_post_payment_message(path,"The bank answered that question; please send only the reference.",len(after["history"]))
    assert current_support_review(later) is None
    assert "Questions to resolve · AI reading" not in render_case_record(later)

def test_legacy_support_reading_stays_compatible_and_empty_questions_are_allowed(tmp_path):
    _,c=fixture(tmp_path)
    a=answer(c)
    assert "open_questions" not in validate_support_review(a,c)
    a["open_questions"]=[]
    assert validate_support_review(a,c)["open_questions"]==[]

@pytest.mark.parametrize("draft",[None,{},{"text":"","source_numbers":[1]},{"text":"x","source_numbers":[]},{"text":"x","source_numbers":[0]},{"text":"x","source_numbers":[True]},{"text":"x","source_numbers":[1,1]},{"text":"x","source_numbers":[99]},{"text":"x","source_numbers":["1"]},{"text":"x"*3001,"source_numbers":[1]}])
def test_support_reply_rejects_invalid_text_or_source_references(tmp_path,draft):
    _,c=fixture(tmp_path)
    a=answer(c);a["reply_draft"]=draft
    with pytest.raises(CaseError,match="reply draft"):
        validate_support_review(a,c)

def test_support_reply_is_saved_with_exact_sources_and_stales_with_new_words(tmp_path):
    path,c=fixture(tmp_path,"COMPLETED")
    class WithReply(Reviewer):
        def review_support_case(self,case,*,before_request):
            before_request()
            a=answer(case)
            a["reply_draft"]={"text":"Your Friday update remains pending; I understand you still cannot see the original refund.","source_numbers":[1,2]}
            return validate_support_review(a,case)
    after=review_support_case(path,WithReply(),len(c["history"]))
    reading=case_snapshot(path)["support_review"]
    assert reading["reply_draft"]["source_numbers"]==[1,2]
    assert LocalCases(tmp_path).detail(path.stem)["case"]["support_review"]==reading
    assert after["merchant_replies"]==c["merchant_replies"]
    assert after["state"]==c["state"] and after["refund_id"]==c["refund_id"]
    assert "reply_draft" in support_content(c)
    later=add_post_payment_message(path,"The refund arrived. Please cancel the bank question.",len(after["history"]))
    assert current_support_review(later) is None
    assert LocalCases(tmp_path).detail(path.stem)["case"].get("support_review") is None

def test_support_reply_dismissal_does_not_revive_on_reload(tmp_path):
    path,c=fixture(tmp_path)
    a=answer(c);a["reply_draft"]={"text":"Your question remains pending confirmation.","source_numbers":[2]}
    class Draft(Reviewer):
        def review_support_case(self,case,*,before_request):
            before_request();return validate_support_review(a,case)
    after=review_support_case(path,Draft(),len(c["history"]))
    dismissed=dismiss_support_review(path,len(after["history"]))
    assert current_support_review(dismissed) is None
    assert LocalCases(tmp_path).detail(path.stem)["case"].get("support_review") is None


def purchase_fixture(tmp_path):
    path, case = fixture(tmp_path, "COMPLETED")
    observation = {"observed_at": "2026-11-11T19:00:00+00:00", "capture": {
        "capture_id": case["capture_id"], "original_cents": 2850,
        "order_id": "AUTHOREDORDER001", "order": {"id": "AUTHOREDORDER001",
        "total_cents": 2850, "items": [{"name": "Blue damaged pouch", "quantity": 1,
        "unit_cents": 2850, "line_cents": 2850}]}}}
    seed = new_case(tmp_path/"purchase-seed.json", case["capture_id"], "28.50",
                    case["complaint"], purchase_observation=observation)
    case["purchase_context"] = seed["purchase_context"]
    path.write_text(json.dumps(case))
    return path, case_snapshot(path), observation


def test_support_purchase_keeps_historical_source_numbers_and_locations(tmp_path):
    from returnlight_app.returnlight_purchase_sources import purchase_sources
    _, case, _ = purchase_fixture(tmp_path)
    legacy = dict(case); legacy.pop("purchase_context")
    previous = support_sources(legacy)
    sources = support_sources(case)
    assert sources[:len(previous)] == previous
    added = sources[len(previous):]
    assert [s["text"] for s in added] == purchase_sources(case)
    assert [(s["kind"], s["purchase_number"]) for s in added] == [("purchase", 1), ("purchase", 2)]
    content = support_content(case)
    assert all(s["text"] in content for s in added)
    assert "not current refund eligibility" in content
    for identifier in (case["capture_id"], case["request_id"], case["refund_id"], "AUTHOREDORDER001"):
        assert identifier not in content


def test_support_purchase_quote_is_exact_and_cannot_be_a_buyer_question(tmp_path):
    _, case, _ = purchase_fixture(tmp_path)
    value = answer(case)
    bank = support_sources(case)
    value["source_quotes"].append({"source_number": len(bank), "quote": bank[-1]["text"]})
    value["reply_draft"] = {"text": "Our saved purchase lists the blue damaged pouch.",
                            "source_numbers": [len(value["source_quotes"])]}
    reading = validate_support_review(value, case)
    source = reading["sources"][-1]
    assert source["kind"] == "purchase" and source["purchase_number"] == 2
    assert source["quote_start"] == 0 and source["quote"] == bank[-1]["text"]
    value["open_questions"] = [len(value["source_quotes"])]
    with pytest.raises(CaseError, match="unanswered question"):
        validate_support_review(value, case)
    value["open_questions"] = []
    value["source_quotes"][-1]["quote"] = "Blue damaged pouch · USD 1.00"
    with pytest.raises(CaseError, match="exact"):
        validate_support_review(value, case)


def test_support_purchase_refresh_retires_old_reading_without_payment_change(tmp_path):
    from returnlight_app.returnlight import retain_purchase
    path, case, observation = purchase_fixture(tmp_path)
    before_payment = {k: case[k] for k in ("state", "approved_cents", "request_id", "refund_id")}
    case = review_support_case(path, Reviewer(), len(case["history"]))
    original_basis = case["support_review"]["source_basis"]
    observation["observed_at"] = "2026-11-11T20:00:00+00:00"
    observation["capture"]["order"]["items"][0]["name"] = "Corrected blue pouch"
    changed = retain_purchase(path, observation, len(case["history"]))
    assert support_basis(changed) != original_basis
    assert current_support_review(changed) is None
    assert LocalCases(tmp_path).detail(path.stem)["case"]["support_review"] is None
    assert {k: changed[k] for k in before_payment} == before_payment


def test_support_purchase_from_another_capture_is_not_sent(tmp_path):
    _, case, _ = purchase_fixture(tmp_path)
    case["purchase_context"]["capture_id"] = "ANOTHERCAPTURE123"
    assert not any(s["kind"] == "purchase" for s in support_sources(case))
    assert "Blue damaged pouch" not in support_content(case)


def test_support_purchase_requires_expanded_consent_before_dispatch(tmp_path):
    path, case, _ = purchase_fixture(tmp_path)
    class Remote(Reviewer):
        destination_id = "chosen-destination"
        calls = 0
        def review_support_case(self, case, *, before_request):
            self.calls += 1
            return super().review_support_case(case, before_request=before_request)
    reader = Remote(); app = LocalCases(tmp_path, remote_reviewer=reader)
    payload = {"remote_confirmed": True, "destination_id": reader.destination_id,
               "case_revision": len(case["history"]), "review_scope": "current-support-v1"}
    with pytest.raises(CaseError, match="saved purchase"):
        app.action(path.stem, "support-review", payload)
    assert reader.calls == 0 and case_snapshot(path) == case
    payload["review_scope"] = "current-support-purchase-v2"
    received = app.action(path.stem, "support-review", payload)["case"]
    assert reader.calls == 1 and received["state"] == "COMPLETED"


def test_support_remote_validation_uses_the_sent_purchase_snapshot(tmp_path):
    from returnlight_app.returnlight_remote_review import RemoteReviewer
    _, case, _ = purchase_fixture(tmp_path)
    bank = support_sources(case); expected_basis = support_basis(case)
    response = answer(case)
    response["source_quotes"].append({"source_number": len(bank), "quote": bank[-1]["text"]})
    class Capture(RemoteReviewer):
        def review_conversation(self, messages, *, _content, _validator, **kwargs):
            assert bank[-1]["text"] in _content
            case["purchase_context"]["order"]["items"][0]["name"] = "Changed after construction"
            return _validator(response)
    reading = Capture("http://127.0.0.1:12345/v1/chat/completions", "controlled",
                      api_key="authored-fixture").review_support_case(case)
    assert reading["source_basis"] == expected_basis
    assert reading["sources"][-1]["quote"] == bank[-1]["text"]

# The same later buyer words need the saved order's actual item context.
from test_returnlight_remote_review import provider, reviewer
from test_returnlight_purchase_reading import make as purchase_case
from returnlight_app.returnlight_purchase_sources import purchase_sources


def purchase_support_case(tmp_path, variant="bowl"):
    path, case = purchase_case(tmp_path, variant, key=("a" if variant == "bowl" else "b") * 32)
    case.update(state="COMPLETED", approved_cents=500,
                request_id="authored-support-request", refund_id="AUTHOREDREFUND")
    path.write_text(json.dumps(case))
    case = add_post_payment_message(path,
        "What was the original price of the blue item? I am not asking for another refund.",
        len(case["history"]))
    return path, case


def purchase_support_answer(case, number=2):
    bank = support_sources(case)
    latest = next(i + 1 for i, source in enumerate(bank) if source.get("support_number") == 1)
    purchase = next(i + 1 for i, source in enumerate(bank) if source.get("purchase_number") == number)
    return {"current_request": "The buyer asks the saved original price and does not request another refund.",
            "source_quotes": [{"source_number": latest, "quote": bank[latest - 1]["text"]},
                              {"source_number": purchase, "quote": bank[purchase - 1]["text"]}],
            "open_questions": [], "reply_draft": {"text": "I will check the original item price.",
                                                   "source_numbers": [1, 2]}}


def test_support_purchase_requires_its_scope_and_sends_distinct_original_items(tmp_path, provider):
    model = reviewer(provider)
    app = LocalCases(tmp_path, remote_reviewer=model)
    for variant, number in [("bowl", 2), ("towel", 3)]:
        path, before = purchase_support_case(tmp_path, variant)
        original = path.read_bytes()
        count = len(provider["calls"])
        payload = {"remote_confirmed": True, "destination_id": model.destination_id,
                   "case_revision": len(before["history"]), "review_scope": "current-support-v1"}
        with pytest.raises(CaseError, match="saved purchase"):
            app.action(path.stem, "support-review", payload)
        assert path.read_bytes() == original and len(provider["calls"]) == count
        provider["answer"] = purchase_support_answer(before, number)
        app.action(path.stem, "support-review", {**payload, "review_scope": "current-support-purchase-v2"})
        saved = case_snapshot(path)
        for field in ("state", "approved_cents", "request_id", "refund_id", "purchase_context"):
            assert saved[field] == before[field]
        content = provider["calls"][-1]["messages"][0]["content"]
        assert all(words in content for words in purchase_sources(before))
        assert all(value not in content for value in ("AUTHOREDPURCHASE01", "AUTHOREDORDER001", "PRIVATE-SKU"))
        assert saved["support_review"]["sources"][1]["purchase_number"] == number
        record = render_case_record(saved)
        assert 'id="ai-source-1"' in record and "<mark" in record
    assert provider["calls"][0]["messages"][0]["content"] != provider["calls"][1]["messages"][0]["content"]


@pytest.mark.parametrize("change", ["wrong-number", "invented-price", "purchase-question"])
def test_support_purchase_cannot_supply_another_source_or_buyer_question(tmp_path, change):
    _, case = purchase_support_case(tmp_path)
    value = purchase_support_answer(case)
    if change == "wrong-number":
        value["source_quotes"][1]["source_number"] += 1
    elif change == "invented-price":
        value["source_quotes"][1]["quote"] = "USD 99.00"
    else:
        value["open_questions"] = [2]
    with pytest.raises(CaseError):
        validate_support_review(value, case)


def test_changed_purchase_retires_support_reading_and_exact_export(tmp_path):
    _, case = purchase_support_case(tmp_path)
    old_sequence = support_sources(case)
    case["support_review"] = validate_support_review(purchase_support_answer(case), case)
    assert current_support_review(case)
    case["purchase_context"]["order"]["items"][0]["name"] = "Natural ceramic bowl"
    assert current_support_review(case) is None
    assert "AI reading · current support" not in render_case_record(case)
    assert support_sources(case)[:3] == old_sequence[:3]


def test_purchase_changed_during_support_review_rejects_the_late_answer(tmp_path):
    path, before = purchase_support_case(tmp_path)
    class RacingPurchase:
        def review_support_case(self, case, *, before_request):
            before_request()
            changed = json.loads(path.read_text())
            changed["purchase_context"]["order"]["items"][0]["name"] = "Natural ceramic bowl"
            path.write_text(json.dumps(changed))
            return validate_support_review(purchase_support_answer(case), case)
    with pytest.raises(CaseChanged):
        review_support_case(path, RacingPurchase(), len(before["history"]))
    after = case_snapshot(path)
    assert not after.get("support_review")
    assert after["purchase_context"]["order"]["items"][0]["name"] == "Natural ceramic bowl"
    assert after["state"] == before["state"] and after["approved_cents"] == before["approved_cents"]


def test_support_purchase_bank_stays_bound_to_the_existing_capture(tmp_path):
    _, case = purchase_support_case(tmp_path)
    original_sequence = support_sources(case)
    case["purchase_context"]["capture_id"] = "OTHER_CAPTURE01"
    assert support_sources(case) == original_sequence[:3]


@pytest.mark.parametrize("payment_state", ["PENDING", "COMPLETED"])
def test_changed_support_stops_obsolete_wait_without_changing_payment(tmp_path, payment_state):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from returnlight_app.returnlight import ReviewInProgress
    from returnlight_app.returnlight_remote_review import RemoteReviewer
    path, case = fixture(tmp_path, payment_state)
    payment = {k: case[k] for k in ("state", "capture_id", "approved_cents", "request_id", "refund_id")}
    started, release = threading.Event(), threading.Event()
    state = {"calls": 0, "answer": answer(case)}
    class Held(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"] += 1
            first = state["calls"] == 1
            value = state["answer"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            if first:
                started.set()
                release.wait(5)
            event = {"choices": [{"delta": {"content": json.dumps(value)}, "finish_reason": "stop"}]}
            try:
                self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Held)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    reader = RemoteReviewer(f"http://127.0.0.1:{server.server_port}/chat",
                            "controlled", api_key="fixture-only", timeout=4)
    errors = []
    def run():
        try:
            review_support_case(path, reader, len(case["history"]))
        except Exception as exc:
            errors.append(exc)
    pending = threading.Thread(target=run)
    try:
        pending.start()
        assert started.wait(1)
        with pytest.raises(ReviewInProgress):
            review_support_case(path, reader, len(case["history"]))
        assert pending.is_alive() and state["calls"] == 1
        current = add_post_payment_message(path, "Leave the refund unchanged; please send the original receipt.",
                                           len(case["history"]))
        saved = path.read_bytes()
        pending.join(1.5)
        assert not pending.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CaseChanged)
        assert path.read_bytes() == saved and not release.is_set()
        state["answer"] = answer(current)
        after = review_support_case(path, reader, len(current["history"]))
        assert state["calls"] == 2
        assert {k: after[k] for k in payment} == payment
        assert current_support_review(after)["sources"][-1]["quote"] == current["post_payment_messages"][-1]["text"]
        final = path.read_bytes()
        release.set()
        assert path.read_bytes() == final
    finally:
        release.set()
        pending.join(5)
        server.shutdown()
        server.server_close()
        serving.join()


def test_existing_review_slot_observation_does_not_create_or_change_case(tmp_path):
    from returnlight_app.returnlight import _review_slot, review_in_progress
    path, before = fixture(tmp_path)
    original = path.read_bytes()
    lock = Path(str(path) + ".review.lock")
    assert not lock.exists()
    assert review_in_progress(path) is False
    assert not lock.exists()
    desk = LocalCases(tmp_path)
    with _review_slot(path):
        assert review_in_progress(path) is True
        assert desk.detail(path.stem)["review_in_progress"] is True
        assert path.read_bytes() == original
    assert review_in_progress(path) is False
    assert desk.detail(path.stem)["review_in_progress"] is False
    assert path.read_bytes() == original


def test_pending_observation_releases_when_original_process_stops(tmp_path):
    import subprocess
    import sys
    from returnlight_app.returnlight import review_in_progress
    path, _ = fixture(tmp_path)
    original = path.read_bytes()
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time;from pathlib import Path;from returnlight_app.returnlight import _review_slot;"
         "\nwith _review_slot(Path(sys.argv[1])):\n print('held',flush=True);time.sleep(20)",
         str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        assert review_in_progress(path) is True
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert review_in_progress(path) is False
    assert path.read_bytes() == original
