"""Payment entry repair exercises the actual durable state machine."""
import json

import pytest

import returnlight_app.returnlight as app

WORDS = 'The espresso scale arrived cracked. I paid $99.50 and would like a refund.'
ORIGINAL = 'CAPTURE0000000091'
CORRECTED = 'CAPTURE0000000092'
REASON = 'I copied the previous payment and misplaced the decimal point.'


class EvidenceFixture:
    def review(self, words):
        return {'category': 'damaged', 'summary': 'Buyer reports a cracked espresso scale.',
                'quote': 'The espresso scale arrived cracked.'}


class PaymentFixture:
    def __init__(self, outcome='COMPLETED'):
        self.outcome = outcome
        self.posts = []

    def capture(self, capture_id):
        return {'id': capture_id, 'status': 'COMPLETED',
                'amount': {'currency_code': 'USD', 'value': '99.50'}}

    def refund(self, capture_id, cents, request_id):
        self.posts.append((capture_id, cents, request_id))
        if self.outcome == 'UNKNOWN':
            raise TimeoutError('Fixture response lost after dispatch')
        return {'id': 'REFUND0000000001', 'status': self.outcome,
                'amount': {'currency_code': 'USD', 'value': app.dollars(cents)}}


@pytest.fixture
def case_path(tmp_path, monkeypatch):
    monkeypatch.setenv('RETURNLIGHT_CAPTURE_STORE', str(tmp_path / 'capture-attempts'))
    path = tmp_path / 'case.json'
    app.new_case(path, ORIGINAL, '9.95', WORDS)
    return path


def correct(path, capture_id=CORRECTED, ceiling='99.50', reason=REASON, revision=None):
    current = app.case_snapshot(path)
    return app.correct_payment_details(path, capture_id, ceiling, reason,
                                       len(current['history']) if revision is None else revision)


def approve(path, amount='24.95'):
    current = app.case_snapshot(path)
    if (current.get('review') or {}).get('kind') != 'human_selection':
        current = app.human_review_case(path, WORDS, True, expected_case_revision=len(current['history']))
    return app.approve_case(path, amount, app.approval_phrase(current, amount),
                            expected_case_revision=len(current['history']))


def test_corrections_preserve_case_buyer_words_original_values_and_reload(case_path):
    before = app.case_snapshot(case_path)
    first = correct(case_path)
    assert first['id'] == before['id']
    assert first['complaint'] == WORDS
    assert first['state'] == 'OPEN'
    assert first['review'] is None
    assert first['approved_cents'] == 0
    assert first['history'][:-1] == before['history']
    assert first['original_payment_details'] == {'capture_id': ORIGINAL, 'ceiling_cents': 995}
    assert first['history'][-1]['recorded_at']
    assert REASON in first['history'][-1]['detail']
    assert 'USD 9.95 → USD 99.50' in first['history'][-1]['detail']
    assert app.case_snapshot(case_path) == first
    second = correct(case_path, ceiling='89.50', reason='Removed the separately paid delivery charge.')
    assert second['original_payment_details'] == first['original_payment_details']
    assert second['history'][:-1] == first['history']
    assert 'USD 99.50 → USD 89.50' in second['history'][-1]['detail']
    assert len(list(case_path.parent.glob('*.json'))) == 1


def test_correction_withdraws_unsent_approval_and_old_tabs_cannot_send(case_path):
    correct(case_path)
    app.review_case(case_path, EvidenceFixture())
    current = app.case_snapshot(case_path)
    reviewed = app.human_review_case(case_path, WORDS, True, expected_case_revision=len(current['history']))
    approved = approve(case_path)
    old_approval = app.approval_revision(approved)
    old_send = 'SEND ' + approved['id'][:8] + ' 24.95'
    changed = correct(case_path, ceiling='79.50', reason='Excluded the undamaged accessory from the case ceiling.')
    assert changed['state'] == 'REVIEWED'
    assert changed['review'] == reviewed['review']
    assert changed['approved_cents'] == 0
    assert 'Unsent USD 24.95 approval withdrawn' in changed['history'][-1]['detail']
    with pytest.raises(app.CaseError, match='changed in another tab'):
        app.approve_case(case_path, '24.95', app.approval_phrase(changed, '24.95'),
                         expected_case_revision=len(reviewed['history']))
    gateway = PaymentFixture()
    with pytest.raises(app.CaseError, match='Only a new approval'):
        app.execute_case(case_path, gateway, expected_approval_revision=old_approval,
                         send_confirmation=old_send)
    fresh = approve(case_path)
    with pytest.raises(app.CaseError, match='changed in another tab'):
        app.execute_case(case_path, gateway, expected_approval_revision=old_approval,
                         send_confirmation=old_send)
    assert gateway.posts == []
    final = app.execute_case(case_path, gateway,
                             expected_approval_revision=app.approval_revision(fresh),
                             send_confirmation=old_send)
    assert final['state'] == 'COMPLETED'
    assert gateway.posts[0][:2] == (CORRECTED, 2495)


@pytest.mark.parametrize('outcome', ['UNKNOWN', 'PENDING', 'COMPLETED', 'FAILED', 'CLOSED'])
def test_attempted_or_closed_cases_cannot_change_the_payment(case_path, outcome):
    correct(case_path)
    if outcome == 'CLOSED':
        case = app.case_snapshot(case_path)
        app.close_case(case_path, 'Buyer accepted replacement instead of a refund.',
                       'CLOSE ' + case['id'][:8])
    else:
        app.review_case(case_path, EvidenceFixture())
        approve(case_path)
        gateway = PaymentFixture(outcome)
        if outcome == 'UNKNOWN':
            with pytest.raises(app.OutcomeUnknown):
                app.execute_case(case_path, gateway)
        else:
            app.execute_case(case_path, gateway)
    before = case_path.read_bytes()
    with pytest.raises(app.CaseError, match='no refund attempt'):
        correct(case_path, capture_id='CAPTURE0000000098')
    assert case_path.read_bytes() == before


def test_interrupted_capture_reservation_cannot_be_hidden_by_correction(case_path, monkeypatch):
    correct(case_path)
    app.review_case(case_path, EvidenceFixture())
    approve(case_path)
    original_save = app._save

    def stop_at_case_checkpoint(target, case):
        if target == case_path and case.get('state') == 'UNKNOWN':
            raise OSError('Fixture interruption after capture reservation, before case checkpoint')
        original_save(target, case)

    monkeypatch.setattr(app, '_save', stop_at_case_checkpoint)
    gateway = PaymentFixture()
    with pytest.raises(OSError, match='Fixture interruption'):
        app.execute_case(case_path, gateway)
    assert app.case_snapshot(case_path)['state'] == 'APPROVED'
    assert gateway.posts == []
    before = case_path.read_bytes()
    with pytest.raises(app.CaseError, match='already reserved'):
        correct(case_path, capture_id='CAPTURE0000000098')
    assert case_path.read_bytes() == before


def test_unchanged_or_stale_or_unexplained_edits_leave_case_unchanged(case_path):
    before = case_path.read_bytes()
    for overrides, message in [({'capture_id': ORIGINAL, 'ceiling': '9.95'}, 'unchanged'),
                               ({'revision': 0}, 'Reload this page'),
                               ({'reason': ''}, 'correction reason')]:
        with pytest.raises(app.CaseError, match=message):
            correct(case_path, **overrides)
        assert case_path.read_bytes() == before


def test_correction_preserves_only_current_review_after_buyer_followup(case_path):
    correct(case_path)
    reviewed = app.review_case(case_path, EvidenceFixture())
    words = 'I can keep the scale. Please refund only 19.50 for a replacement part.'
    changed = app.add_buyer_followup(case_path, words, len(reviewed['history']))
    corrected = correct(case_path, ceiling='79.50', reason='The ceiling excludes the separate accessory.')
    assert corrected['state'] == 'OPEN' and corrected['review'] is None
    assert corrected['buyer_followups'] == changed['buyer_followups']
    class CurrentEvidenceFixture:
        def review(self, source):
            assert WORDS in source and words in source
            return {'category': 'damaged', 'summary': 'Buyer asks for only the replacement part.',
                    'quote': 'Please refund only 19.50 for a replacement part.'}
    current = app.review_case(case_path, CurrentEvidenceFixture())
    final = correct(case_path, ceiling='69.50', reason='Shipping charge was included in the previous ceiling.')
    assert final['review'] == current['review'] and final['state'] == 'OPEN'
    assert final['buyer_followups'] == current['buyer_followups']
    with pytest.raises(app.CaseError, match='changed in another tab'):
        app.approve_case(case_path, '19.50', app.approval_phrase(final, '19.50'),
                         expected_case_revision=len(current['history']))


def test_changed_capture_keeps_old_purchase_out_of_current_context(tmp_path):
    from test_returnlight_purchase_context import observation, CAPTURE, COMPLAINT
    from returnlight_app.returnlight_record import render_case_record
    path = tmp_path / "purchase.json"
    created = app.new_case(path, CAPTURE, "9.60", COMPLAINT,
                           purchase_observation=observation())
    changed = app.correct_payment_details(path, CORRECTED, "12.00", REASON,
                                          len(created["history"]))
    assert "purchase_context" not in changed
    assert changed["previous_purchase_contexts"] == [created["purchase_context"]]
    assert app.case_snapshot(path) == changed
    record = render_case_record(changed)
    assert "Earlier payment purchase" in record and "Ceramic spoon rest" in record
    assert CAPTURE in record and CORRECTED in record


def test_limit_only_correction_keeps_current_purchase(tmp_path):
    from test_returnlight_purchase_context import observation, CAPTURE, COMPLAINT
    path = tmp_path / "purchase.json"
    created = app.new_case(path, CAPTURE, "9.60", COMPLAINT,
                           purchase_observation=observation())
    changed = app.correct_payment_details(path, CAPTURE, "8.00", REASON,
                                          len(created["history"]))
    assert changed["purchase_context"] == created["purchase_context"]
    assert not changed.get("previous_purchase_contexts")
    assert app.case_snapshot(path) == changed
