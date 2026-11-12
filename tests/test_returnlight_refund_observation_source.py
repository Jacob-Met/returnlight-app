"""Authored original-refund checks; no real model or payment requests."""
import copy
import json
import pytest
from returnlight_app.returnlight import (
    CaseError, CaseChanged, new_case, case_snapshot, add_post_payment_message,
    retain_refund_observation, review_support_case)
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_support_review import (
    support_sources, support_content, support_basis, validate_support_review,
    current_support_review)

KEY = "a" * 32

class Gateway:
    def __init__(self):
        self.calls = []
        self.status = "COMPLETED"
        self.fail = False
    def refund_details(self, rid):
        self.calls.append(rid)
        if self.fail:
            raise OSError("Controlled unavailable read")
        return {"id": rid, "status": self.status,
                "amount": {"currency_code": "USD", "value": "5.40"},
                "supplementary_data": {"related_ids": {"capture_id": "CHECKCAPTURE01"}}}

@pytest.fixture
def desk(tmp_path):
    path = tmp_path / (KEY + ".json")
    case = new_case(path, "CHECKCAPTURE01", "5.40", "The blue towel was damaged. Please refund USD5.40.")
    case.update(state="PENDING", approved_cents=540, request_id="original-request", refund_id="CHECKREFUND01")
    path.write_text(json.dumps(case))
    case = add_post_payment_message(path, "My refund has not arrived. When did you last check it? Do not send another refund.", len(case["history"]))
    gateway = Gateway()
    return LocalCases(tmp_path, gateway_factory=lambda: gateway), path, case, gateway

def keep(app, path, token):
    return app.action(KEY, "refund-observation", {
        "case_revision": len(case_snapshot(path)["history"]),
        "refund_observation_id": token})["case"]

def answer(case):
    bank = support_sources(case)
    latest = next(i for i, s in enumerate(bank, 1) if s.get("support_number") == len(case["post_payment_messages"]))
    return {"current_request": "Explain the dated original refund check; receipt is still unconfirmed.",
            "source_quotes": [{"source_number": latest, "quote": bank[latest-1]["text"]},
                              {"source_number": len(bank), "quote": bank[-1]["text"]}],
            "open_questions": [1],
            "reply_draft": {"text": "The original refund check reported completed. I still need to confirm receipt.", "source_numbers": [1,2]}}

def test_get_is_readonly_explicit_keep_survives_restart_and_changes_reading(desk):
    app, path, before, gateway = desk
    raw = path.read_bytes(); original_input = support_content(before)
    checked = app.inspect_refunds(KEY)
    assert path.read_bytes() == raw and len(gateway.calls) == 1
    assert "refund_observation" not in case_snapshot(path)
    kept = keep(app, path, checked["refund_observation_id"])
    for field in ("state", "approved_cents", "capture_id", "request_id", "refund_id"):
        assert kept[field] == before[field]
    assert kept["history"][:-1] == before["history"]
    assert kept["history"][-1]["event"] == "refund.observation_kept"
    restarted = LocalCases(path.parent)
    assert restarted.detail(KEY)["case"]["refund_observation"] == kept["refund_observation"]
    assert restarted.detail(KEY)["refund_check_source"] == support_sources(kept)[-1]["text"]
    assert support_content(kept) != original_input
    bank = support_sources(kept)
    assert bank[-1]["kind"] == "refund_check"
    assert "COMPLETED" in bank[-1]["text"] and "UTC" in bank[-1]["text"]
    assert "PENDING" in next(s["text"] for s in bank if s["kind"] == "payment")
    assert "not a new check or confirmation" in bank[-1]["text"]
    assert len(gateway.calls) == 1
    raw = path.read_bytes()
    keep(app, path, checked["refund_observation_id"])
    assert path.read_bytes() == raw

@pytest.mark.parametrize("change", ["expired", "wrong_case", "wrong_identity", "no_token"])
def test_retention_requires_current_server_observation(desk, change):
    app, path, before, gateway = desk
    token = app.inspect_refunds(KEY)["refund_observation_id"]
    if change == "expired": app._refund_observations[token]["stored_at"] -= 601
    if change == "wrong_case": app._refund_observations[token]["key"] = "b"*32
    if change == "wrong_identity": app._refund_observations[token]["identity"] = ("different",)
    if change == "no_token": token = "not-observed"
    raw = path.read_bytes()
    with pytest.raises(CaseError): keep(app, path, token)
    assert path.read_bytes() == raw

def test_failed_new_check_preserves_last_retained_source(desk):
    app, path, before, gateway = desk
    keep(app, path, app.inspect_refunds(KEY)["refund_observation_id"])
    raw = path.read_bytes(); gateway.fail = True
    with pytest.raises(OSError): app.inspect_refunds(KEY)
    assert path.read_bytes() == raw
    assert len(gateway.calls) == 2

def test_changed_case_during_get_has_no_shareable_token(desk):
    app, path, before, gateway = desk
    original = gateway.refund_details
    def changed(rid):
        result = original(rid)
        c = json.loads(path.read_text()); c["state"] = "COMPLETED"; path.write_text(json.dumps(c))
        return result
    gateway.refund_details = changed
    with pytest.raises(CaseChanged): app.inspect_refunds(KEY)
    assert not app._refund_observations

def test_newer_retained_check_retires_old_support_reading_and_older_check(desk):
    app, path, before, gateway = desk
    first = app.inspect_refunds(KEY)["refund_observation_id"]
    kept = keep(app, path, first)
    old = validate_support_review(answer(kept), kept)
    kept["support_review"] = old; path.write_text(json.dumps(kept))
    gateway.status = "FAILED"
    second = app.inspect_refunds(KEY)["refund_observation_id"]
    updated = keep(app, path, second)
    assert current_support_review(updated) is None
    assert support_basis(updated) != old["source_basis"]
    assert updated["state"] == "PENDING"
    with pytest.raises(CaseError, match="newer"): keep(app, path, first)

def test_dated_check_cannot_supply_unanswered_buyer_question(desk):
    app, path, before, gateway = desk
    kept = keep(app, path, app.inspect_refunds(KEY)["refund_observation_id"])
    good = answer(kept); validate_support_review(good, kept)
    bad = copy.deepcopy(good); bad["open_questions"] = [2]
    with pytest.raises(CaseError, match="unanswered"): validate_support_review(bad, kept)

def test_consent_includes_dated_check_before_remote_call(desk):
    app, path, before, gateway = desk
    kept = keep(app, path, app.inspect_refunds(KEY)["refund_observation_id"])
    class Reviewer:
        destination_id = "controlled"
        calls = 0
        def review_support_case(self, case, before_request):
            before_request(); self.calls += 1
            return validate_support_review(answer(case), case)
    reviewer = Reviewer()
    app.remote_reviewer = reviewer; app.reviewer_factory = lambda: reviewer
    payload = {"case_revision": len(kept["history"]), "remote_confirmed": True,
               "destination_id": "controlled", "review_scope": "current-support-v1"}
    with pytest.raises(CaseError, match="dated"): app.action(KEY, "support-review", payload)
    assert reviewer.calls == 0
    payload["review_scope"] = "current-support-refund-check-v3"
    app.action(KEY, "support-review", payload)
    assert reviewer.calls == 1

def test_new_kept_check_while_reading_discards_stale_result(desk):
    app, path, before, gateway = desk
    token = app.inspect_refunds(KEY)["refund_observation_id"]
    class Reader:
        def review_support_case(self, case, before_request):
            before_request()
            keep(app, path, token)
            return {"kind": "support_brief", "summary": "old", "sources": [], "source_basis": support_basis(case)}
    with pytest.raises(CaseChanged):
        review_support_case(path, Reader(), len(before["history"]))
    assert not case_snapshot(path).get("support_review")

@pytest.mark.parametrize("field,value", [("amount_cents", True), ("refund_id", "OTHERREFUND01"),
                                       ("checked_at", "2026-11-11T22:00:00"), ("status", "ARRIVED")])
def test_corrupt_retained_observation_is_not_presented(desk, field, value):
    app, path, before, gateway = desk
    kept = keep(app, path, app.inspect_refunds(KEY)["refund_observation_id"])
    kept["refund_observation"][field] = value; path.write_text(json.dumps(kept))
    with pytest.raises(CaseError): case_snapshot(path)


def test_order_candidate_is_not_an_original_refund_source(desk, monkeypatch):
    app, path, before, gateway = desk
    monkeypatch.setattr("returnlight_app.returnlight_web.discover_case_refunds",
                        lambda *a: {"capture_id": before["capture_id"], "refunds": []})
    result = app.inspect_refunds(KEY)
    assert "refund_observation_id" not in result
    assert not app._refund_observations

def test_payload_cannot_replace_verified_check_words(desk):
    app, path, before, gateway = desk
    token = app.inspect_refunds(KEY)["refund_observation_id"]
    result = app.action(KEY, "refund-observation", {
        "case_revision": len(before["history"]), "refund_observation_id": token,
        "status": "ARRIVED", "checked_at": "invented",
        "refund_observation": {"status": "ARRIVED"}})
    assert result["case"]["refund_observation"]["status"] == "COMPLETED"
    assert "ARRIVED" not in result["refund_check_source"]


def test_observed_check_can_invalidate_saved_reply_without_retaining_facts(desk):
    app, path, case, gateway = desk
    draft = {"decision": None, "reply": {"capture": case["capture_id"],
             "version": json.dumps([case["capture_id"],case["state"],540,case["refund_id"],len(case["history"])]),
             "text": "Our saved record still says the refund is pending."}}
    saved = app.working_drafts.save(path, {"case_revision":len(case["history"]),"draft_version":None,"draft":draft})
    raw=path.read_bytes(); source=support_content(case)
    observed=app.inspect_refunds(KEY)
    assert app.working_drafts.detail(path)[1]==saved
    marked=app.action(KEY,"refund-reply-review",{"refund_observation_id":observed["refund_observation_id"]})["working_draft"]
    assert marked["draft"]["reply"]=={**draft["reply"],"review_required":True}
    assert marked["version"]!=saved["version"]
    assert path.read_bytes()==raw and support_content(case_snapshot(path))==source
    assert "refund_observation" not in case_snapshot(path)
    assert LocalCases(path.parent).detail(KEY)["working_draft"]==marked
    assert app.action(KEY,"refund-reply-review",{"refund_observation_id":observed["refund_observation_id"]})["working_draft"]==marked
    with pytest.raises(CaseChanged):
        app.working_drafts.save(path,{"case_revision":len(case["history"]),"draft_version":saved["version"],"draft":draft})
    checked=app.working_drafts.save(path,{"case_revision":len(case["history"]),"draft_version":marked["version"],"draft":draft})
    assert app.action(KEY,"refund-reply-review",{"refund_observation_id":observed["refund_observation_id"]})["working_draft"]==checked
    assert len(gateway.calls)==1

@pytest.mark.parametrize("change",["expired","wrong_case","wrong_identity","missing"])
def test_reply_review_requires_the_current_original_check(desk,change):
    app,path,case,gateway=desk
    token=app.inspect_refunds(KEY)["refund_observation_id"]
    if change=="expired":app._refund_observations[token]["stored_at"]-=601
    if change=="wrong_case":app._refund_observations[token]["key"]="b"*32
    if change=="wrong_identity":app._refund_observations[token]["identity"]=("wrong",)
    if change=="missing":token="missing"
    raw=path.read_bytes()
    with pytest.raises(CaseError):app.action(KEY,"refund-reply-review",{"refund_observation_id":token})
    assert path.read_bytes()==raw and not app.working_drafts.directory.exists()


@pytest.mark.parametrize("action",["reply-draft","merchant-reply"])
def test_older_reply_action_requires_acknowledging_current_saved_review(desk,monkeypatch,action):
    app,path,case,gateway=desk
    text="Our saved record still says the refund is pending."
    reply={"capture":case["capture_id"],"version":json.dumps([case["capture_id"],case["state"],540,case["refund_id"],len(case["history"])]),"text":text}
    app.working_drafts.save(path,{"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":reply}})
    observed=app.inspect_refunds(KEY)
    work=app.action(KEY,"refund-reply-review",{"refund_observation_id":observed["refund_observation_id"]})["working_draft"]
    raw=path.read_bytes();rendered=[]
    monkeypatch.setattr("returnlight_app.returnlight_web.render_reply_email",lambda *a,**k:rendered.append(a[1]["text"]) or b"Unsent checked email")
    payload={"text":text,"confirmed_sent":True,"record_id":"f"*32,"case_revision":len(case["history"])}
    for version in [None,"0"*32]:
        with pytest.raises(CaseChanged,match="newer refund check"):
            app.action(KEY,action,{**payload,"reviewed_work_version":version})
    assert path.read_bytes()==raw and not rendered
    result=app.action(KEY,action,{**payload,"reviewed_work_version":work["version"]})
    if action=="reply-draft":
        assert rendered==[text] and "email_base64" in result and path.read_bytes()==raw
    else:
        assert result["case"]["merchant_replies"][-1]["text"]==text
    assert len(gateway.calls)==1

@pytest.mark.parametrize("boundary", ["current", "expired", "wrong_case", "wrong_identity", "unmarked"])
def test_reply_review_reveals_only_existing_current_checked_facts(desk, boundary):
    app,path,case,gateway=desk
    reply={"capture":case["capture_id"],"version":"saved","text":"Our saved record says pending."}
    app.working_drafts.save(path,{"case_revision":len(case["history"]),"draft_version":None,"draft":{"decision":None,"reply":reply}})
    observed=app.inspect_refunds(KEY)
    token=observed["refund_observation_id"]
    app.action(KEY,"refund-reply-review",{"refund_observation_id":token})
    cached=app._refund_observations[token]
    if boundary=="expired":cached["stored_at"]-=601
    if boundary=="wrong_case":cached["key"]="b"*32
    if boundary=="wrong_identity":cached["identity"]=("other",)
    if boundary=="unmarked":cached.pop("reply_review_done")
    raw=path.read_bytes()
    work=app.working_drafts.detail(path)[1]
    detail=app.detail(KEY)
    if boundary=="current":
        assert detail["reply_review_observation"]==cached["observation"]
        detail["reply_review_observation"]["status"]="MUTATED"
        assert app.detail(KEY)["reply_review_observation"]["status"]=="COMPLETED"
    else:
        assert detail["reply_review_observation"] is None
    assert path.read_bytes()==raw
    assert app.working_drafts.detail(path)[1]==work
    assert LocalCases(path.parent).detail(KEY)["reply_review_observation"] is None
    assert len(gateway.calls)==1
