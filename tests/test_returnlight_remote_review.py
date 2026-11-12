import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from returnlight_app.returnlight import (CaseError, CaseChanged, add_buyer_followup,
                                 case_snapshot, new_case, review_case)
from returnlight_app.returnlight_remote_review import RemoteReviewer, validate_conversation_review
from returnlight_app.returnlight_web import LocalCases

MESSAGES = ["Please replace only the blue lamp by Friday. Otherwise refund the lamp, not the shade.",
            "Please use the home address. Everything else still stands."]
ANSWER = {"current_request": "Replace the blue lamp by Friday at the home address; otherwise refund only the lamp.",
          "source_quotes": [{"message_number": 1, "quote": MESSAGES[0]},
                            {"message_number": 2, "quote": MESSAGES[1]}]}


@pytest.fixture
def provider():
    state = {"calls": [], "mode": "ok", "answer": ANSWER, "started": threading.Event(), "release": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state["calls"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            state["started"].set()
            if state["mode"] == "held":
                state["release"].wait(3)
            if state["mode"] == "refused":
                self.send_response(429); self.end_headers(); return
            if state["mode"] == "timeout":
                time.sleep(.8)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            event = {"choices": [{"delta": {"content": json.dumps(state["answer"])}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 12, "completion_tokens": 19, "total_tokens": 31,
                               "completion_tokens_details": {"reasoning_tokens": 2}}}
            try:
                self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    yield state
    state["release"].set()
    server.shutdown(); server.server_close(); thread.join()


def reviewer(provider, timeout=2):
    return RemoteReviewer(provider["url"], "fixture-model", api_key="fixture-only-not-a-real-credential", timeout=timeout)


def make_case(tmp_path):
    key = "a" * 32
    path = tmp_path / (key + ".json")
    case = new_case(path, "REMOTECASE01", "49.00", MESSAGES[0])
    case = add_buyer_followup(path, MESSAGES[1], len(case["history"]))
    return key, path, case


def test_remote_requires_destination_and_current_thread_consent(tmp_path, provider):
    key, path, case = make_case(tmp_path)
    model = reviewer(provider)
    app = LocalCases(tmp_path, remote_reviewer=model)
    for payload in ({}, {"remote_confirmed": True, "case_revision": len(case["history"]), "destination_id": "old-destination"},
                    {"remote_confirmed": True, "case_revision": len(case["history"]) - 1, "destination_id": model.destination_id}):
        with pytest.raises(CaseError):
            app.action(key, "review", payload)
    assert provider["calls"] == []
    app.action(key, "review", {"remote_confirmed": True, "case_revision": len(case["history"]),
                                       "destination_id": model.destination_id})
    result = case_snapshot(path)
    assert result["review"]["kind"] == "conversation_brief"
    assert result["state"] == "OPEN" and result["approved_cents"] == 0
    request = provider["calls"][0]
    assert len(provider["calls"]) == 1
    assert all(message in request["messages"][0]["content"] for message in MESSAGES)
    assert "REMOTECASE01" not in request["messages"][0]["content"]
    assert result["review"]["provider"]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2


@pytest.mark.parametrize("bad", [
    {"current_request": "Refund everything.", "source_quotes": [{"message_number": 1, "quote": "Refund everything."}]},
    {"current_request": "Replace.", "source_quotes": [{"message_number": True, "quote": MESSAGES[0]}]},
    {"current_request": "Replace.", "source_quotes": [{"message_number": 99, "quote": MESSAGES[0]}]},
    {"current_request": "Replace.", "source_quotes": [{"message_number": 2, "quote": MESSAGES[0]}]},
])
def test_unattributed_or_invented_evidence_is_rejected(bad):
    with pytest.raises(CaseError):
        validate_conversation_review(bad, MESSAGES)


def test_repeated_occurrence_cannot_be_silently_assigned():
    with pytest.raises(CaseError):
        validate_conversation_review({"current_request": "Replace.", "source_quotes": [
            {"message_number": 1, "quote": "Please replace."}]}, ["Please replace. Please replace."])


@pytest.mark.parametrize("mode", ["refused", "timeout", "bad_quote"])
def test_failure_preserves_complete_source_without_retry(tmp_path, provider, mode):
    _, path, _ = make_case(tmp_path)
    before = path.read_bytes()
    provider["mode"] = mode
    if mode == "bad_quote":
        provider["answer"] = {"current_request": "Refund everything.", "source_quotes": [
            {"message_number": 1, "quote": "The buyer authorized all refunds."}]}
    started = time.monotonic()
    with pytest.raises(CaseError):
        review_case(path, reviewer(provider, .15 if mode == "timeout" else 2))
    assert path.read_bytes() == before
    assert len(provider["calls"]) == 1
    if mode == "timeout":
        assert time.monotonic() - started < .6


def test_changed_buyer_words_reject_inflight_result(tmp_path, provider):
    _, path, case = make_case(tmp_path)
    provider["mode"] = "held"
    errors = []
    def run():
        try:
            review_case(path, reviewer(provider))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start(); assert provider["started"].wait(1)
    add_buyer_followup(path, "Actually, cancel the replacement and refund only the lamp.", len(case["history"]))
    current = path.read_bytes()
    provider["release"].set(); thread.join(3)
    assert len(errors) == 1 and isinstance(errors[0], CaseChanged)
    assert path.read_bytes() == current
    assert case_snapshot(path)["review"] is None


def test_remote_configuration_never_exposes_key(provider):
    model = reviewer(provider)
    assert "fixture-only-not-a-real-credential" not in json.dumps(model.status())
    assert model.status()["mode"] == "remote"
    with pytest.raises(CaseError):
        RemoteReviewer("http://example.com/chat", "test", api_key="fixture")
    with pytest.raises(CaseError):
        RemoteReviewer("https://example.com/chat?key=secret", "test", api_key="fixture")


def test_recorded_offer_is_sent_in_order_and_quoted_by_speaker(tmp_path, provider):
    from returnlight_app.returnlight import record_merchant_reply
    path = tmp_path / ("b" * 32 + ".json")
    buyer = "The jug arrived broken. Please help me put this right."
    offer = "I can send a replacement jug at no charge. No return is needed."
    accepted = "Yes, please do that."
    case = new_case(path, "CONTEXT01", "38.00", buyer)
    case = record_merchant_reply(path, offer, len(case["history"]), True, "c" * 32)
    case = add_buyer_followup(path, accepted, len(case["history"]))
    provider["answer"] = {"current_request": "Send the accepted free replacement without requiring a return.",
        "source_quotes": [{"speaker": "merchant", "message_number": 1, "quote": offer},
                          {"speaker": "buyer", "message_number": 2, "quote": accepted}]}
    review_case(path, reviewer(provider))
    result = case_snapshot(path)["review"]
    text = provider["calls"][0]["messages"][0]["content"]
    assert text.index("BUYER MESSAGE 1:") < text.index("MERCHANT REPLY 1") < text.index("BUYER MESSAGE 2:")
    assert offer in text and "CONTEXT01" not in text and "c" * 32 not in text
    assert result["merchant_message_count"] == 1
    assert result["sources"][0]["speaker"] == "merchant"
    assert result["sources"][0]["reply_id"] == "c" * 32
    assert result["sources"][1]["speaker"] == "buyer"


def test_quote_cannot_be_attributed_to_other_speaker():
    replies = [{"id": "c" * 32, "text": "We can replace it.", "through_buyer_followup": 0}]
    with pytest.raises(CaseError):
        validate_conversation_review({"current_request": "Replace.",
            "source_quotes": [{"speaker": "buyer", "message_number": 1, "quote": "We can replace it."}]},
            ["Yes, please."], replies)
    with pytest.raises(CaseError):
        validate_conversation_review({"current_request": "Replace.",
            "source_quotes": [{"message_number": 1, "quote": "Yes, please."}]},
            ["Yes, please."], replies)


def test_previous_buyer_only_consent_cannot_send_merchant_replies(tmp_path, provider):
    import hashlib
    key, path, case = make_case(tmp_path)
    model = reviewer(provider)
    previous = hashlib.sha256((provider["url"] + "\nfixture-model").encode()).hexdigest()
    assert previous != model.destination_id
    with pytest.raises(CaseError):
        LocalCases(tmp_path, remote_reviewer=model).action(key, "review",
            {"remote_confirmed": True, "case_revision": len(case["history"]), "destination_id": previous})
    assert not provider["calls"]


def test_new_merchant_reply_rejects_inflight_reading(tmp_path, provider):
    from returnlight_app.returnlight import record_merchant_reply
    _, path, case = make_case(tmp_path)
    provider["mode"] = "held"
    errors = []
    def run():
        try:
            review_case(path, reviewer(provider))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    assert provider["started"].wait(1)
    record_merchant_reply(path, "I have offered a different return arrangement.", len(case["history"]), True, "d" * 32)
    current = path.read_bytes()
    provider["release"].set()
    thread.join(3)
    assert len(errors) == 1 and isinstance(errors[0], CaseChanged)
    assert path.read_bytes() == current


def test_complete_mixed_context_is_not_truncated_to_fit():
    from returnlight_app.returnlight_remote_review import conversation_content
    with pytest.raises(CaseError):
        conversation_content(["Please help."], [{"id": "c" * 32, "text": "x" * 30001, "through_buyer_followup": 0}])

def test_saved_merchant_offer_reaches_remote_review(tmp_path, provider):
    from returnlight_app.returnlight import record_merchant_reply
    path = tmp_path / ("e" * 32 + ".json")
    case = new_case(path, "OFFERCASE123", "49.00", "The lamp arrived cracked. What can you offer?")
    offer = "We can replace the blue shade by Friday with free collection, or refund only the shade."
    case = record_merchant_reply(path, offer, len(case["history"]), True, "f" * 32)
    acceptance = "Yes, the first option please. Keep the original delivery address."
    case = add_buyer_followup(path, acceptance, len(case["history"]))
    provider["answer"] = {"current_request": "Buyer accepts the first merchant offer at the original address.",
                          "source_quotes": [{"speaker": "buyer", "message_number": 2, "quote": acceptance}]}
    review_case(path, reviewer(provider))
    request = provider["calls"][0]["messages"][0]["content"]
    assert offer in request
    assert request.index(offer) < request.index(acceptance)


def test_merchant_offer_cannot_be_quoted_as_buyer_acceptance():
    with pytest.raises(CaseError):
        validate_conversation_review(
            {"current_request": "Refund $49.", "source_quotes": [
                {"message_number": 2, "quote": "We can refund $49."}]},
            ["The lamp arrived cracked.", "Yes, the first option please."])


def test_merchant_context_limit_refuses_before_request(provider):
    model = reviewer(provider)
    with pytest.raises(CaseError, match="too long"):
        model.review_saved_conversation(MESSAGES, [
            {"text": "x" * 30001, "through_buyer_followup": 0, "through_post_payment_message": 0}])
    assert provider["calls"] == []


def test_imported_email_origin_survives_request_source_and_export(tmp_path, provider):
    from returnlight_app.returnlight import record_merchant_reply
    from returnlight_app.returnlight_record import render_case_record
    text = "I have not agreed to this.\n> I can refund the $38 jug price."
    path = tmp_path / ("e" * 32 + ".json")
    case = new_case(path, "EMAILSOURCE01", "38.00", text,
                    imported_email={"sender": "Buyer <buyer@example.com>", "subject": "Jug reply"})
    case = add_buyer_followup(path, "Please explain the replacement option.", len(case["history"]))
    offer = "A replacement jug can ship tomorrow."
    case = record_merchant_reply(path, offer, len(case["history"]), True, "d" * 32)
    provider["answer"] = {"current_request": "The buyer has not agreed and asks about replacement.",
        "source_quotes": [{"speaker": "email", "message_number": 1, "quote": "I have not agreed to this."},
                          {"speaker": "buyer", "message_number": 2, "quote": "Please explain the replacement option."},
                          {"speaker": "merchant", "message_number": 1, "quote": offer}]}
    result = review_case(path, reviewer(provider))
    request = provider["calls"][0]["messages"][0]["content"]
    assert "EMAIL MESSAGE 1:" in request and "BUYER MESSAGE 2:" in request
    assert "MERCHANT REPLY 1" in request
    assert "buyer@example.com" not in request and "Jug reply" not in request
    assert result["review"]["email_message_count"] == 1
    assert [source["speaker"] for source in result["review"]["sources"]] == ["email", "buyer", "merchant"]
    record = render_case_record(result)
    assert "Email message 1" in record and "Buyer message 2" in record and "Merchant reply 1" in record
    assert result["approved_cents"] == 0


@pytest.mark.parametrize("speaker", ["buyer", "merchant"])
def test_imported_quote_cannot_be_relabelled_as_a_direct_speaker(tmp_path, provider, speaker):
    text = "Not accepted.\n> I can refund the $38 jug price."
    path = tmp_path / ("f" * 32 + ".json")
    new_case(path, "EMAILSOURCE02", "38.00", text,
             imported_email={"sender": "buyer@example.com", "subject": "Quoted proposal"})
    provider["answer"] = {"current_request": "Refund accepted.",
        "source_quotes": [{"speaker": speaker, "message_number": 1, "quote": "I can refund the $38 jug price."}]}
    before = path.read_bytes()
    with pytest.raises(CaseError):
        review_case(path, reviewer(provider))
    assert path.read_bytes() == before
    assert len(provider["calls"]) == 1


def test_followup_email_origin_does_not_change_original_message_number(tmp_path, provider):
    path = tmp_path / ("b" * 32 + ".json")
    case = new_case(path, "EMAILSOURCE03", "38.00", "Please replace the jug.")
    case = add_buyer_followup(path, "Please wait.\n> Send the replacement today.", len(case["history"]),
        imported_email={"sender": "buyer@example.com", "subject": "Re: Jug"})
    provider["answer"] = {"current_request": "Wait for the buyer.",
        "source_quotes": [{"speaker": "email", "message_number": 2, "quote": "Please wait."}]}
    result = review_case(path, reviewer(provider))
    assert result["review"]["sources"][0]["message_number"] == 2
    assert result["review"]["sources"][0]["speaker"] == "email"
    content = provider["calls"][0]["messages"][0]["content"]
    assert "BUYER MESSAGE 1:" in content and "EMAIL MESSAGE 2:" in content


def test_unanswered_questions_keep_exact_sources_through_confirmation_and_export(tmp_path, provider):
    from returnlight_app.returnlight import human_review_case
    from returnlight_app.returnlight_record import render_case_record
    question = "Does the no-return offer still apply if I ask for a refund instead?"
    path = tmp_path / ("c" * 32 + ".json")
    case = new_case(path, "QUESTION01", "38.00", question)
    provider["answer"] = {"current_request": "The buyer asks whether no return applies to the requested refund.",
                         "source_quotes": [{"message_number": 1, "quote": question}], "open_questions": [1]}
    result = review_case(path, reviewer(provider))
    assert result["review"]["open_questions"] == [1]
    assert "open_questions" in provider["calls"][0]["messages"][0]["content"]
    assert "Questions to resolve" in render_case_record(result)
    result = human_review_case(path, question, True, expected_case_revision=len(result["history"]))
    assert result["ai_suggestion"]["open_questions"] == [1]
    record = render_case_record(result)
    current = record.split('<section class="review"', 1)[1].split("</section>", 1)[0]
    assert "Questions to resolve" in current and question in current
    result = add_buyer_followup(path, "I have now returned the jug.", len(result["history"]))
    result = human_review_case(path, "I have now returned the jug.", True, expected_case_revision=len(result["history"]))
    record = render_case_record(result)
    current = record.split('<section class="review"', 1)[1].split("</section>", 1)[0]
    assert "Questions to resolve" not in current
    assert result["approved_cents"] == 0


@pytest.mark.parametrize("questions", [[0], [3], [True], [1, 1], "1", [None]])
def test_invalid_question_sources_are_rejected(questions):
    with pytest.raises(CaseError, match="question source"):
        validate_conversation_review({**ANSWER, "open_questions": questions}, MESSAGES)


def test_empty_questions_and_old_readings_remain_distinct():
    assert "open_questions" not in validate_conversation_review(ANSWER, MESSAGES)
    assert validate_conversation_review({**ANSWER, "open_questions": []}, MESSAGES)["open_questions"] == []


def test_reply_draft_keeps_source_identity_through_human_review(tmp_path, provider):
    from returnlight_app.returnlight import human_review_case
    _, path, _ = make_case(tmp_path)
    draft = {"text": "I understand your request for the blue lamp, not the shade. I am reviewing the Friday timing and home address before confirming the next step.", "source_numbers": [1, 2]}
    provider["answer"] = {**ANSWER, "open_questions": [], "reply_draft": draft}
    result = review_case(path, reviewer(provider))
    assert result["review"]["reply_draft"] == draft
    assert result["review"]["sources"][1]["quote"] == MESSAGES[1]
    result = human_review_case(path, MESSAGES[-1], True, expected_case_revision=len(result["history"]))
    assert result["ai_suggestion"]["reply_draft"] == draft
    assert result["approved_cents"] == 0
    assert "reply_draft" in provider["calls"][0]["messages"][0]["content"]


@pytest.mark.parametrize("draft", [None, {}, {"text":"", "source_numbers":[1]},
    {"text":"x" * 3001, "source_numbers":[1]}, {"text":"Hello", "source_numbers":[]},
    {"text":"Hello", "source_numbers":[True]}, {"text":"Hello", "source_numbers":[3]},
    {"text":"Hello", "source_numbers":[1,1]}, {"text":"Hello", "source_numbers":"1"}])
def test_reply_draft_requires_exact_valid_source_references(draft):
    with pytest.raises(CaseError, match="reply draft"):
        validate_conversation_review({**ANSWER, "reply_draft":draft}, MESSAGES)


def test_unchecked_suggested_reply_is_saved_as_unfinished_work(tmp_path):
    from returnlight_app.returnlight_working_drafts import WorkingDraftStore
    _, path, case = make_case(tmp_path)
    store = WorkingDraftStore(tmp_path)
    reply = {"capture":case["capture_id"], "version":"current-case", "text":"I am reviewing the request.", "review_required":True}
    store.save(path, {"case_revision":len(case["history"]), "draft_version":None,
                     "draft":{"decision":None,"reply":reply}})
    assert store.detail(path)[1]["draft"]["reply"] == reply
    reply["review_required"] = "yes"
    with pytest.raises(CaseError, match="unfinished reply"):
        store.save(path, {"case_revision":len(case["history"]), "draft_version":None,
                         "draft":{"decision":None,"reply":reply}})


def test_selected_internal_work_requires_explicit_note_scope_and_keeps_provenance(tmp_path,provider):
    from returnlight_app.returnlight import update_merchant_followup, merchant_work_sources
    key,path,case=make_case(tmp_path)
    case=update_merchant_followup(path,"Check whether the driver can bring the prepaid label.",False,len(case["history"]))
    note="Courier confirmed that the driver can bring the prepaid label. Collection is not booked."
    case=update_merchant_followup(path,note,True,len(case["history"]))
    selected={"work_seq":merchant_work_sources(case)[-1]["seq"],"source_start":note.index("driver"),"quote":"driver can bring the prepaid label"}
    provider["answer"]={"current_request":"Check the buyer request against the merchant-recorded label result.",
        "source_quotes":[{"speaker":"buyer","message_number":2,"quote":MESSAGES[1]},
                         {"speaker":"work","message_number":1,"quote":note}],
        "open_questions":[],"reply_draft":{"text":"Our courier check says the driver can bring the prepaid label; collection is not booked.","source_numbers":[2]}}
    model=reviewer(provider);app=LocalCases(tmp_path,remote_reviewer=model)
    payload={"remote_confirmed":True,"destination_id":model.destination_id,"case_revision":len(case["history"]),
             "selected_conditions":[selected],"condition_scope":"selected-source-conditions-v1"}
    before=path.read_bytes()
    with pytest.raises(CaseError):app.action(key,"review",payload)
    assert provider["calls"]==[] and path.read_bytes()==before
    payload["condition_scope"]="selected-source-conditions-v2-work"
    result=app.action(key,"review",payload)["case"]
    request=provider["calls"][0]["messages"][0]["content"]
    assert note in request and "MERCHANT WORK NOTE 1" in request and "internal" in request
    assert "marked complete" in request and "not sent replies" in request
    assert result["review"]["sources"][1]["work_seq"]==selected["work_seq"]
    assert result["review"]["work_source_revision"]==selected["work_seq"]
    assert result["state"]=="OPEN" and result["approved_cents"]==0

def test_internal_work_cannot_be_an_unanswered_buyer_question_or_unselected_source():
    from returnlight_app.returnlight_remote_review import selected_work_notes
    notes=[{"seq":7,"text":"The driver can bring a prepaid label; no collection is booked.",
            "completed":True,"recorded_at":"2026-11-12T00:00:00+00:00","after_buyer_message":2,"current":True,"basis_seq":7}]
    value={"current_request":"Confirm current requirements.","source_quotes":[{"speaker":"work","message_number":1,"quote":notes[0]["text"]}],"open_questions":[1]}
    with pytest.raises(CaseError):validate_conversation_review(value,MESSAGES,work_notes=notes)
    value["open_questions"]=[]
    with pytest.raises(CaseError):validate_conversation_review(value,MESSAGES)
    result=validate_conversation_review(value,MESSAGES,work_notes=notes)
    assert result["sources"][0]["work_seq"]==7
    case={"history":[{"seq":7,"event":"merchant.followup_completed","detail":notes[0]["text"],"recorded_at":notes[0]["recorded_at"]},
                     {"seq":8,"event":"merchant.followup_opened","detail":"Check the new drop-off request.","recorded_at":notes[0]["recorded_at"]}]}
    chosen=selected_work_notes(case,[{"work_seq":7,"source_start":0,"quote":notes[0]["text"]}])
    assert len(chosen)==1 and chosen[0]["current"] is False and chosen[0]["basis_seq"]==8


@pytest.mark.parametrize("held_phase", ["headers", "stream"])
def test_changed_case_interrupts_obsolete_transport_and_releases_slot(tmp_path, held_phase):
    from returnlight_app.returnlight import ReviewInProgress
    started, release = threading.Event(), threading.Event()
    state = {"calls": 0}
    class HeldHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"] += 1
            first = state["calls"] == 1
            if first and held_phase == "headers":
                started.set()
                release.wait(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if first and held_phase == "stream":
                self.wfile.flush()
                started.set()
                release.wait(5)
            event = {"choices": [{"delta": {"content": json.dumps(ANSWER)}, "finish_reason": "stop"}]}
            try:
                self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), HeldHandler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    _, path, case = make_case(tmp_path)
    model = RemoteReviewer(f"http://127.0.0.1:{server.server_port}/chat",
                           "controlled", api_key="fixture-only", timeout=4)
    errors = []
    def run():
        try:
            review_case(path, model)
        except Exception as exc:
            errors.append(exc)
    pending = threading.Thread(target=run)
    try:
        pending.start()
        assert started.wait(1)
        # The unchanged active request still owns the slot; no second transmission.
        with pytest.raises(ReviewInProgress):
            review_case(path, model)
        assert state["calls"] == 1 and pending.is_alive()
        changed = add_buyer_followup(path, "Please use a drop-off point instead.",
                                    len(case["history"]))
        original = path.read_bytes()
        pending.join(1.5)
        assert not pending.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CaseChanged)
        assert path.read_bytes() == original
        assert not release.is_set()
        # Only the merchant's explicit new request transmits the current thread.
        fresh = review_case(path, model, expected_case_revision=len(changed["history"]))
        assert state["calls"] == 2 and fresh["state"] == "OPEN"
        assert fresh["buyer_followups"][-1]["text"] == "Please use a drop-off point instead."
        saved = path.read_bytes()
        release.set()
        time.sleep(.05)
        assert path.read_bytes() == saved
    finally:
        release.set()
        pending.join(5)
        server.shutdown()
        server.server_close()
        serving.join()


def test_changed_case_releases_a_held_tls_handshake(tmp_path, provider):
    import socket
    from returnlight_app.returnlight import ReviewInProgress
    _, path, case = make_case(tmp_path)
    started, release = threading.Event(), threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen(1)
    port = listener.getsockname()[1]
    def serve():
        connection, _ = listener.accept()
        try:
            assert connection.recv(4096)
            started.set()
            release.wait(5)
        finally:
            connection.close(); listener.close()
    serving = threading.Thread(target=serve, daemon=True); serving.start()
    model = RemoteReviewer(f"https://127.0.0.1:{port}/chat", "held-tls",
                           api_key="fixture-only", timeout=4)
    errors = []
    def read():
        try:
            review_case(path, model, expected_case_revision=len(case["history"]))
        except Exception as exc:
            errors.append(exc)
    pending = threading.Thread(target=read); pending.start()
    try:
        assert started.wait(2), repr(errors)
        with pytest.raises(ReviewInProgress):
            review_case(path, model, expected_case_revision=len(case["history"]))
        assert pending.is_alive()
        changed = add_buyer_followup(path, "Do not arrange collection. I will use the drop-off point.",
                                     len(case["history"]))
        saved = path.read_bytes()
        pending.join(1.5)
        assert not pending.is_alive(), "The obsolete TLS handshake still owns the review slot."
        assert len(errors) == 1 and isinstance(errors[0], CaseChanged)
        assert path.read_bytes() == saved and not release.is_set()
        provider["answer"] = {"current_request": changed["buyer_followups"][-1]["text"],
            "source_quotes": [{"message_number": 3, "quote": changed["buyer_followups"][-1]["text"]}]}
        current = review_case(path, reviewer(provider), expected_case_revision=len(changed["history"]))
        assert current["review"]["sources"][-1]["quote"] == changed["buyer_followups"][-1]["text"]
        final = path.read_bytes()
        release.set(); serving.join(2)
        assert path.read_bytes() == final
    finally:
        release.set(); pending.join(5); serving.join(5)
