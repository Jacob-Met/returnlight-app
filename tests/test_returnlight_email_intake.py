from email.message import EmailMessage
import pytest
from returnlight_app.email_input import html_email_text
from returnlight_app.returnlight_intake import import_buyer_email

def email(body):
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["Subject"] = "Replacement for café order"
    message.set_content(body)
    return message

def test_decodes_plain_body_and_keeps_complete_conversation():
    body = "Please replace the broken lid, not the kettle.\nPayPal capture ID: AB1234567890\nEarlier: I initially requested a refund."
    message = email(body)
    message.add_alternative("<p>Do not substitute this summary</p>", subtype="html")
    result = import_buyer_email(message.as_string())
    assert result["complaint"] == body
    assert result["capture_id"] == "AB1234567890"
    assert result["subject"] == "Replacement for café order"
    assert "ceiling" not in result

def test_attachment_cannot_supply_capture_or_message():
    message = email("Please send a replacement for my broken lid.")
    message.add_attachment(b"Capture ID: ATTACH123456", maintype="text", subtype="plain", filename="order.txt")
    assert import_buyer_email(message.as_string())["capture_id"] == ""

def test_ambiguous_capture_references_require_manual_choice():
    message = email("Please review these two orders.\nCapture ID: FIRST123456\nCapture ID: SECOND123456")
    assert import_buyer_email(message.as_string())["capture_id"] == ""

@pytest.mark.parametrize("raw", [None, "", "plain text without email headers", "x"*60001])
def test_invalid_source_is_rejected(raw):
    with pytest.raises(ValueError):
        import_buyer_email(raw)

def test_html_only_is_readable_and_long_email_is_not_truncated():
    message = email("Short readable source for this buyer.")
    message.set_content("<p>Please help with my order.</p>", subtype="html")
    assert import_buyer_email(message.as_string())["complaint"] == "Please help with my order."
    with pytest.raises(ValueError):
        import_buyer_email(email("x"*15001).as_string())

def test_unknown_charset_is_an_editable_intake_error():
    raw = b'From: buyer@example.com\nSubject: Broken cup\nContent-Type: text/plain; charset=made-up-codec\n\nPlease replace the broken cup.'
    with pytest.raises(ValueError, match="unsupported text encoding"):
        import_buyer_email(raw)

def test_legacy_charset_bytes_are_preserved():
    raw = b'From: buyer@example.com\nSubject: Broken cup\nContent-Type: text/plain; charset=iso-8859-1\n\nPlease replace my caf\xe9 cup.'
    assert import_buyer_email(raw)["complaint"] == "Please replace my café cup."

def test_only_explicit_paypal_order_ids_prefill_order_lookup():
    message = email("Please replace my broken cup.\nOrder ID: SHOPORDER123\nPayPal order ID: PAYPALORDER456")
    result = import_buyer_email(message.as_bytes())
    assert result["order_id"] == "PAYPALORDER456"
    assert result["capture_id"] == ""
    message = email("Please replace my broken cup.\nPayPal order ID: PAYPALORDER456\nPayPal order ID: SECONDORDER789")
    assert import_buyer_email(message.as_bytes())["order_id"] == ""

def test_html_preserves_conditions_and_history_without_active_content():
    message = email("placeholder")
    message.set_content('<html><head><style>fake amount</style></head><body><p>Please replace the lid; <b>do not refund</b>.</p><p>PayPal order ID: ORDEREMAIL12345</p><div class="gmail_quote"><blockquote><p>Earlier I requested a full refund.</p></blockquote></div><script>sendMoney()</script><img src="https://example.com/tracker"></body></html>', subtype="html")
    result = import_buyer_email(message.as_bytes())
    assert result["complaint"] == "Please replace the lid; do not refund.\nPayPal order ID: ORDEREMAIL12345\n> Earlier I requested a full refund."
    assert result["order_id"] == "ORDEREMAIL12345"

def test_invoice_html_reader_keeps_its_existing_quote_policy():
    from returnlight_app.email_input import html_email_text
    source = '<p>Current replacement request.</p><blockquote>Older refund request.</blockquote><div class="gmail_quote">Quoted old price.</div>'
    assert html_email_text(source) == "Current replacement request."
    assert "Older refund request." in html_email_text(source, preserve_quotes=True)


def test_large_attachment_does_not_replace_or_inject_buyer_words():
    body = "Please replace the broken lid, not the kettle."
    message = email(body)
    message.add_attachment(b"Capture ID: ATTACH123456\n" * 10000,
                           maintype="text", subtype="plain", filename="receipt.txt")
    raw = message.as_bytes()
    assert len(raw) > 40000
    result = import_buyer_email(raw)
    assert result["complaint"] == body
    assert result["capture_id"] == ""
    assert result["attachments_omitted"] == 1


def test_photo_attachment_does_not_block_short_buyer_message():
    body = "Please replace the broken cup, not the entire set."
    message = email(body)
    message.add_attachment(b"photo data" * 120000, maintype="image", subtype="jpeg", filename="broken-cup.jpg")
    raw = message.as_bytes()
    assert len(raw) > 1000000
    result = import_buyer_email(raw)
    assert result["complaint"] == body
    assert result["capture_id"] == ""

def test_oversize_email_rejected_before_mime_parsing():
    from returnlight_app.returnlight_intake import MAX_EMAIL_BYTES
    with pytest.raises(ValueError, match="10 MB"):
        import_buyer_email(b"x" * (MAX_EMAIL_BYTES + 1))

@pytest.mark.parametrize("body", ["Cancel it.", "No", "✓"])
def test_short_buyer_reply_keeps_exact_words_and_reference(body):
    message = email(body)
    message["In-Reply-To"] = "<original@example.com>"
    result = import_buyer_email(message.as_bytes())
    assert result["complaint"] == body
    assert result["reply_references"] == ["<original@example.com>"]
    assert result["capture_id"] == ""

@pytest.mark.parametrize("body", ["", "   ", "x" * 15001])
def test_empty_or_oversize_buyer_reply_is_rejected(body):
    with pytest.raises(ValueError, match="1 to 15000"):
        import_buyer_email(email(body).as_bytes())

@pytest.mark.parametrize("quoted", [
    '<blockquote><p>Keep the bowl.</p><p>Replace <b>only</b> the lid.</p></blockquote>',
    '<div class="gmail_quote"><div>Keep the bowl.</div><div>Replace <b>only</b> the lid.</div></div>',
    '<div class="gmail_quote"><blockquote><p>Keep the bowl.</p><p>Replace <b>only</b> the lid.</p></blockquote></div>',
    '<div class="yahoo_quoted"><div>Keep the bowl.</div><div>Replace only the lid.</div></div>',
    '<div class="protonmail_quote"><div>Keep the bowl.</div><div>Replace only the lid.</div></div>',
])
def test_html_quote_boundaries_are_neutral_and_do_not_leak(quoted):
    html = '<p>My current request.</p>' + quoted + '<p>My final condition.</p>'
    assert html_email_text(html, preserve_quotes=True) == (
        "My current request.\n> Keep the bowl.\n> Replace only the lid.\nMy final condition."
    )
    assert html_email_text(html) == "My current request.\nMy final condition."


def test_nested_html_quotes_keep_depth_and_skip_active_content():
    html = '<p>Current.</p><blockquote>Quoted.<blockquote>Older.<script>hidden</script></blockquote>Quoted again.</blockquote><p>Current again.</p>'
    assert html_email_text(html, preserve_quotes=True) == "Current.\n> Quoted.\n> > Older.\n> Quoted again.\nCurrent again."


def test_quote_wrapper_inline_children_cannot_end_quote_early():
    html = '<div class="gmail_quote"><div><span>Quoted first.</span></div><div>Quoted second.</div></div><p>Current.</p>'
    assert html_email_text(html, preserve_quotes=True) == "> Quoted first.\n> Quoted second.\nCurrent."


@pytest.mark.parametrize("format", ["plain", "html"])
def test_quoted_payment_references_do_not_prefill_current_intake(format):
    message = email("Current request.\n> PayPal capture ID: OLDCAPTURE123\n> PayPal order ID: OLDORDER12345")
    if format == "html":
        message.set_content('<p>Current request.</p><blockquote><p>PayPal capture ID: OLDCAPTURE123</p><p>PayPal order ID: OLDORDER12345</p></blockquote>', subtype="html")
    result = import_buyer_email(message.as_bytes())
    assert result["capture_id"] == result["order_id"] == ""
    assert "OLDCAPTURE123" in result["complaint"] and "OLDORDER12345" in result["complaint"]


def test_current_payment_reference_stays_usable_beside_quoted_other_order():
    message = email("placeholder")
    message.set_content('<p>Please replace this lid.</p><p>PayPal capture ID: CURRENTCAP123</p><p>PayPal order ID: CURRENTORDER123</p><blockquote><p>PayPal capture ID: OLDCAPTURE123</p><p>PayPal order ID: OLDORDER12345</p></blockquote>', subtype="html")
    result = import_buyer_email(message.as_bytes())
    assert result["capture_id"] == "CURRENTCAP123"
    assert result["order_id"] == "CURRENTORDER123"


def test_complete_substantive_exported_thread_is_preserved():
    import pytest; pytest.skip("uses a retained sample file that is not part of this repository")
    from pathlib import Path
    body = (Path(__file__).resolve().parents[1] / "evidence/returnlight-email-context/long-thread.txt").read_text().strip()
    assert 5000 < len(body) < 15000
    result = import_buyer_email(email(body).as_bytes())
    assert result["complaint"] == body
    assert result["capture_id"] == "LONGEMAIL12345"
    assert result["complaint"].endswith("or close the case as completed.")


def test_html_link_keeps_label_and_original_web_destination():
    message = email("placeholder")
    message.set_content('<p>Please <a href="https://tracking.example.com/parcel/RETURN123?x=1&amp;y=2"><b>track</b> my return</a> before closing.</p>', subtype="html")
    assert import_buyer_email(message.as_bytes())["complaint"] == "Please track my return (https://tracking.example.com/parcel/RETURN123?x=1&y=2) before closing."


def test_quoted_tracking_link_stays_quoted_without_duplicate_visible_url():
    source = '<blockquote><p><a href="https://tracking.example.com/RETURN123">https://tracking.example.com/RETURN123</a></p><p><a href="https://tracking.example.com/OLD123">Earlier parcel</a></p></blockquote><p>Current.</p>'
    assert html_email_text(source, preserve_quotes=True, preserve_links=True) == "> https://tracking.example.com/RETURN123\n> Earlier parcel (https://tracking.example.com/OLD123)\nCurrent."
    assert html_email_text(source) == "Current."


@pytest.mark.parametrize("target", ["javascript:alert(1)", "data:text/html,hello", "/relative/RETURN123", "//tracking.example.com/RETURN123", "mailto:buyer@example.com", "https://[broken", "https://example.com/a b"])
def test_unsupported_link_target_keeps_its_label(target):
    source = '<p>Read <a href="' + target + '">the buyer reference</a> carefully.</p>'
    assert html_email_text(source, preserve_links=True) == "Read the buyer reference carefully."


def test_link_opt_in_does_not_change_invoice_reader_or_fetch_hidden_targets():
    source = '<p>Track <a href="https://tracking.example.com/RETURN123">parcel</a>.</p><script><a href="https://hidden.example.com">hidden</a></script>'
    assert html_email_text(source) == "Track parcel."
    assert html_email_text(source, preserve_links=True) == "Track parcel (https://tracking.example.com/RETURN123)."


def test_unclosed_or_unlabelled_web_link_keeps_destination_once():
    assert html_email_text('<p>Track <a href="https://tracking.example.com/RETURN123">parcel</p>', preserve_links=True) == "Track parcel (https://tracking.example.com/RETURN123)"
    assert html_email_text('<a href="https://tracking.example.com/RETURN123">', preserve_links=True) == "https://tracking.example.com/RETURN123"


def test_web_target_trims_only_surrounding_html_whitespace():
    assert html_email_text('<a href="  https://tracking.example.com/RETURN123  ">parcel</a>', preserve_links=True) == "parcel (https://tracking.example.com/RETURN123)"


def test_linked_current_payment_ids_prefill_from_labels_not_destinations():
    message = email("placeholder")
    message.set_content('<p>PayPal capture ID: <a href="https://payments.example.com/receipt/OTHERPAYMENT123">CURRENTCAP123</a></p><p>PayPal order ID: <a href="https://payments.example.com/order/OTHERORDER123">CURRENTORDER123</a></p><blockquote>PayPal capture ID: <a href="https://payments.example.com/old">OLDCAPTURE123</a></blockquote>', subtype="html")
    result = import_buyer_email(message.as_bytes())
    assert result["capture_id"] == "CURRENTCAP123"
    assert result["order_id"] == "CURRENTORDER123"
    assert "OTHERPAYMENT123" in result["complaint"]
    assert "> PayPal capture ID: OLDCAPTURE123" in result["complaint"]


def test_link_destination_cannot_supply_an_explicit_payment_reference():
    message = email("placeholder")
    message.set_content('<p>See <a href="https://payments.example.com/receipt/CAPTURE12345">my receipt</a>.</p>', subtype="html")
    result = import_buyer_email(message.as_bytes())
    assert result["capture_id"] == result["order_id"] == ""
    assert "https://payments.example.com/receipt/CAPTURE12345" in result["complaint"]


@pytest.mark.parametrize("value", ["Fri, 6 Nov 2026 09:15:00 -0500", "Fri, 6 Nov 2026 09:15:00 -0000", "Fri, 6 Nov 2026 14:15:00 +0000"])
def test_original_date_text_and_zone_are_retained_without_relabeling_as_receipt(value):
    raw = ("From: Buyer <buyer@example.com>\r\nSubject: Older message\r\nDate: " + value + "\r\n\r\nKeep the original request.").encode()
    result = import_buyer_email(raw)
    assert result["date"] == value
    assert result["complaint"] == "Keep the original request."
    assert not {"received_at", "sent_at", "recorded_at"} & result.keys()


@pytest.mark.parametrize("headers", ["", "Date: not a date\r\n", "Date: Fri, 6 Nov 2026 09:15:00 -0500\r\nDate: Sat, 7 Nov 2026 09:15:00 -0500\r\n"])
def test_absent_invalid_or_ambiguous_date_does_not_block_buyer_words(headers):
    raw = ("From: buyer@example.com\r\nSubject: Help\r\n" + headers + "\r\nKeep the request.").encode()
    result = import_buyer_email(raw)
    assert "date" not in result
    assert result["complaint"] == "Keep the request."


def test_date_folding_is_unfolded_without_changing_offset():
    raw = b"From: buyer@example.com\r\nSubject: Help\r\nDate: Fri, 6 Nov 2026\r\n 09:15:00 -0500\r\n\r\nKeep the request."
    assert import_buyer_email(raw)["date"] == "Fri, 6 Nov 2026 09:15:00 -0500"


@pytest.mark.parametrize("date", ["", 123, "x" * 999, "Fri, 6 Nov\n2026"])
def test_stored_date_context_uses_the_existing_single_line_header_boundary(date):
    from returnlight_app.returnlight import _validate_imported_email, CaseError
    with pytest.raises(CaseError):
        _validate_imported_email({"sender": "buyer@example.com", "subject": "Help", "date": date})


def attached_offer(body="No return is required for that replacement."):
    earlier = EmailMessage()
    earlier["From"] = "Merchant <support@example.test>"
    earlier["Subject"] = "Replacement offer for R1042"
    earlier["Date"] = "Tue, 10 Nov 2026 09:00:00 -0800"
    earlier.set_content(body)
    return earlier


def test_attached_email_keeps_quoted_condition_and_outer_identity():
    outer = email("The replacement offer was withdrawn. Please refund instead.\nCapture ID: CURRENTCAP123")
    outer["Date"] = "Wed, 11 Nov 2026 10:00:00 -0800"
    outer["Message-ID"] = "<current@example.test>"
    outer.add_attachment(attached_offer(), filename="earlier.eml")
    result = import_buyer_email(outer.as_bytes())
    assert result["sender"] == "Buyer <buyer@example.com>"
    assert result["subject"] == "Replacement for café order"
    assert result["date"] == "Wed, 11 Nov 2026 10:00:00 -0800"
    assert result["capture_id"] == "CURRENTCAP123"
    assert result["attachments_omitted"] == 0
    assert result["complaint"].startswith("The replacement offer was withdrawn.")
    assert "Attached email 1 (quoted source)\n> From: Merchant <support@example.test>\n> Date: Tue, 10 Nov 2026 09:00:00 -0800\n> Subject: Replacement offer for R1042\n>\n> No return is required for that replacement." in result["complaint"]


def test_attached_payment_references_never_prefill_current_payment():
    outer = email("Please review the attached earlier offer.")
    outer.add_attachment(attached_offer("Capture ID: OLDCAPTURE123\nPayPal order ID: OLDORDER123"), filename="earlier.eml")
    result = import_buyer_email(outer.as_bytes())
    assert result["capture_id"] == result["order_id"] == ""
    assert "> Capture ID: OLDCAPTURE123" in result["complaint"]


def test_attached_html_keeps_nested_quote_and_safe_link_without_active_content():
    earlier = attached_offer()
    earlier.set_content('<p>Read <a href="https://example.test/terms">replacement terms</a>.</p><blockquote>Earlier offer only.</blockquote><script>sendMoney()</script>', subtype="html")
    outer = email("The attached terms need checking.")
    outer.add_attachment(earlier, filename="terms.eml")
    words = import_buyer_email(outer.as_bytes())["complaint"]
    assert "> Read replacement terms (https://example.test/terms)." in words
    assert "> > Earlier offer only." in words
    assert "sendMoney" not in words


def test_nested_attached_email_words_appear_once_and_plain_alternative_wins():
    earlier = attached_offer("First merchant terms.")
    earlier.add_alternative("<p>Alternative not selected.</p>", subtype="html")
    earlier.add_attachment(attached_offer("Second merchant terms."), filename="older.eml")
    outer = email("Please compare both earlier messages.")
    outer.add_attachment(earlier, filename="earlier.eml")
    result = import_buyer_email(outer.as_bytes())
    assert result["complaint"].count("First merchant terms.") == 1
    assert result["complaint"].count("Second merchant terms.") == 1
    assert "Alternative not selected." not in result["complaint"]
    assert "Attached email 2 (quoted source)" in result["complaint"]
    assert result["attachments_omitted"] == 0


def test_combined_attached_email_limit_rejects_before_photo_storage():
    outer = email("Please check the complete conversation.")
    outer.add_attachment(b"photo", maintype="image", subtype="jpeg", filename="broken.jpg")
    outer.add_attachment(attached_offer("x" * 14900), filename="earlier.eml")
    photos = []
    with pytest.raises(ValueError, match="complete email has not been imported"):
        import_buyer_email(outer.as_bytes(), store_photo=lambda *a, **k: photos.append(a))
    assert not photos


def test_unreadable_attached_message_is_still_counted_as_omitted():
    earlier = attached_offer()
    earlier.set_param("charset", "unsupported-codec")
    outer = email("Please inspect the earlier offer.")
    outer.add_attachment(earlier, filename="earlier.eml")
    result = import_buyer_email(outer.as_bytes())
    assert result["complaint"] == "Please inspect the earlier offer."
    assert result["attachments_omitted"] == 1
