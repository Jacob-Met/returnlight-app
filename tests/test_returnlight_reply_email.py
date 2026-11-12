import base64
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import pytest
from returnlight_app.returnlight import CaseError, CaseChanged, new_case, add_buyer_followup, case_snapshot
from returnlight_app.returnlight_intake import import_buyer_email
from returnlight_app.returnlight_record import reply_email_choices, render_reply_email, render_case_record
from returnlight_app.returnlight_web import LocalCases

WORDS = "The pitcher handle is broken. Please leave the matching cups with me."
REPLY = "Please keep the cups. I will confirm the return instructions for the pitcher."


def context(reply_to="Morgan <morgan@example.test>"):
    message = EmailMessage()
    message["From"] = "Shop relay <no-reply@shop.example.test>"
    message["Subject"] = "R3042 café pitcher"
    message["Message-ID"] = "<buyer-r3042@shop.example.test>"
    message["References"] = "<older@shop.example.test>"
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message.set_content(WORDS)
    imported = import_buyer_email(message.as_bytes())
    return {key: value for key, value in imported.items() if key not in {"complaint", "capture_id", "order_id"}}


def opened(tmp_path, reply_to="Morgan <morgan@example.test>"):
    path = tmp_path / ("a"*32 + ".json")
    return path, new_case(path, None, None, WORDS, payment_pending=True, imported_email=context(reply_to))


def request(case, recipient=None):
    choice = reply_email_choices(case)[0]
    return {"version": json.dumps([case["capture_id"], case["state"], case["approved_cents"], case["refund_id"], len(case["history"])]),
            "text": REPLY, "confirmed": True, "source": choice["source"], "basis": choice["basis"],
            "recipient": recipient or choice["recipients"][0]["address"]}


def test_reply_to_stays_distinct_and_export_keeps_exact_thread(tmp_path):
    path, case = opened(tmp_path)
    before = path.read_bytes()
    assert case["imported_email"]["sender"] == "Shop relay <no-reply@shop.example.test>"
    assert case["imported_email"]["reply_to"] == "Morgan <morgan@example.test>"
    assert "morgan@example.test" in render_case_record(case)
    exported = render_reply_email(case, request(case))
    message = BytesParser(policy=policy.default).parsebytes(exported)
    assert message["To"].addresses[0].addr_spec == "morgan@example.test"
    assert str(message["Subject"]) == "Re: R3042 café pitcher"
    assert str(message["In-Reply-To"]) == "<buyer-r3042@shop.example.test>"
    assert str(message["References"]) == "<older@shop.example.test> <buyer-r3042@shop.example.test>"
    assert message["X-Unsent"] == "1"
    assert message.get_content().rstrip("\r\n") == REPLY
    assert not any(message.get(header) for header in ["From", "Bcc", "Cc", "Date", "Message-ID"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("header", ["", "not an address", "Morgan <morgan@example.test>, broken-address"])
def test_explicit_unusable_reply_to_never_falls_back_to_relay(tmp_path, header):
    path, case = opened(tmp_path, header)
    assert "reply_to" in case_snapshot(path)["imported_email"]
    assert reply_email_choices(case)[0]["recipients"] == []


def test_sender_is_available_only_when_reply_to_absent(tmp_path):
    _, case = opened(tmp_path, None)
    assert reply_email_choices(case)[0]["recipients"][0]["address"] == "no-reply@shop.example.test"


def test_multiple_reply_addresses_require_one_explicit_recorded_choice(tmp_path):
    _, case = opened(tmp_path, "Morgan <morgan@example.test>, Buyer desk <buyerdesk@example.test>")
    assert len(reply_email_choices(case)[0]["recipients"]) == 2
    message = BytesParser(policy=policy.default).parsebytes(render_reply_email(case, request(case, "buyerdesk@example.test")))
    assert len(message["To"].addresses) == 1 and message["To"].addresses[0].addr_spec == "buyerdesk@example.test"
    with pytest.raises(CaseError, match="recorded reply address"):
        render_reply_email(case, request(case, "unrelated@example.test"))


def test_changed_case_and_unchecked_destination_reject_without_mutation(tmp_path):
    path, case = opened(tmp_path)
    payload = request(case)
    with pytest.raises(CaseError, match="Check the recipient"):
        render_reply_email(case, {**payload, "confirmed": False})
    changed = add_buyer_followup(path, "Please wait for my next message before agreeing any remedy.", len(case["history"]))
    before = path.read_bytes()
    with pytest.raises(CaseChanged):
        render_reply_email(changed, payload)
    assert path.read_bytes() == before


def test_latest_email_choices_keep_their_own_address_subject_and_reference(tmp_path):
    path, case = opened(tmp_path)
    newer = {**context("Morgan new <new-address@example.test>"), "subject": "Re: Updated pitcher collection", "message_id": "<new-message@example.test>"}
    case = add_buyer_followup(path, "Please use my new email address for this return.", len(case["history"]), imported_email=newer)
    options = reply_email_choices(case)
    assert options[0]["recipients"][0]["address"] == "new-address@example.test"
    assert options[1]["recipients"][0]["address"] == "morgan@example.test"
    message = BytesParser(policy=policy.default).parsebytes(render_reply_email(case, request(case)))
    assert str(message["In-Reply-To"]) == "<new-message@example.test>"
    assert str(message["Subject"]) == "Re: Updated pitcher collection"


def test_actual_local_action_is_read_only_and_has_no_provider(tmp_path):
    def forbidden(): raise AssertionError("No provider must be constructed")
    app = LocalCases(tmp_path, gateway_factory=forbidden)
    result = app.create({"capture_id":None,"ceiling":None,"complaint":WORDS,"payment_pending":True,"imported_email":context()})
    key, case = result["key"], result["case"]
    before = app.path(key).read_bytes()
    assert result["reply_email_choices"] == app.detail(key)["reply_email_choices"]
    exported = app.action(key, "reply-draft", request(case))
    assert exported["filename"].endswith(".eml")
    assert BytesParser(policy=policy.default).parsebytes(base64.b64decode(exported["email_base64"]))["X-Unsent"] == "1"
    assert app.path(key).read_bytes() == before


def document_case(tmp_path):
    observation = {"observed_at":"2026-11-11T19:00:00+00:00", "capture":{
        "capture_id":"DOCUMENTCAPTURE01", "original_cents":3050, "order_id":"DOCUMENTORDER01",
        "order":{"id":"DOCUMENTORDER01", "total_cents":3050, "items":[
            {"name":"Blue ceramic mug","quantity":1,"unit_cents":1250,"line_cents":1250},
            {"name":"Green stoneware bowl","quantity":1,"unit_cents":1800,"line_cents":1800}]}}}
    path=tmp_path/("b"*32+".json")
    return path,new_case(path,"DOCUMENTCAPTURE01","30.50",WORDS,
                         purchase_observation=observation, imported_email=context())


def test_checked_document_attaches_exact_allowlisted_bytes_without_mutation(tmp_path):
    import hashlib
    from returnlight_app.returnlight_record import render_purchase_details
    path,case=document_case(tmp_path);before=path.read_bytes()
    document=render_purchase_details(case).encode("utf-8")
    payload={**request(case),"include_purchase_details":True,
             "purchase_details_sha256":hashlib.sha256(document).hexdigest()}
    message=BytesParser(policy=policy.default).parsebytes(render_reply_email(case,payload))
    attachments=list(message.iter_attachments())
    assert len(attachments)==1 and attachments[0].get_filename()=="purchase-details.html"
    assert attachments[0].get_payload(decode=True)==document
    assert message.get_body(preferencelist=("plain",)).get_content().rstrip("\r\n")==REPLY
    assert WORDS.encode() not in document and b"DOCUMENTCAPTURE01" not in document
    assert path.read_bytes()==before
    assert not list(BytesParser(policy=policy.default).parsebytes(render_reply_email(case,request(case))).iter_attachments())


def test_missing_or_changed_document_check_rejects_attachment(tmp_path):
    import hashlib
    from returnlight_app.returnlight_record import render_purchase_details
    path,case=document_case(tmp_path);before=path.read_bytes()
    payload={**request(case),"include_purchase_details":True}
    with pytest.raises(CaseChanged,match="Purchase details changed"):
        render_reply_email(case,payload)
    payload["purchase_details_sha256"]=hashlib.sha256(render_purchase_details(case).encode()).hexdigest()
    case["purchase_context"]["order"]["items"][0]["name"]="Corrected blue ceramic mug"
    with pytest.raises(CaseChanged,match="Purchase details changed"):
        render_reply_email(case,payload)
    with pytest.raises(CaseError,match="Choose whether"):
        render_reply_email(case,{**request(case),"include_purchase_details":"yes"})
    assert path.read_bytes()==before


def test_original_document_email_uses_case_bound_file_and_rejects_replacement(tmp_path):
    app=LocalCases(tmp_path)
    created=app.create({"capture_id":None,"ceiling":None,"complaint":WORDS,"payment_pending":True,"imported_email":context()})
    key,case=created["key"],created["case"];before=app.path(key).read_bytes()
    original=b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
    work=app.action(key,"reply-document",{"case_revision":len(case["history"]),"draft_version":None,"filename":"merchant-original.pdf","data_base64":base64.b64encode(original).decode()})["working_draft"]
    reference=work["draft"]["reply_document"]
    payload={**request(case),"include_reply_document":True,"reply_document_id":reference["id"],"reply_document_sha256":reference["sha256"]}
    result=app.action(key,"reply-draft",payload)
    email=BytesParser(policy=policy.default).parsebytes(base64.b64decode(result["email_base64"]));parts=list(email.iter_attachments())
    assert len(parts)==1 and parts[0].get_filename()=="merchant-original.pdf" and parts[0].get_payload(decode=True)==original
    assert email.get_body(preferencelist=("plain",)).get_content().rstrip("\r\n")==REPLY
    assert str(email["In-Reply-To"])=="<buyer-r3042@shop.example.test>"
    plain=app.action(key,"reply-draft",request(case))
    assert not list(BytesParser(policy=policy.default).parsebytes(base64.b64decode(plain["email_base64"])).iter_attachments())
    app.action(key,"reply-document",{"case_revision":len(case["history"]),"draft_version":work["version"],"remove":reference["id"]})
    with pytest.raises(CaseChanged):app.action(key,"reply-draft",payload)
    with pytest.raises(CaseError,match="Choose whether"):
        render_reply_email(case,{**request(case),"include_reply_document":"yes"})
    assert app.path(key).read_bytes()==before


def test_recorded_documents_stay_with_reply_after_replacement_and_restart(tmp_path):
    import hashlib
    from returnlight_app.returnlight_record import render_purchase_details
    path, case = document_case(tmp_path)
    app = LocalCases(tmp_path)
    key = path.stem
    original = b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
    work = app.action(key, "reply-document", {"case_revision":len(case["history"]), "draft_version":None,
        "filename":"original-receipt.pdf", "data_base64":base64.b64encode(original).decode()})["working_draft"]
    reference = work["draft"]["reply_document"]
    summary = render_purchase_details(case).encode()
    payload = {"text":REPLY, "case_revision":len(case["history"]), "confirmed_sent":True, "record_id":"f"*32,
        "include_purchase_details":True, "purchase_details_sha256":hashlib.sha256(summary).hexdigest(),
        "include_reply_document":True, "reply_document_id":reference["id"], "reply_document_sha256":reference["sha256"]}
    before = path.read_bytes()
    with pytest.raises(CaseError, match="these exact checked documents"):
        app.action(key, "merchant-reply", payload)
    assert path.read_bytes() == before
    recorded = app.action(key, "merchant-reply", {**payload, "confirmed_documents":True})["case"]
    assert len(recorded["merchant_replies"]) == 1
    work = app.detail(key)["working_draft"]
    app.action(key, "reply-document", {"case_revision":len(recorded["history"]), "draft_version":work["version"],
        "filename":"later-receipt.pdf", "data_base64":base64.b64encode(original.replace(b"%%EOF", b"% later\n%%EOF")).decode()})
    recorded["purchase_context"]["order"]["items"][0]["name"] = "Later corrected mug name"
    path.write_text(json.dumps(recorded))
    reopened = LocalCases(tmp_path)
    assert reopened.read_recorded_reply_document(key, "f"*32, "0")[2] == summary
    assert reopened.read_recorded_reply_document(key, "f"*32, "1")[2] == original
    export = render_case_record(reopened.detail(key)["case"], photo_loader=reopened.photos.read)
    assert base64.b64encode(summary).decode() in export
    assert base64.b64encode(original).decode() in export
    assert "original-receipt.pdf" in export and "Saved purchase summary" in export
    # Reuse the exact earlier original through the existing current-document handoff.
    current_work = reopened.detail(key)["working_draft"]
    choose = {"case_revision":len(recorded["history"]), "draft_version":current_work["version"],
              "recorded_reply_id":"f"*32, "recorded_document_index":1}
    case_before = path.read_bytes()
    with pytest.raises(CaseChanged):
        reopened.action(key, "reply-document", {**choose, "draft_version":"0"*32})
    with pytest.raises(CaseError):
        reopened.action(key, "reply-document", {**choose, "recorded_reply_id":"e"*32})
    with pytest.raises(CaseError):
        reopened.action(key, "reply-document", {**choose, "recorded_document_index":0})
    restored = reopened.action(key, "reply-document", choose)["working_draft"]
    previous_original = restored["draft"]["reply_document"]
    assert previous_original == reference
    assert previous_original["name"] == "original-receipt.pdf"
    assert reopened.read_reply_document(key, previous_original["id"], previous_original["sha256"])[1] == original
    assert reopened.action(key, "reply-document", choose)["working_draft"]["version"] == restored["version"]
    assert path.read_bytes() == case_before
    assert any(item["name"] == "original-receipt.pdf" for item in reopened.summary(key)["recorded_documents"])
    assert LocalCases(tmp_path).detail(key)["working_draft"]["draft"]["reply_document"] == previous_original
    # A lost response remains recoverable even after the working document changes.
    again = reopened.action(key, "merchant-reply", {**payload, "confirmed_documents":True})["case"]
    assert len(again["merchant_replies"]) == 1
    with pytest.raises(CaseError, match="different documents"):
        reopened.action(key, "merchant-reply", {**payload, "confirmed_documents":True, "include_reply_document":False})
    with pytest.raises(CaseError):reopened.read_recorded_reply_document(key, "e"*32, "0")
    assert not any(event["event"].startswith("refund.") for event in again["history"])


def test_changed_original_cannot_be_recorded_as_the_checked_document(tmp_path):
    path, case = opened(tmp_path);app=LocalCases(tmp_path);key=path.stem
    original=b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
    work=app.action(key,"reply-document",{"case_revision":len(case["history"]),"draft_version":None,
        "filename":"original.pdf","data_base64":base64.b64encode(original).decode()})["working_draft"]
    reference=work["draft"]["reply_document"]
    app.action(key,"reply-document",{"case_revision":len(case["history"]),"draft_version":work["version"],"remove":reference["id"]})
    before=path.read_bytes()
    with pytest.raises(CaseChanged):
        app.action(key,"merchant-reply",{"text":REPLY,"case_revision":len(case["history"]),"confirmed_sent":True,
            "confirmed_documents":True,"record_id":"d"*32,"include_reply_document":True,
            "reply_document_id":reference["id"],"reply_document_sha256":reference["sha256"]})
    assert path.read_bytes()==before


def test_received_document_selection_is_case_bound_and_keeps_original_bytes(tmp_path):
    app = LocalCases(tmp_path)
    original = b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"
    reference = app.photos.put(original, "received-receipt.pdf")
    foreign = app.photos.put(original.replace(b"%%EOF", b"% foreign\n%%EOF"), "other-case.pdf")
    created = app.create({"capture_id":None, "ceiling":None, "complaint":WORDS,
                          "payment_pending":True, "imported_email":{**context(), "photos":[reference]}})
    key, case = created["key"], created["case"]
    before = app.path(key).read_bytes()
    selection = {"case_revision":len(case["history"]), "draft_version":None,
                 "received_document_id":reference["id"], "received_document_sha256":reference["sha256"]}
    for invalid in [{**selection, "received_document_id":foreign["id"], "received_document_sha256":foreign["sha256"]},
                    {**selection, "received_document_sha256":"0"*64}]:
        with pytest.raises(CaseError, match="saved message"):
            app.action(key, "reply-document", invalid)
    work = app.action(key, "reply-document", selection)["working_draft"]
    assert work["draft"]["reply_document"] == reference
    assert app.action(key, "reply-document", selection)["working_draft"]["version"] == work["version"]
    reopened = LocalCases(tmp_path)
    result = reopened.action(key, "reply-draft", {**request(case), "include_reply_document":True,
        "reply_document_id":reference["id"], "reply_document_sha256":reference["sha256"]})
    message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(result["email_base64"]))
    assert list(message.iter_attachments())[0].get_payload(decode=True) == original
    assert str(message["In-Reply-To"]) == "<buyer-r3042@shop.example.test>"
    assert app.path(key).read_bytes() == before
    saved = reopened.detail(key)["working_draft"]
    app.photos.path(reference["id"]).write_bytes(original.replace(b"Catalog", b"Changed"))
    with pytest.raises(CaseError, match="unavailable or changed"):
        reopened.action(key, "reply-document", {**selection, "draft_version":saved["version"]})
    assert reopened.detail(key)["working_draft"] == saved
    assert app.path(key).read_bytes() == before
