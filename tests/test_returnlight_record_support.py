"""Downloaded support records retain the actual reopen/handle sequence."""
import json
from returnlight_app.returnlight import add_post_payment_message, resolve_post_payment_support
from returnlight_app.returnlight_record import render_case_record
from test_returnlight_postpayment_support import attempted


def test_record_keeps_repeated_handling_between_the_messages_it_resolved(tmp_path):
    _, _, path, before = attempted(tmp_path)
    current = before
    steps = [
        ("buyer", "First buyer asks where their refund is."),
        ("handled", "First merchant supplied the refund reference."),
        ("buyer", "Second buyer reports their bank still cannot locate it."),
        ("buyer", "Third buyer supplies the bank follow-up."),
        ("handled", "Second merchant explained the bank processing interval."),
        ("buyer", "Fourth buyer asks for another update."),
    ]
    for kind, text in steps:
        action = add_post_payment_message if kind == "buyer" else resolve_post_payment_support
        current = action(path, text, len(current["history"]))
    # Timestamp sorting must not rewrite the recorded causal order.
    for entry in current["post_payment_messages"] + current["support_resolutions"]:
        entry["recorded_at"] = "2026-11-11T10:00:00Z"
    unchanged = json.dumps(current, sort_keys=True)
    record = render_case_record(current)
    positions = [record.index(text) for _, text in steps]
    assert positions == sorted(positions)
    assert "Support follow-up open" in record
    assert "through buyer message 1" in record
    assert "through buyer message 3" in record
    assert json.dumps(current, sort_keys=True) == unchanged


def test_handled_record_retains_each_merchant_note_and_escapes_it(tmp_path):
    _, _, path, before = attempted(tmp_path, "PENDING")
    first = add_post_payment_message(path, "Please explain the pending refund.", len(before["history"]))
    handled = resolve_post_payment_support(path, 'Merchant checked <bank> & sent the reference.', len(first["history"]))
    record = render_case_record(handled)
    assert "Support follow-up handled" in record
    assert "&lt;bank&gt; &amp;" in record and "<bank>" not in record
    assert record.index("Please explain") < record.index("Merchant checked")
    assert "Refund pending when recorded" in record
