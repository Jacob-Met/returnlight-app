import pytest
from returnlight_app.returnlight import new_case, close_case, reopen_case, update_merchant_followup, case_snapshot, CaseChanged, CaseError
from returnlight_app.returnlight_web import LocalCases
from returnlight_app.returnlight_record import render_case_record

NOTE = "Await the buyer's measurements, then arrange the replacement lid."
DONE = "Buyer confirmed the replacement fits and no further work is needed."

def create(cases):
    row = cases.create({"capture_id":"FOLLOWUPCAP123","ceiling":"24.40","complaint":"The replacement lid does not fit; please send the right size."})
    c = row["case"]
    return row["key"], cases.action(row["key"],"close",{"reason":NOTE,"confirmation":"CLOSE "+c["id"][:8],"case_revision":len(c["history"]),"followup_needed":True})["case"]

def test_commitment_survives_restart_and_completion_preserves_payment(tmp_path):
    cases=LocalCases(tmp_path);key,c=create(cases)
    assert c["state"]=="CLOSED" and cases.summary(key)["queue"]=="followup"
    assert cases.summary(key)["merchant_followup_note"]==NOTE
    assert LocalCases(tmp_path).detail(key)["case"]["merchant_followup"]["note"]==NOTE
    before={k:c[k] for k in ("state","resolution","approved_cents","request_id","refund_id","capture_id")}
    finished=cases.action(key,"merchant-followup",{"note":DONE,"completed":True,"case_revision":len(c["history"])})["case"]
    assert {k:finished[k] for k in before}==before
    assert cases.summary(key)["queue"]=="finished"
    assert not finished["merchant_followup"]["open"]
    record=render_case_record(finished)
    assert NOTE.replace("'","&#x27;") in record and DONE in record
    assert "Agreed work completed" in record

def test_stale_tab_and_changed_buyer_evidence_cannot_complete_old_commitment(tmp_path):
    cases=LocalCases(tmp_path);key,c=create(cases)
    changed=update_merchant_followup(cases.path(key),"Still waiting for the buyer to confirm the lid diameter.",False,len(c["history"]))
    with pytest.raises(CaseChanged):update_merchant_followup(cases.path(key),DONE,True,len(c["history"]))
    current=reopen_case(cases.path(key),"The replacement arrived broken and I now need a different remedy.",len(changed["history"]))
    assert cases.summary(key)["queue"]=="decide"
    assert cases.summary(key)["merchant_followup_open"]
    assert cases.summary(key)["merchant_followup_needs_review"]
    with pytest.raises(CaseError):update_merchant_followup(cases.path(key),DONE,True,len(current["history"]))
    assert case_snapshot(cases.path(key))==current
    closed=close_case(cases.path(key),"We agreed to another replacement without refund.","CLOSE "+current["id"][:8],expected_case_revision=len(current["history"]))
    assert closed["merchant_followup"]["open"] and cases.summary(key)["queue"]=="followup"

@pytest.mark.parametrize("note,done", [("short",True),("x"*1001,False),(NOTE,1)])
def test_invalid_update_has_no_effect(tmp_path,note,done):
    cases=LocalCases(tmp_path);key,c=create(cases);before=cases.path(key).read_bytes()
    with pytest.raises(CaseError):update_merchant_followup(cases.path(key),note,done,len(c["history"]))
    assert cases.path(key).read_bytes()==before

def test_saved_followup_choice_is_only_unfinished_work_until_explicit_closure(tmp_path):
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"FOLLOWUPCAP123","ceiling":"24.40","complaint":"Please replace only the damaged lid, without refund."})
    key,c=row["key"],row["case"]
    raw=cases.path(key).read_bytes()
    decision={"capture":c["capture_id"],"reason":NOTE,"amount":"","revision":len(c["history"]),"active":"reason","followup_needed":True}
    cases.action(key,"working-draft",{"case_revision":len(c["history"]),"draft_version":None,"draft":{"decision":decision,"reply":None}})
    detail=LocalCases(tmp_path).detail(key)
    assert detail["working_draft"]["draft"]["decision"]["followup_needed"] is True
    assert cases.path(key).read_bytes()==raw
    closed=cases.action(key,"close",{"reason":NOTE,"confirmation":"CLOSE "+c["id"][:8],"case_revision":len(c["history"]),"followup_needed":True})["case"]
    assert closed["merchant_followup"]["open"]
    assert cases.detail(key)["working_draft"]["draft"]["decision"] is None

@pytest.mark.parametrize("state", ["UNKNOWN", "PENDING", "COMPLETED", "FAILED"])
def test_returning_buyer_next_step_preserves_question_and_payment(tmp_path,state):
    import json
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"FOLLOWUPCAP123","ceiling":"24.40","complaint":"The spoon rest arrived cracked."})
    key,c=row["key"],row["case"]
    c.update(state=state,request_id="original-request",refund_id="original-refund",approved_cents=960,
             post_payment_messages=[{"text":"The refund is absent. Do I contact my bank?","recorded_at":c["history"][-1]["recorded_at"]}],
             support_followup_open=True)
    cases.path(key).write_text(json.dumps(c))
    note="Bank receipt remains unconfirmed. Check the original reference on Friday."
    updated=update_merchant_followup(cases.path(key),note,False,len(c["history"]))
    for field in ("state","request_id","refund_id","approved_cents","post_payment_messages","support_followup_open"):
        assert updated[field]==c[field]
    assert not updated.get("merchant_replies") and not updated.get("support_resolutions")
    assert LocalCases(tmp_path).summary(key)["merchant_followup_note"]==note
    assert LocalCases(tmp_path).summary(key)["queue"]=="followup"
    assert note in render_case_record(updated)
    from returnlight_app.returnlight import resolve_post_payment_support
    resolved=resolve_post_payment_support(cases.path(key),"Buyer question answered; Friday follow-up still promised.",len(updated["history"]))
    assert cases.summary(key)["queue"]=="followup"
    assert resolved["merchant_followup"]["open"]
    # Reopen the authored support question for the independent completion check.
    cases.path(key).write_text(json.dumps(updated))
    with pytest.raises(CaseChanged):
        update_merchant_followup(cases.path(key),"Another window must not overwrite this step.",False,len(c["history"]))
    done=update_merchant_followup(cases.path(key),"Reference checked; buyer receipt still needs confirmation.",True,len(updated["history"]))
    assert done["support_followup_open"] and not done["merchant_followup"]["open"]
    assert cases.summary(key)["queue"]=="followup"

def test_new_buyer_message_marks_earlier_support_plan_for_review(tmp_path):
    import json
    from returnlight_app.returnlight import add_post_payment_message, merchant_followup_needs_review
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"FOLLOWUPCAP123","ceiling":"24.40","complaint":"The spoon rest arrived cracked."})
    key,c=row["key"],row["case"]
    c.update(state="COMPLETED",request_id="original-request",refund_id="original-refund",approved_cents=960)
    cases.path(key).write_text(json.dumps(c))
    c=add_post_payment_message(cases.path(key),"Please check the original refund on Friday.",len(c["history"]))
    c=update_merchant_followup(cases.path(key),"Check the original refund reference on Friday.",False,len(c["history"]))
    assert not merchant_followup_needs_review(c)
    c=add_post_payment_message(cases.path(key),"Stop the Friday check. I only need an itemized receipt.",len(c["history"]))
    assert merchant_followup_needs_review(c)
    assert cases.summary(key)["merchant_followup_needs_review"]
    assert "Earlier next step" in render_case_record(c)
    assert c["merchant_followup"]["open"]
    assert "Friday" in c["merchant_followup"]["note"]
    c=update_merchant_followup(cases.path(key),"Obtain the original itemized receipt and reply to the buyer.",False,len(c["history"]))
    assert not merchant_followup_needs_review(c)
    assert not cases.summary(key)["merchant_followup_needs_review"]
    assert c["support_followup_open"]

@pytest.mark.parametrize("state", ["OPEN", "REVIEWED", "APPROVED"])
def test_pre_payment_next_step_tracks_work_without_deciding_case(tmp_path, state):
    import json
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"SHOESCAPTURE123","ceiling":"49.00",
                      "complaint":"Refund only the shoes. Can the driver bring the prepaid label?"})
    key,c=row["key"],row["case"]
    if state in {"REVIEWED","APPROVED"}:
        c=cases.action(key,"human-review",{"case_revision":len(c["history"]),"quote":c["complaint"],"confirmed":True})["case"]
    if state=="APPROVED":c.update(state=state,approved_cents=4900)
    cases.path(key).write_text(json.dumps(c))
    note="Check whether the driver can bring the prepaid return label."
    saved=cases.action(key,"merchant-followup",{"note":note,"completed":False,"case_revision":len(c["history"])})["case"]
    for field in ("state","approved_cents","request_id","refund_id","review","complaint"):
        assert saved.get(field)==c.get(field)
    assert not saved.get("resolution") and not saved.get("merchant_replies")
    assert LocalCases(tmp_path).summary(key)["queue"]=="followup"
    assert LocalCases(tmp_path).detail(key)["case"]["merchant_followup"]["note"]==note
    record=render_case_record(saved)
    assert "Merchant next step" in record and note in record
    raw=cases.path(key).read_bytes()
    with pytest.raises(CaseChanged):
        update_merchant_followup(cases.path(key),DONE,True,len(c["history"]))
    assert cases.path(key).read_bytes()==raw
    finished=update_merchant_followup(cases.path(key),"Courier confirmed label availability; collection is not booked.",True,len(saved["history"]))
    for field in ("state","approved_cents","request_id","refund_id","review","complaint"):
        assert finished.get(field)==c.get(field)
    assert cases.summary(key)["queue"]==("followup" if state=="APPROVED" else "decide")
    assert not finished.get("resolution") and not finished.get("merchant_replies")

def test_new_pre_payment_message_requires_reconsidering_old_step(tmp_path):
    from returnlight_app.returnlight import add_buyer_followup, merchant_followup_needs_review
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"SHOESCAPTURE123","ceiling":"49.00","complaint":"Can the driver bring the label?"})
    key,c=row["key"],row["case"]
    c=update_merchant_followup(cases.path(key),"Check whether the driver can bring the label.",False,len(c["history"]))
    c=add_buyer_followup(cases.path(key),"Do not book collection. I will drop the shoes off instead.",len(c["history"]))
    assert merchant_followup_needs_review(c)
    assert cases.summary(key)["queue"]=="decide"
    assert "Earlier next step" in render_case_record(c)
    before=cases.path(key).read_bytes()
    with pytest.raises(CaseError,match="newer message"):
        update_merchant_followup(cases.path(key),DONE,True,len(c["history"]))
    assert cases.path(key).read_bytes()==before
    c=update_merchant_followup(cases.path(key),"Check the drop-off label requirements; do not book collection.",False,len(c["history"]))
    assert not merchant_followup_needs_review(c) and cases.summary(key)["queue"]=="followup"
    c=update_merchant_followup(cases.path(key),"Drop-off label requirements checked; buyer still needs a reply.",True,len(c["history"]))
    assert c["state"]=="OPEN" and cases.summary(key)["queue"]=="decide"

@pytest.mark.parametrize("state",["UNKNOWN","PENDING","COMPLETED","FAILED"])
def test_existing_pre_payment_work_remains_editable_after_attempt(tmp_path,state):
    import json
    cases=LocalCases(tmp_path)
    row=cases.create({"capture_id":"SHOESCAPTURE123","ceiling":"49.00","complaint":"Please check the return label."})
    key,c=row["key"],row["case"]
    c=update_merchant_followup(cases.path(key),"Check the prepaid label before arranging collection.",False,len(c["history"]))
    c.update(state=state,request_id="existing-request",approved_cents=4900)
    cases.path(key).write_text(json.dumps(c))
    updated=update_merchant_followup(cases.path(key),"Courier confirmed a prepaid label; collection is not booked.",True,len(c["history"]))
    for field in ("state","request_id","refund_id","approved_cents"):
        assert updated[field]==c[field]
    assert not updated.get("support_resolutions") and not updated.get("merchant_replies")


def test_internal_completed_work_is_exact_review_source_after_restart(tmp_path):
    from returnlight_app.returnlight import human_review_case, merchant_work_sources, condition_source
    cases = LocalCases(tmp_path)
    row = cases.create({"capture_id":"WORKNOTE123", "ceiling":"49.00",
                       "complaint":"Do I need to print the prepaid return label?"})
    key, c = row["key"], row["case"]
    c = update_merchant_followup(cases.path(key), "Check whether the driver brings the prepaid label.", False, len(c["history"]))
    words = "Courier confirmed 📦 the driver can bring the prepaid label. Collection is not booked."
    c = update_merchant_followup(cases.path(key), words, True, len(c["history"]))
    seq = merchant_work_sources(c)[-1]["seq"]
    kept = {"work_seq":seq,"source_start":0,"quote":words}
    cases.action(key,"working-draft",{"case_revision":len(c["history"]),"draft_version":None,
                 "draft":{"decision":None,"reply":None,"conditions":{"capture":c["capture_id"],"revision":len(c["history"]),"items":[kept]}}})
    restored = LocalCases(tmp_path).detail(key)
    assert restored["working_draft"]["draft"]["conditions"]["items"] == [kept]
    reviewed = human_review_case(cases.path(key), c["complaint"], True,
                                expected_case_revision=len(c["history"]), selected_conditions=[kept])
    assert reviewed["state"] == "REVIEWED" and reviewed["approved_cents"] == 0
    assert not reviewed["request_id"] and not reviewed["refund_id"]
    assert condition_source(reviewed,kept) == (("work",seq),words)
    record = render_case_record(reviewed)
    assert "Internal merchant work note" in record
    assert 'href="#kept-source-0"' in record and 'id="kept-source-0"' in record
    assert 'data-work-source="'+str(seq)+'"' in record
    assert " · Internal merchant note</p>" in record

def test_work_source_keeps_original_identity_after_later_note_and_buyer_change(tmp_path):
    from returnlight_app.returnlight import checked_source_conditions, condition_source, merchant_work_sources, add_buyer_followup
    cases = LocalCases(tmp_path)
    row = cases.create({"capture_id":"WORKNOTE123","ceiling":"49.00","complaint":"Please arrange a prepaid return."})
    key,c = row["key"],row["case"]
    old = "Check whether the collection driver brings the label."
    c=update_merchant_followup(cases.path(key),old,False,len(c["history"]))
    seq=merchant_work_sources(c)[-1]["seq"];kept={"work_seq":seq,"source_start":0,"quote":old}
    c=add_buyer_followup(cases.path(key),"Please do not book collection. I will use the drop-off point.",len(c["history"]))
    c=update_merchant_followup(cases.path(key),"Check the prepaid drop-off label requirements instead.",False,len(c["history"]))
    assert condition_source(c,kept)==(("work",seq),old)
    assert checked_source_conditions(c,[kept])==[kept]
    for forged in [
        {"work_seq":1,"source_start":0,"quote":c["complaint"]},
        {"work_seq":True,"source_start":0,"quote":old},
        {"work_seq":seq,"source_start":1,"quote":old},
    ]:
        with pytest.raises(CaseError):checked_source_conditions(c,[forged])
    assert c["state"]=="OPEN" and not c["approved_cents"]


def test_search_retains_original_and_revised_work_after_completion_and_restart(tmp_path):
    cases = LocalCases(tmp_path)
    row = cases.create({"capture_id": "WORKSEARCH123", "ceiling": "24.40",
                        "complaint": "Please check the missing item."})
    key, case = row["key"], row["case"]
    notes = [
        ("Check the available original document for the extra amount.", False),
        ("Ask the archive clerk for the violet parcel reference.", False),
        ("Archive check completed; the breakdown remains unavailable.", True),
    ]
    for note, completed in notes:
        case = update_merchant_followup(cases.path(key), note, completed, len(case["history"]))
    before = cases.path(key).read_bytes()
    summary = LocalCases(tmp_path).summary(key)
    for note, _ in notes[:-1]:
        assert note in [entry["text"] for entry in summary["merchant_work_history"]]
    assert notes[-1][0] not in summary["complaint"]
    assert summary["merchant_followup_note"] == notes[-1][0]
    assert not summary["merchant_followup_open"]
    assert summary["queue"] == "decide"
    assert cases.path(key).read_bytes() == before
