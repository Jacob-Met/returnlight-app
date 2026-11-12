"""Source-context regressions replay retained model selections, not new inference."""
import io
import json
from pathlib import Path

import pytest

from returnlight_app.returnlight import OllamaReviewer, _source_context_bounds
from returnlight_app.returnlight_record import _selection

ROOT = Path(__file__).resolve().parents[1]


def test_retained_lamp_selection_keeps_explicit_refund_exclusion(monkeypatch):
    observed = json.loads((ROOT / "tests/fixtures/lamp-heldout-result.json").read_text())
    message = observed["case"]["buyer_followups"][-1]["text"]
    quote = observed["case"]["review"]["quote"]
    calls = []
    def respond(request, **kwargs):
        calls.append(request)
        return io.BytesIO(json.dumps({"message": {"content": json.dumps({"quote": quote})}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    review = OllamaReviewer().review(message)
    exclusion = "I am not authorising a refund or a whole-lamp exchange at this point."
    assert review["quote"] == quote
    assert exclusion in review["context"]
    assert exclusion in _selection(review)
    assert "<mark>" + quote + "</mark>" in _selection(review)
    assert len(calls) == 1
    assert len(review["context"]) <= 900
    assert message[review["context_start"]:review["context_start"] + len(review["context"])] == review["context"]
    assert message[review["quote_start"]:review["quote_start"] + len(quote)] == quote


def test_long_quote_preserves_following_condition_and_whitespace(monkeypatch):
    prefix = "The courier sent another update. " * 34
    quote = "Please refund only the broken handle."
    message = prefix + "The lid works.\n\n" + quote + "  Only if a replacement cannot arrive by Friday.\nI will keep the pot."
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs:
        io.BytesIO(json.dumps({"message": {"content": json.dumps({"quote": quote})}}).encode()))
    review = OllamaReviewer().review(message)
    assert review["context"] == "The lid works.\n\n" + quote + "  Only if a replacement cannot arrive by Friday.\n"
    assert review["quote_start"] == message.index(quote)
    assert review["context_start"] == message.index("The lid works.")


@pytest.mark.parametrize("prefix,suffix", [
    ("x" * 1200, "y" * 1200),
    ("Earlier message. " * 100, " Following sentence. " * 100),
    ("📦 " * 500, "\nOnly if it cannot be replaced."),
    ("Earlier message. " * 100, ""),
])
def test_source_context_remains_bounded_and_exact(prefix, suffix):
    quote = "Please replace the shade."
    message = prefix + quote + suffix
    start = len(prefix)
    left, right = _source_context_bounds(message, start, len(quote))
    assert 0 <= left <= start < start + len(quote) <= right <= len(message)
    assert right - left <= 900
    assert quote in message[left:right]
