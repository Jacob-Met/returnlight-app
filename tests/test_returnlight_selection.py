import io
import json
import pytest
from returnlight_app.returnlight import (CaseError, OllamaReviewer, validate_model_review,
    new_case, review_case, add_buyer_followup, case_snapshot)
from returnlight_app.returnlight_record import render_case_record

ORIGINAL = "The travel adapter was missing from my parcel. Please refund the $18 adapter."
LATEST = "I found the adapter inside the sleeve. Everything works. Please close this case without a refund; my earlier request is withdrawn."

def test_free_model_intent_is_not_accepted_as_source_selection(monkeypatch):
    result = {"evidence_id": 0, "summary": "Buyer wants a refund", "category": "missing"}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": json.dumps(result)}}).encode()))
    with pytest.raises(CaseError, match="must select"):
        OllamaReviewer().review(ORIGINAL)

@pytest.mark.parametrize("review", [
    {"kind": "source_selection", "quote": "invented buyer words"},
    {"kind": "summary", "quote": ORIGINAL},
    {"kind": "source_selection", "quote": ""},
    {"kind": "source_selection", "quote": True},
])
def test_invalid_source_selection_is_refused(review):
    with pytest.raises(CaseError):
        validate_model_review(review, ORIGINAL)

def test_latest_complete_message_survives_stale_selection_and_history(tmp_path):
    path = tmp_path / "case.json"
    case = new_case(path, "CAPTURE0123456789", "25.00", ORIGINAL)
    class Selector:
        def review(self, complaint):
            return {"kind": "source_selection", "quote": "The travel adapter was missing from my parcel."}
    case = review_case(path, Selector())
    case = add_buyer_followup(path, LATEST, len(case["history"]))
    case = review_case(path, Selector())  # Explicitly stale but exact; selection is fallible.
    assert case_snapshot(path) == case
    record = render_case_record(case)
    assert "Selected passage" in record
    assert "Buyer follow-up 1" in record
    assert LATEST in record
    assert record.index(LATEST) < record.index("<h2>AI suggestion · needs merchant review</h2>")
    assert "Model summary" not in record
    assert "Suggested category" not in record
    assert "Previous model review · superseded" in record
    assert case["approved_cents"] == 0

def test_legacy_interpretation_remains_readable_beside_latest_message(tmp_path):
    path = tmp_path / "case.json"
    case = new_case(path, "CAPTURE0123456789", "25.00", ORIGINAL)
    case["review"] = {"category": "missing", "summary": "Legacy saved summary", "quote": ORIGINAL}
    record = render_case_record(case)
    assert "Legacy saved summary" in record
    assert "Original buyer message" in record


def test_actual_reviewer_only_selects_latest_message_passages(monkeypatch):
    bodies = []
    def respond(request, **kwargs):
        bodies.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 0}'}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    result = OllamaReviewer().review_messages([ORIGINAL, "Please send a replacement instead.", LATEST])
    assert result["quote"] == "I found the adapter inside the sleeve."
    message = json.loads(bodies[0]["messages"][1]["content"])
    assert message["earlier_buyer_messages"] == [ORIGINAL, "Please send a replacement instead."]
    assert all(item["text"] in LATEST for item in message["buyer_passages"])
    assert not any(item["text"] in ORIGINAL for item in message["buyer_passages"])

def test_service_reviewer_cannot_save_an_earlier_message_as_latest_evidence(tmp_path):
    path = tmp_path / "case.json"
    case = new_case(path, "CAPTURE0123456789", "25.00", ORIGINAL)
    case = add_buyer_followup(path, LATEST, len(case["history"]))
    before = path.read_bytes()
    class StaleSelector:
        def review_messages(self, messages):
            assert messages == [ORIGINAL, LATEST]
            return {"kind": "source_selection", "quote": ORIGINAL}
    with pytest.raises(CaseError, match="not found"):
        review_case(path, StaleSelector())
    assert path.read_bytes() == before

def test_service_preserves_exact_multifollowup_boundaries(tmp_path):
    path = tmp_path / "case.json"
    middle = "I found the parcel, but thought the sleeve was broken. Please replace the sleeve."
    case = new_case(path, "CAPTURE0123456789", "25.00", ORIGINAL)
    case = add_buyer_followup(path, middle, len(case["history"]))
    case = add_buyer_followup(path, LATEST, len(case["history"]))
    class Selector:
        def review_messages(self, messages):
            assert messages == [ORIGINAL, middle, LATEST]
            return {"kind": "source_selection", "quote": LATEST}
    case = review_case(path, Selector())
    assert case_snapshot(path)["review"]["quote"] == LATEST
    assert [entry["text"] for entry in case["buyer_followups"]] == [middle, LATEST]


@pytest.mark.parametrize("selected", [1, 2])
def test_conditional_request_keeps_both_request_and_condition(monkeypatch, selected):
    message = ("The replacement lid fits the jar. Please refund the $12.40 lid. "
               "Only if you cannot send the matching blue one by Friday; otherwise no refund. "
               "The jar itself is fine.")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": json.dumps({"evidence_id": selected})}}).encode()))
    review = OllamaReviewer().review(message)
    assert "Please refund the $12.40 lid." in review["context"]
    assert "Only if you cannot send the matching blue one by Friday; otherwise no refund." in review["context"]
    assert review["quote"] in review["context"] in message
    assert len(review["quote"]) <= 300
    assert len(review["context"]) <= 900

@pytest.mark.parametrize("context", ["invented text", True, "Earlier request only."])
def test_untrusted_selection_context_is_rejected(context):
    with pytest.raises(CaseError, match="context"):
        validate_model_review({"kind": "source_selection", "quote": "Everything works.",
                               "context": context}, LATEST)

def test_context_preserves_whitespace_and_repeated_sentence_occurrence(monkeypatch):
    message = "Please refund it.  Actually, keep the replacement.\nPlease refund it.\nOnly if the replacement fails."
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 2}'}}).encode()))
    review = OllamaReviewer().review(message)
    assert review["context"] == message

def test_saved_review_context_stays_bound_to_latest_message(tmp_path, monkeypatch):
    path = tmp_path / "case.json"
    case = new_case(path, "CAPTURE0123456789", "25.00", ORIGINAL)
    case = add_buyer_followup(path, LATEST, len(case["history"]))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 1}'}}).encode()))
    case = review_case(path, OllamaReviewer())
    assert case_snapshot(path)["review"]["context"] == LATEST
    assert case["approved_cents"] == 0

def test_offline_record_preserves_escaped_context(tmp_path, monkeypatch):
    message = 'Please refund the lid. Only if <replacement> fails & the shop agrees. The jar is fine.'
    path = tmp_path / "case.json"
    new_case(path, "CAPTURE0123456789", "25.00", message)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 0}'}}).encode()))
    case = review_case(path, OllamaReviewer())
    record = render_case_record(case)
    assert '<mark>Please refund the lid.</mark>' in record
    assert 'Only if &lt;replacement&gt; fails &amp; the shop agrees.' in record
    assert '<replacement>' not in record


def test_short_correction_retains_exclusion_and_final_instruction(monkeypatch):
    message = ("I said refund both items yesterday, but I found the blue mug in the second box. "
               "Please do NOT refund that one. The red bowl is cracked. "
               "If you can replace just the bowl and get it here before Friday, I prefer that; otherwise refund only the bowl. "
               "Shipping was free. Please check delivery before doing anything.")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 3}'}}).encode()))
    review = OllamaReviewer().review(message)
    assert "Please do NOT refund that one." in review["context"]
    assert "Please check delivery before doing anything." in review["context"]
    assert review["quote"] == "If you can replace just the bowl and get it here before Friday, I prefer that; otherwise refund only the bowl."

def test_long_context_remains_bounded_and_exact(monkeypatch):
    message = "An earlier shipping update arrived. " * 40 + "Please send the replacement. Only if it can arrive by Friday."
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": json.dumps({"quote": "Please send the replacement. Only if it can arrive by Friday."})}}).encode()))
    review = OllamaReviewer().review(message)
    assert len(review["context"]) <= 900
    assert review["context"] in message
    assert "Only if it can arrive by Friday." in review["context"]

@pytest.mark.parametrize("bad", [
    {"quote": "Please refund everything immediately."},
    {"quote": True},
    {"quote": "x" * 301},
    {"quote": "Please send the replacement.", "amount": "42.00"},
    {"evidence_id": 999},
])
def test_long_review_rejects_invented_or_invalid_text_without_saving(tmp_path, monkeypatch, bad):
    message = "An earlier shipping update arrived. " * 40 + "Please send the replacement. Only if it can arrive by Friday."
    path = tmp_path / "case.json"
    new_case(path, "CAPTURE0123456789", "25.00", message)
    before = path.read_bytes()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": json.dumps(bad)}}).encode()))
    with pytest.raises(CaseError):
        review_case(path, OllamaReviewer())
    assert path.read_bytes() == before


def test_long_review_cannot_reuse_a_superseded_refund_request(tmp_path, monkeypatch):
    original = "Please refund the entire lamp purchase."
    latest = "The package has now arrived intact. " * 30 + "I want only a replacement shade, no refund."
    path = tmp_path / "case.json"
    case = new_case(path, "CAPTURE0123456789", "25.00", original)
    add_buyer_followup(path, latest, len(case["history"]))
    before = path.read_bytes()
    requests = []
    def respond(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": json.dumps({"quote": original})}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    with pytest.raises(CaseError):
        review_case(path, OllamaReviewer())
    assert path.read_bytes() == before
    sent = json.loads(requests[0]["messages"][1]["content"])
    assert sent["latest_buyer_message"] == latest
    assert sent["earlier_buyer_messages"] == [original]

def test_long_case_inbox_focus_is_removed_when_buyer_corrects_it(tmp_path):
    from returnlight_app.returnlight_web import LocalCases
    request = "Please replace the shade only; do not refund the lamp."
    message = "Thank you for explaining the return process. " * 25 + request
    class Selector:
        def review_messages(self, messages):
            return {"kind": "source_selection", "quote": request}
    service = LocalCases(tmp_path, reviewer_factory=Selector)
    opened = service.create({"capture_id": "CAPTURE0123456789", "ceiling": "25.00", "complaint": message})
    key = opened["key"]
    assert service.summary(key)["review_quote"] is None
    reviewed = service.action(key, "review", {})["case"]
    assert service.summary(key)["review_quote"] == request
    service.action(key, "followup", {"text": "I have now found an intact shade; please close the case.", "case_revision": len(reviewed["history"])})
    assert service.summary(key)["review_quote"] is None



def test_selected_repeated_passage_retains_actual_occurrence_in_record(tmp_path, monkeypatch):
    message = "📦 First request. Please refund it. Actually, keep the replacement. Please refund it. Only if the replacement fails."
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": '{"evidence_id": 3}'}}).encode()))
    path = tmp_path / "case.json"
    new_case(path, "CAPTURE0123456789", "25.00", message)
    case = review_case(path, OllamaReviewer())
    assert case["review"]["quote_start"] == message.rindex("Please refund it.")
    record = render_case_record(case)
    assert "Actually, keep the replacement. <mark>Please refund it.</mark> Only if" in record
    assert "First request. <mark>" not in record
    assert case["approved_cents"] == 0


@pytest.mark.parametrize("start", [-1, True, 1, 10000])
def test_invalid_selected_occurrence_is_rejected(start):
    with pytest.raises(CaseError, match="position"):
        validate_model_review({"kind": "source_selection", "quote": "Please refund it.",
            "context": "Please refund it. Only if needed.", "quote_start": start},
            "Please refund it. Only if needed.")

def test_long_invalid_copy_recovers_with_exact_numbered_source(monkeypatch):
    message = "Earlier shipping update. " * 50 + "Please replace the bowl. Do not refund yet."
    replies = iter([{"quote": "Please refund everything now."}, {"evidence_id": 51}])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k:
        io.BytesIO(json.dumps({"message": {"content": json.dumps(next(replies))}}).encode()))
    review = OllamaReviewer().review(message)
    assert review["quote"] == "Do not refund yet."
    assert "Please replace the bowl." in review["context"]
    assert review["context"] in message


def test_long_transport_timeout_does_not_start_second_request(monkeypatch):
    calls = []
    def timeout(*a, **k):
        calls.append(1)
        raise TimeoutError("timed out")
    monkeypatch.setattr("urllib.request.urlopen", timeout)
    with pytest.raises(TimeoutError):
        OllamaReviewer().review("Earlier shipping update. " * 50)
    assert len(calls) == 1


def test_bounded_context_preserves_later_occurrence():
    prefix = "Earlier detail. " * 90 + "Please refund it. That request is withdrawn. "
    context = "Current request. Please refund it. Only if replacement fails."
    message = prefix + context
    start = message.rindex("Please refund it.")
    review = validate_model_review({"kind": "source_selection", "quote": "Please refund it.",
        "context": context, "quote_start": start, "context_start": len(prefix)}, message)
    from returnlight_app.returnlight_record import _selection
    assert _selection(review) == "Current request. <mark>Please refund it.</mark> Only if replacement fails."
    for bad in [True, -1, 0, len(prefix) + 1]:
        with pytest.raises(CaseError, match="position"):
            validate_model_review({**review, "context_start": bad}, message)


def test_repeated_long_quote_requires_numbered_occurrence(monkeypatch):
    message = "Earlier shipping update. " * 50 + (
        "Please refund it. That request is withdrawn. Please refund it. Only if replacement fails.")
    replies = iter([{"quote": "Please refund it."}, {"evidence_id": 52}])
    calls = []
    def respond(request, **kwargs):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": json.dumps(next(replies))}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    review = OllamaReviewer().review(message)
    assert len(calls) == 2
    assert review["quote_start"] == message.rindex("Please refund it.")
    assert "Only if replacement fails." in review["context"]


def test_unique_long_quote_keeps_position_without_second_call(monkeypatch):
    message = "Earlier shipping update. " * 50 + "Please replace only the cracked bowl."
    calls = []
    def respond(request, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps({"message": {"content": json.dumps(
            {"quote": "Please replace only the cracked bowl."})}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    review = OllamaReviewer().review(message)
    assert len(calls) == 1
    assert review["quote_start"] == message.index(review["quote"])
    assert review["context_start"] < review["quote_start"]
    assert message[review["context_start"]:review["context_start"] + len(review["context"])] == review["context"]
