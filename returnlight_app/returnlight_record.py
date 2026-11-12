"""Readable, offline snapshots of an existing private merchant case."""
import base64
import json
from datetime import datetime, timezone
from html import escape
from .returnlight import condition_source, merchant_followup_needs_review, merchant_work_sources
from .returnlight_purchase_sources import purchase_sources

_OUTCOMES = {
    "OPEN": "Review needed", "REVIEWED": "Ready for a decision",
    "APPROVED": "Approved · not sent", "UNKNOWN": "Payment outcome unknown",
    "PENDING": "Refund pending", "COMPLETED": "Refund completed",
    "FAILED": "Refund failed", "CLOSED": "Closed · no refund",
}

def _text(value):
    return escape(str(value if value is not None else ""), quote=True)

def _money(cents):
    return f"USD {cents // 100}.{cents % 100:02d}"

def _recorded_time(value):
    if not isinstance(value, str):
        return "Time not recorded"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "Time not recorded"
        return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")
    except (ValueError, OverflowError):
        return "Time not recorded"

def _ai_source_link(source, links):
    target = (links or {}).get(id(source))
    if not target:
        return '<p class="meta">Original passage could not be located in this record.</p>'
    return '<p><a class="source-link ai-source-link" href="#' + target + '">Read original words</a></p>'


def _open_questions(review, earlier=False, ai_links=None):
    sources = review.get("sources", [])
    numbers = review.get("open_questions", [])
    if not numbers:
        return ""
    words = "".join(
        '<blockquote>' + _text(sources[number - 1].get("quote")) + '</blockquote>' + _ai_source_link(sources[number - 1], ai_links)
        for number in numbers if type(number) is int and 1 <= number <= len(sources))
    label = 'Questions from earlier AI reading' if earlier else 'Questions to resolve · AI reading'
    return '<h3>' + label + '</h3>' + words


def _selection(review, ai_links=None):
    if review.get("kind") == "conversation_brief":
        return "".join(f'<p class="meta">{_text(({"merchant": "Merchant reply ", "email": "Email message ", "purchase": "Saved purchase ", "work": "Internal merchant work note "}.get(source.get("speaker"), "Buyer message ")) + str(source.get("message_number")))}</p><p class="verbatim">{_text(source.get("quote"))}</p>' + _ai_source_link(source, ai_links) for source in review.get("sources", []))
    quote = review.get("quote", "")
    context = review.get("context") or quote
    if not isinstance(context, str) or not isinstance(quote, str) or quote not in context:
        return _text(quote)
    start = review.get("quote_start")
    context_start = review.get("context_start", 0)
    if type(start) is int and type(context_start) is int:
        start -= context_start
    if type(start) is int and start >= 0 and context[start:start + len(quote)] == quote:
        before, after = context[:start], context[start + len(quote):]
    else:
        before, _, after = context.partition(quote)
    return f'{_text(before)}<mark>{_text(quote)}</mark>{_text(after)}'



def _purchase_record(case, source_markup=None):
    purchase = case.get("purchase_context")
    if not purchase:
        return ""
    order = purchase.get("order", {})
    rows = "".join(
        f'<tr><th scope="row">{_text(item["name"])}'
        f'<span class="item-detail">{item["quantity"]} × {_money(item["unit_cents"])}</span></th>'
        f'<td>{_money(item["line_cents"])}</td></tr>'
        for item in order.get("items", []))
    items = (f'<table class="purchase-table" aria-label="Original purchase items">'
             f'<thead><tr><th>Item</th><th>Line total</th></tr></thead><tbody>{rows}</tbody></table>'
             if rows else "")
    reference = (f'<p class="reference">Order {_text(purchase["order_id"])}</p>'
                 if purchase.get("order_id") else "")
    description = (f'<p class="verbatim">{_text(order["description"])}</p>'
                   if order.get("description") else "")
    total = (f'<p class="purchase-total">Linked purchase total <strong>{_money(order["total_cents"])}</strong></p>'
             if order else "")
    from .returnlight_support_review import current_support_review
    support_reading = current_support_review(case) or {}
    cited_purchase = any(reading.get("purchase_sources") for reading in [case.get("review") or {}, case.get("ai_suggestion") or {}]) or any(source.get("kind") == "purchase" for source in support_reading.get("sources", []))
    if source_markup is not None and cited_purchase:
        words = purchase_sources(case)
        return '<section class="purchase"><h2>Purchase</h2>' + reference + ''.join(
            '<p class="verbatim">' + source_markup.get(("purchase", index + 1), _text(text)) + '</p>'
            for index, text in enumerate(words)) + description + '</section>'
    return f"""<section class="purchase"><h2>Purchase</h2>
<p>Original payment <strong>{_money(purchase["original_cents"])}</strong></p>
<p class="meta">Purchase checked {_text(_recorded_time(purchase["observed_at"]))}</p>
{('<p class="meta">PayPal payment created ' + _text(_recorded_time(purchase["capture_created_at"])) + '.</p>') if purchase.get("capture_created_at") else ''}
{reference}{description}{items}{total}</section>"""


def _refund_check_record(case, source_markup=None):
    if not case.get("refund_observation"):
        return ""
    from .returnlight_support_review import refund_check_source
    words = refund_check_source(case)
    if not words:
        return ""
    rendered = (source_markup or {}).get(("refund_check", 1), _text(words))
    return '<section class="refund-check"><h2>Kept refund check</h2><p class="verbatim" data-refund-check-source="1">' + rendered + '</p></section>'



def purchase_details_reply(case):
    """Prepare explicit, editable buyer wording from the same allowlisted item facts."""
    from .returnlight import CaseError, _validate_purchase_context
    import json
    purchase = _validate_purchase_context(case.get("purchase_context"), case.get("capture_id"))
    order = purchase.get("order") or {}
    items = order.get("items") or []
    if not items:
        raise CaseError("Saved item details are unavailable. Obtain the original receipt from the merchant's order records.")
    lines = ["Here are the item details I have on file:", ""]
    for item in items:
        name = item["name"]
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            name = json.dumps(name, ensure_ascii=False)
        line = f'{item["quantity"]} × {name}'
        if item["quantity"] != 1:
            line += " at " + _money(item["unit_cents"]) + " each"
        lines.append(line + " — " + _money(item["line_cents"]))
    listed = sum(item["line_cents"] for item in items)
    lines += ["", "Listed item total: " + _money(listed) + "."]
    if listed != order["total_cents"]:
        lines.append("The saved order total is " + _money(order["total_cents"]) +
                     "; the saved details do not include a breakdown of other charges or adjustments.")
    if purchase["original_cents"] != order["total_cents"]:
        lines.append("The original payment recorded is " + _money(purchase["original_cents"]) +
                     ", a separate original value.")
    if purchase.get("capture_created_at"):
        lines += ["", "PayPal payment created " + _recorded_time(purchase["capture_created_at"]) +
                  ". This is the payment record timestamp, not an original receipt date."]
    lines += ["", "This is a summary of saved purchase details, not the original receipt. "
                  "The prices above do not deduct later refunds."]
    text = "\n".join(lines)
    if len(text) > 3000:
        raise CaseError("These item details exceed the reply limit. Use the complete itemized document instead.")
    return text



def recorded_purchase_details_reply(case, reply_id, document_index, checked_sha256):
    """Prepare words from one verified historical summary, never current purchase facts."""
    import re
    from .returnlight import CaseError, CaseChanged
    reply = next((entry for entry in case.get("merchant_replies", [])
                  if entry["id"] == reply_id), None)
    if type(document_index) is not str or not re.fullmatch(r"[01]", document_index) or reply is None:
        raise CaseError("Choose a purchase summary from this recorded reply.")
    documents = reply.get("documents", [])
    index = int(document_index)
    if index >= len(documents) or documents[index].get("kind") != "purchase_summary":
        raise CaseError("That recorded document is not a purchase summary.")
    snapshot = documents[index]["snapshot"]
    if type(checked_sha256) is not str or checked_sha256 != snapshot["sha256"]:
        raise CaseChanged("The recorded summary changed. Reopen it before adding its details.")
    render_recorded_purchase_details(snapshot)
    historical = {"capture_id": case["capture_id"], "purchase_context": snapshot["source"]}
    text = purchase_details_reply(historical)
    text = text.replace("Here are the item details I have on file:",
                        "The earlier purchase summary listed:", 1)
    text = text.removesuffix("\n\nThis is a summary of saved purchase details, not the original receipt. "
                             "The prices above do not deduct later refunds.")
    if len(text) > 3000:
        raise CaseError("These item details exceed the reply limit. Use the complete itemized document instead.")
    return {"text": text, "recorded_purchase": {
        "reply_id": reply_id, "document_index": index, "document_sha256": snapshot["sha256"]}}


def render_purchase_details(case):
    """Current buyer-facing purchase document; recorded versions remain stable."""
    return (_render_purchase_details_v2(case) if (case.get("purchase_context") or {}).get("capture_created_at")
            else _render_purchase_details_v1(case))


def freeze_purchase_document(case, checked_sha256):
    """Retain only the checked buyer document inputs, never the private conversation."""
    import hashlib
    from .returnlight import CaseChanged, _validate_purchase_context
    purchase = _validate_purchase_context(case.get("purchase_context"), case.get("capture_id"))
    order = purchase.get("order") or {}
    source = {key: purchase[key] for key in ("capture_id", "original_cents", "observed_at", "order_id", "capture_created_at") if key in purchase}
    if order:
        source["order"] = {key: order[key] for key in ("id", "total_cents")}
        source["order"]["items"] = [{key: item[key] for key in ("name", "quantity", "unit_cents", "line_cents")}
                                    for item in order["items"]]
    version = 2 if "capture_created_at" in source else 1
    renderer = _render_purchase_details_v2 if version == 2 else _render_purchase_details_v1
    document = renderer({"capture_id": source["capture_id"], "purchase_context": source}).encode("utf-8")
    digest = hashlib.sha256(document).hexdigest()
    if type(checked_sha256) is not str or checked_sha256 != digest:
        raise CaseChanged("Purchase details changed. Review the current document before using it with this reply.")
    return {"version": version, "sha256": digest, "bytes": len(document), "source": source}


def render_recorded_purchase_details(value):
    """Read the immutable checked version, without consulting current case purchase facts."""
    import hashlib
    import re
    from .returnlight import CaseError
    if (type(value) is not dict or set(value) != {"version", "sha256", "bytes", "source"}
            or type(value["version"]) is not int or value["version"] not in (1, 2)
            or type(value["source"]) is not dict
            or type(value["source"].get("capture_id")) is not str
            or not re.fullmatch(r"[A-Za-z0-9]{8,40}", value["source"]["capture_id"])
            or type(value["bytes"]) is not int):
        raise CaseError("The recorded purchase document is unavailable.")
    if ("capture_created_at" in value["source"]) != (value["version"] == 2):
        raise CaseError("The recorded purchase document has an invalid date version.")
    renderer = _render_purchase_details_v2 if value["version"] == 2 else _render_purchase_details_v1
    document = renderer({"capture_id": value["source"]["capture_id"], "purchase_context": value["source"]}).encode("utf-8")
    if len(document) != value["bytes"] or hashlib.sha256(document).hexdigest() != value["sha256"]:
        raise CaseError("The recorded purchase document no longer matches its checked version.")
    return document


def _render_purchase_details_v2(case):
    """Dated provider payment context; the version-one renderer remains unchanged."""
    html = _render_purchase_details_v1(case)
    purchase = case["purchase_context"]
    when = _text(_recorded_time(purchase["capture_created_at"]))
    note = (f'<p class="note">PayPal payment created {when}.</p>'
            '<p class="note">This is the payment record timestamp, not an original receipt date.</p>')
    return html.replace("<footer><p>Purchase details checked ", note + "<footer><p>Purchase details checked ", 1)


def _render_purchase_details_v1(case):
    """Version one buyer document. Preserve its bytes for already recorded replies."""
    from .returnlight import CaseError, _validate_purchase_context
    purchase = _validate_purchase_context(case.get("purchase_context"), case.get("capture_id"))
    order = purchase.get("order") or {}
    items = order.get("items") or []
    if not items:
        raise CaseError("Saved item details are unavailable. Obtain the original receipt from the merchant's order records.")
    rows = "".join(
        '<tr><th scope="row">' + _text(item["name"]) +
        '<span class="item-detail">' + str(item["quantity"]) + ' × ' + _money(item["unit_cents"]) +
        '</span></th><td>' + _money(item["line_cents"]) + '</td></tr>'
        for item in items)
    listed = sum(item["line_cents"] for item in items)
    total_note = (
        '<p class="note">The saved order total differs from the listed items. '
        'A breakdown of other charges or adjustments is not included in the saved details.</p>'
        if listed != order["total_cents"] else "")
    payment_note = (
        '<p class="note">The recorded payment and saved order total are separate original values.</p>'
        if purchase["original_cents"] != order["total_cents"] else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Itemized purchase details</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f5f0;color:#1f3029;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}}
main{{max-width:800px;margin:48px auto;background:#fff;padding:52px 56px;border:1px solid #d8e0d7;border-radius:8px}}
.eyebrow{{color:#42644f;text-transform:uppercase;letter-spacing:.12em;font-size:12px;font-weight:700;margin:0 0 12px}}
h1{{font-family:Georgia,serif;font-weight:400;font-size:38px;line-height:1.13;margin:0 0 16px}}
.reference{{color:#52655b;font-size:14px;overflow-wrap:anywhere;margin:0 0 34px}}
table{{width:100%;border-collapse:collapse;margin:0 0 26px}}thead th{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#52655b;border-bottom:2px solid #2b5543;padding:12px 0;text-align:left}}
thead th:last-child,td{{text-align:right;white-space:nowrap}}tbody th{{text-align:left;font-weight:600;overflow-wrap:anywhere}}
td,tbody th{{padding:19px 0;border-bottom:1px solid #dce3da;vertical-align:top}}td{{padding-left:18px;font-variant-numeric:tabular-nums}}
.item-detail{{display:block;font-size:13px;font-weight:400;color:#52655b;margin-top:6px}}
.totals{{margin-left:auto;max-width:380px}}.totals p{{display:flex;justify-content:space-between;gap:24px;margin:11px 0}}.totals strong{{white-space:nowrap;font-variant-numeric:tabular-nums}}
.total{{font-size:18px;border-top:1px solid #c8d4c8;padding-top:13px}}.note{{color:#52655b;font-size:13px}}
footer{{border-top:1px solid #dce3da;margin-top:38px;padding-top:22px;font-size:13px;color:#52655b}}footer p{{margin:0 0 9px}}
@media(max-width:600px){{main{{margin:0;border:0;border-radius:0;padding:30px 22px}}h1{{font-size:31px}}td{{padding-left:12px}}.totals{{max-width:none}}.reference{{margin-bottom:24px}}}}
@media print{{body{{background:white}}main{{margin:0;max-width:none;border:0;padding:15mm 8mm}}tr{{break-inside:avoid}}thead{{display:table-header-group}}footer{{break-inside:avoid}}}}
</style></head><body><main>
<p class="eyebrow">Sandbox · Original purchase information</p><h1>Itemized purchase details</h1>
<p class="reference">Order {_text(purchase["order_id"])}</p>
<table aria-label="Original purchase items"><thead><tr><th scope="col">Item / quantity / unit price</th><th scope="col">Line total</th></tr></thead><tbody>{rows}</tbody></table>
<section class="totals" aria-label="Original amounts">
<p><span>Listed item total</span><strong>{_money(listed)}</strong></p>
<p class="total"><span>Saved order total</span><strong>{_money(order["total_cents"])}</strong></p>
<p><span>Original payment recorded</span><strong>{_money(purchase["original_cents"])}</strong></p>
</section>{total_note}{payment_note}
<footer><p>Purchase details checked {_text(_recorded_time(purchase["observed_at"]))}.</p>
<p>This is a summary of saved original purchase information, not the original merchant receipt.
The check time is not the purchase date. Later refunds are not allocated to these item prices.</p>
</footer></main></body></html>"""



def _support_source_identity(source):
    kind = source.get("kind")
    if (kind == "document" and isinstance(source.get("passage_id"), str) and source["passage_id"]
            and isinstance(source.get("file_sha256"), str)
            and len(source["file_sha256"]) == 64
            and type(source.get("page")) is int and source["page"] > 0):
        return ("document", source["passage_id"], source["file_sha256"], source["page"])
    if (kind == "recorded_purchase" and isinstance(source.get("reply_id"), str)
            and type(source.get("document_number")) is int and source["document_number"] > 0
            and isinstance(source.get("document_sha256"), str)
            and type(source.get("purchase_number")) is int and source["purchase_number"] > 0):
        return ("recorded_purchase", source["reply_id"], source["document_number"],
                source["document_sha256"], source["purchase_number"])
    if kind == "work" and type(source.get("work_seq")) is int and source["work_seq"] > 0:
        return ("work", source["work_seq"])
    if kind == "merchant" and isinstance(source.get("reply_id"), str):
        return ("merchant", source["reply_id"])
    if kind in ("buyer", "email"):
        if type(source.get("support_number")) is int:
            return ("support", source["support_number"])
        if type(source.get("message_number")) is int:
            return ("message", source["message_number"] - 1)
    if kind == "handling" and type(source.get("handling_number")) is int:
        return ("handling", source["handling_number"])
    if kind == "purchase" and type(source.get("purchase_number")) is int and source["purchase_number"] > 0:
        return ("purchase", source["purchase_number"])
    if kind == "refund_check" and type(source.get("refund_check_number")) is int and source["refund_check_number"] == 1:
        return ("refund_check", 1)
    if kind == "payment":
        return ("payment", 0)
    return None


def _buyer_source_markup(case, ai_links=None, support_reading=None, extra_sources=None):
    """Preserve recorded text exactly while locating confirmed ranges in the offline record."""
    messages = [case["complaint"]] + [entry["text"] for entry in case.get("buyer_followups", [])]
    replies = case.get("merchant_replies", [])
    sources = {("message", index): words for index, words in enumerate(messages)}
    sources.update({("merchant", reply["id"]): reply["text"] for reply in replies})
    if support_reading:
        from .returnlight_support_review import support_sources
        for source in support_sources(case):
            identity = _support_source_identity(source)
            if identity is not None:
                sources[identity] = source["text"]
    sources.update({("purchase", index + 1): words for index, words in enumerate(purchase_sources(case))})
    sources.update({("work", note["seq"]): note["text"] for note in merchant_work_sources(case)})
    if case.get("document_passages"):
        from .returnlight_document_sources import validated_document_passages
        for passage in validated_document_passages(case):
            sources[("document", passage["id"], passage["file"]["sha256"], passage["page"])] = passage["text"]
    ranges, links = {}, {}
    review = case.get("review") or {}
    if review.get("kind") == "human_selection":
        for number, item in enumerate(review.get("selected_conditions", [])):
            source = condition_source(case, item)
            if source is None:
                continue
            identity, message = source
            start, quote = item.get("source_start"), item.get("quote")
            if (type(start) is int and start >= 0 and isinstance(quote, str) and quote
                    and message[start:start + len(quote)] == quote):
                target = "kept-source-" + str(number)
                ranges.setdefault(identity, []).append((start, start + len(quote), target))
                links[number] = target
    if ai_links is not None:
        reviews = [case.get("review"), case.get("ai_suggestion"), support_reading]
        reviews.extend(entry.get("superseded_review") for entry in case.get("buyer_followups", []))
        for reading in reviews:
            if not isinstance(reading, dict) or reading.get("kind") not in ("conversation_brief", "support_brief"):
                continue
            for item in reading.get("sources", []):
                if id(item) in ai_links:
                    continue
                number, start, quote = item.get("message_number"), item.get("quote_start"), item.get("quote")
                if reading.get("kind") == "support_brief":
                    identity = _support_source_identity(item)
                elif item.get("speaker") == "purchase":
                    if reading.get("purchase_sources") != purchase_sources(case):
                        continue
                    identity = ("purchase", number)
                elif item.get("speaker") == "work":
                    identity = ("work", item.get("work_seq"))
                elif item.get("speaker") == "merchant":
                    identity = ("merchant", item.get("reply_id"))
                elif item.get("speaker", "buyer") in ("buyer", "email") and type(number) is int:
                    identity = ("message", number - 1)
                else:
                    continue
                words = sources.get(identity)
                if (words is None or type(start) is not int or start < 0
                        or not isinstance(quote, str) or not quote
                        or words[start:start + len(quote)] != quote):
                    continue
                target = "ai-source-" + str(len(ai_links))
                ai_links[id(item)] = target
                ranges.setdefault(identity, []).append((start, start + len(quote), target))
    rendered = {}
    for identity, message in sources.items():
        selected = ranges.get(identity, [])
        boundaries = sorted({0, len(message)} | {point for start, end, _ in selected for point in (start, end)})
        parts = []
        for start, end in zip(boundaries, boundaries[1:]):
            parts.extend('<span class="source-point" id="' + target + '" tabindex="-1"></span>'
                         for first, _, target in selected if first == start)
            words = _text(message[start:end])
            targets = [target for first, last, target in selected if first <= start and end <= last]
            if targets:
                words = '<mark class="kept-source" data-source-targets="' + " ".join(targets) + '">' + words + '</mark>'
            parts.append(words)
        rendered[identity] = "".join(parts)
    if extra_sources is not None:
        extra_sources.update(rendered)
    return ([rendered[("message", index)] for index in range(len(messages))],
            {reply["id"]: rendered[("merchant", reply["id"])] for reply in replies}, links)


def _condition_label(item, email_messages, merchant_numbers):
    if "work_seq" in item:
        return "Internal merchant work note"
    if "reply_id" in item:
        number = merchant_numbers.get(item["reply_id"])
        return "Merchant reply" + (" " + str(number) if number is not None else "")
    index = item["message_index"]
    return ("Email message " if index in email_messages else "Buyer message ") + str(index + 1)


def _retained_conditions(review, source_links=None, email_messages=(), merchant_numbers=None):
    if review.get("kind") != "human_selection" or not review.get("selected_conditions"):
        return ""
    source_links = source_links or {}
    merchant_numbers = merchant_numbers or {}
    return '<h3>Conditions retained by the merchant</h3>' + "".join(
        '<p>' + _text(_condition_label(item, email_messages, merchant_numbers)) + '</p><blockquote>'
        + _text(item["quote"]) + '</blockquote>'
        + ('<p><a class="source-link" href="#' + source_links[number] + '">Read original words</a></p>' if number in source_links else '')
        for number, item in enumerate(review["selected_conditions"]))


def _superseded_review(entry, email_messages=(), merchant_numbers=None, ai_links=None):
    review = entry.get("superseded_review")
    if not isinstance(review, dict):
        return ""
    interpretation = "" if review.get("kind") in ("source_selection", "human_selection") else (
        f'<p class="verbatim">{_text(review.get("summary"))}</p>'
        f'<p class="meta">{_text(review.get("category"))}</p>')
    label = "Previous merchant review" if review.get("kind") == "human_selection" else "Previous model review"
    return f"""<details class="past-review"><summary>{label} · superseded</summary>
{interpretation}{_open_questions(review, earlier=True, ai_links=ai_links)}<blockquote>{_selection(review, ai_links)}</blockquote>{_retained_conditions(review, email_messages=email_messages, merchant_numbers=merchant_numbers)}</details>"""

def _superseded_resolution(entry):
    resolution = entry.get("superseded_resolution") or {}
    reason = resolution.get("reason")
    if not reason:
        return ""
    return (f'<details class="past-resolution"><summary>Earlier saved resolution</summary>'
            f'<p class="verbatim">{_text(reason)}</p></details>')


def _email_context(imported: dict | None, photo_loader=None) -> str:
    if not imported:
        return ""
    result = (f'<p class="meta">Imported email · sender not verified</p>'
              f'<p class="verbatim"><strong>{_text(imported["subject"])}</strong></p>'
              f'<p class="verbatim">From: {_text(imported["sender"])}</p>')
    if "reply_to" in imported:
        result += f'<p class="verbatim">Reply-To: {_text(imported["reply_to"]) or "No address supplied"}</p>'
    if imported.get("date"):
        result += f'<p class="meta">Email date (from message): {_text(imported["date"])}</p>'
    count = imported.get("attachments_omitted", 0)
    if count:
        result += f'<p class="meta">{count} attachment{"s" if count != 1 else ""} not imported.</p>'
    if imported.get("photos"):
        images = []
        for photo in imported["photos"]:
            try:
                if photo_loader is None:
                    raise ValueError("No photo source")
                encoded = base64.b64encode(photo_loader(photo)).decode("ascii")
                if photo["mime"] == "application/pdf":
                    data_url = f'data:application/pdf;base64,{encoded}'
                    images.append(f'<figure class="document-evidence"><figcaption>{_text(photo["name"])}</figcaption><iframe title="{_text(photo["name"])}" src="{data_url}#navpanes=0&amp;view=FitH" loading="lazy"></iframe><a download="{_text(photo["name"])}" href="{data_url}">Download original PDF</a></figure>')
                else:
                    images.append(f'<figure><img src="data:{_text(photo["mime"])};base64,{encoded}" alt="{_text(photo["name"])}"><figcaption>{_text(photo["name"])}</figcaption><a download="{_text(photo["name"])}" href="data:{_text(photo["mime"])};base64,{encoded}">Download original photo</a></figure>')
            except (OSError, ValueError):
                images.append(f'<p>{_text(photo["name"])} — attachment unavailable in this record.</p>')
        label = 'Attached evidence' if any(item['mime'] == 'application/pdf' for item in imported['photos']) else 'Attached photos'
        result += '<details class="photo-evidence"><summary>' + label + ' (' + str(len(images)) + ')</summary>' + ''.join(images) + '</details>'
    return result


def _merchant_reply(entry, source_markup=None, photo_loader=None, recorded_purchase_markup=None):
    """The merchant records an external reply; the application does not verify delivery."""
    documents = []
    passages = []
    for document_number, document in enumerate(entry.get("documents", []), 1):
        try:
            name, mime, data = recorded_reply_document(document, photo_loader)
            encoded = base64.b64encode(data).decode("ascii")
            label = name if document["kind"] == "original" else "Saved purchase summary"
            documents.append(f'<a download="{_text(name)}" href="data:{_text(mime)};base64,{encoded}">{_text(label)}</a>')
            if document["kind"] == "purchase_summary":
                prefix = ("recorded_purchase", entry["id"], document_number, document["snapshot"]["sha256"])
                for identity, markup in sorted((recorded_purchase_markup or {}).items(), key=lambda item: item[0][-1]):
                    if identity[:4] == prefix:
                        passages.append('<p class="verbatim recorded-purchase-source">' + markup + '</p>')

        except (OSError, ValueError):
            missing = (document.get("file") or {}).get("name", "Original document") if document.get("kind") == "original" else "Saved purchase summary"
            documents.append(f'<span>{_text(missing)} — recorded document unavailable in this copy</span>')
    attachments = '<p class="recorded-reply-documents">' + ' · '.join(documents) + '</p>' if documents else ''
    if passages:
        attachments += '<section class="recorded-purchase-passages"><h4>Earlier purchase summary passages</h4>' + "".join(passages) + "</section>"
    return (f'<article class="message merchant-reply"><div class="message-heading">'
            f'<h3>Merchant reply</h3>'
            f'<time>{_text(_recorded_time(entry.get("recorded_at")))}</time></div>'
            f'<p class="verbatim" data-merchant-reply-id="{_text(entry["id"])}">{(source_markup or {}).get(entry["id"], _text(entry["text"]))}</p>'
            f'<p class="meta">Recorded as sent outside Returnlight</p>{attachments}</article>')


def _selected_document_record(case, source_markup, photo_loader=None):
    """Selected local text and its checked original remain distinct from buyer statements."""
    if not case.get("document_passages"):
        return ""
    from .returnlight_document_sources import validated_document_passages
    passages = validated_document_passages(case)
    if not passages:
        return ""
    originals, entries = {}, []
    for passage in passages:
        reference = passage["file"]
        file_identity = (reference["id"], reference["sha256"])
        if file_identity not in originals:
            target = "selected-original-" + str(len(originals) + 1)
            try:
                name, mime, data = recorded_reply_document({"kind": "original", "file": reference}, photo_loader)
                encoded = base64.b64encode(data).decode("ascii")
                link = (f'<a id="{target}" class="source-link" download="{_text(name)}" '
                        f'href="data:{_text(mime)};base64,{encoded}">Download original · {_text(name)}</a>')
            except (OSError, ValueError):
                link = (f'<p id="{target}" tabindex="-1">{_text(reference["name"])}'
                        ' — original document unavailable in this copy</p>')
            originals[file_identity] = (target, link)
        target = originals[file_identity][0]
        identity = ("document", passage["id"], reference["sha256"], passage["page"])
        words = source_markup.get(identity, _text(passage["text"]))
        entries.append(
            f'<article class="message selected-document-passage"><h3>{_text(reference["name"])} · Page {passage["page"]}</h3>'
            f'<p class="meta">Selected {_text(_recorded_time(passage["selected_at"]))}</p>'
            f'<p class="verbatim" data-document-passage="{_text(passage["id"])}" '
            f'data-file-sha256="{_text(reference["sha256"])}" data-document-page="{passage["page"]}">{words}</p>'
            f'<a class="source-link" href="#{target}">Original file · {_text(reference["name"])}</a></article>')
    files = "".join('<div class="selected-original-file">' + link + '</div>' for _, link in originals.values())
    return ('<section class="selected-document-passages"><h2>Selected document passages</h2>'
            + "".join(entries) + '<div class="selected-original-files"><h3>Original files</h3>' + files + '</div></section>')


def render_case_record(case, generated_at=None, *, photo_loader=None, working_draft=None):
    """Render only case evidence and human decisions; never legacy AI remedy advice."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    email_messages = {index for index, message in enumerate([case, *case.get("buyer_followups", [])]) if message.get("imported_email")}
    title = _OUTCOMES.get(case["state"], case["state"])
    if case["state"] == "CLOSED" and (case.get("merchant_followup") or {}).get("open"):
        title = "No refund · follow-up open"
    if case["state"] == "REVIEWED" and (case.get("review") or {}).get("kind") != "human_selection":
        title = "Review needed"
    if case["state"] == "OPEN" and case.get("reopened_at"):
        title = "Reopened · review needed"
    amount = _money(case["approved_cents"]) if case["approved_cents"] else "No current refund amount approved"
    amount_class = "amount" if case["approved_cents"] else "no-amount"
    amount_label = '<p class="meta">Approved amount</p>' if case["approved_cents"] and case["state"] != "COMPLETED" else ""
    amount_display = f'<p class="{amount_class}">{_text(amount)}</p>' if case["state"] != "CLOSED" else ""
    subject_label = f'<p class="case-subject">{_text(case["subject"])}</p>' if case.get("subject") else ""
    resolution = case.get("resolution", {})
    decision = (f'<div class="decision"><h3>Merchant decision</h3><p class="verbatim">{_text(resolution.get("reason"))}</p></div>'
                if case["state"] == "CLOSED" and resolution.get("reason") else "")
    followup = case.get("merchant_followup")
    if followup:
        decision += (f'<div class="decision"><h3>{("Earlier next step · newer buyer message" if merchant_followup_needs_review(case) else "Merchant next step" if followup["open"] else "Merchant work completed") if case.get("post_payment_messages") or case["state"] != "CLOSED" else ("Agreed work needs follow-up" if followup["open"] else "Agreed work completed")}</h3>'
                     f'<p class="verbatim">{_text(followup["note"])}</p></div>')
    fields = [
        ("Case ID", case["id"]), ("Capture ID", case["capture_id"] or "Not linked"),
        ("Recorded state", case["state"]), ("Approved amount", amount),
        ("Merchant-entered ceiling", "Not set" if case.get("payment_pending") else _money(case["ceiling_cents"])),
        ("Refund ID", case.get("refund_id") or "None recorded"),
        ("Request ID", case.get("request_id") or "None recorded"),
    ]
    original = case.get("original_payment_details")
    if original:
        fields.extend([
            ("Originally entered capture ID", original["capture_id"]),
            ("Originally entered merchant ceiling", _money(original["ceiling_cents"])),
        ])
    if case.get("refund_candidate_id"):
        fields.append(("Unverified refund reference", case["refund_candidate_id"]))
    facts = "".join(f"<div><dt>{_text(k)}</dt><dd>{_text(v)}</dd></div>" for k, v in fields)
    review = case.get("review")
    from .returnlight_support_review import current_support_review
    current_reading = current_support_review(case)
    ai_links, extra_sources = {}, {}
    buyer_sources, merchant_sources, source_links = _buyer_source_markup(case, ai_links, current_reading, extra_sources)
    recorded_purchase_markup = {}
    for source in (current_reading or {}).get("sources", []):
        identity = _support_source_identity(source)
        if identity and identity[0] == "recorded_purchase" and identity in extra_sources:
            recorded_purchase_markup[identity] = extra_sources[identity]

    if current_reading and any(source.get("kind") == "payment" for source in current_reading["sources"]):
        facts += ('<div><dt>Recorded refund context</dt><dd class="verbatim" data-payment-source="0">'
                  + extra_sources[("payment", 0)] + '</dd></div>')
    merchant_numbers = {entry["id"]: number for number, entry in enumerate(case.get("merchant_replies", []), 1)}
    evidence = ""
    previous = case.get("ai_suggestion")
    if previous and previous.get("kind") == "conversation_brief" and (review or {}).get("kind") != "conversation_brief":
        evidence = _superseded_review({"superseded_review": previous}, ai_links=ai_links)
    if review:
        interpretation = "" if review.get("kind") in ("source_selection", "human_selection") else (
            f'<p class="verbatim">{_text(review.get("summary"))}</p>'
            f'<p class="meta">{_text(review.get("category"))}</p>')
        human = review.get("kind") == "human_selection"
        last_reading_choice = next((entry.get("event") for entry in reversed(case.get("history", []))
                                    if entry.get("event") in ("ai.reviewed", "merchant.highlight_dismissed")), None)
        question_review = review if review.get("kind") == "conversation_brief" else (
            previous if human and last_reading_choice != "merchant.highlight_dismissed" else None)
        question_review = question_review or {}
        current_questions = (
            question_review.get("message_count") == 1 + len(case.get("buyer_followups", []))
            and question_review.get("merchant_message_count", 0) == len([
                entry for entry in case.get("merchant_replies", []) if not entry.get("through_post_payment_message")])
            and not case.get("post_payment_messages")
            and (not question_review.get("work_sources") or question_review.get("work_source_revision") == max((note["seq"] for note in merchant_work_sources(case)), default=0))
            and (not question_review.get("purchase_sources") or question_review["purchase_sources"] == purchase_sources(case)))
        questions = _open_questions(question_review, earlier=not current_questions, ai_links=ai_links) if (
            review.get("kind") == "conversation_brief" or current_questions) else ""
        review_heading = "AI reading · check against buyer words" if review.get("kind") == "conversation_brief" else "Merchant review" if human else ("AI suggestion · needs merchant review" if case["state"] in ("OPEN", "REVIEWED") else "Model review")
        if case.get("post_payment_messages"):
            review_heading += " before the refund attempt"
        passage_heading = "Buyer source passages" if review.get("kind") == "conversation_brief" else "Reviewed buyer message" if human else "Selected passage"
        conditions = _retained_conditions(review, source_links, email_messages, merchant_numbers)
        reviewed_passage = "<blockquote>" + _selection(review, ai_links) + "</blockquote>"
        if human and len(review.get("context") or review.get("quote") or "") > 1000:
            reviewed_passage = ('<details class="reviewed-passage"><summary>Read the reviewed message</summary>'
                                + reviewed_passage + "</details>")
        evidence += f"""<section class="review" id="merchant-review"><h2>{review_heading}</h2>{interpretation}
{questions}<h3>{passage_heading}</h3>{reviewed_passage}{conditions}</section>"""
    email_context = _email_context(case.get("imported_email"), photo_loader)
    opened = next((e.get("recorded_at") for e in case.get("history", []) if e.get("event") == "case.opened"), None)
    # History order is the causal order, even if clocks tie or move backwards.
    # Replies and support decisions share the same boundary after a buyer message.
    actions = {}
    followup_count = support_count = reply_count = handled_count = 0
    replies = case.get("merchant_replies", [])
    handled = case.get("support_resolutions", [])
    for event in case.get("history", []):
        kind = event.get("event")
        if kind in ("buyer.followup_recorded", "buyer.followup_added"):
            followup_count += 1
        elif kind == "buyer.post_payment_message":
            support_count += 1
        elif kind in ("merchant.followup_opened", "merchant.followup_completed"):
            work_label = "Merchant marked work complete" if kind == "merchant.followup_completed" else "Merchant work planned"
            markup = (f'<article class="message merchant-work"><h3>{work_label}</h3>'
                      f'<p class="meta">{_text(_recorded_time(event.get("recorded_at")))} · Internal merchant note</p>'
                      f'<p class="verbatim" data-work-source="{event["seq"]}">{extra_sources.get(("work", event["seq"]), _text(event["detail"]))}</p></article>')
            actions.setdefault((followup_count, support_count), []).append(markup)
        elif kind == "merchant.reply_recorded" and reply_count < len(replies):
            actions.setdefault((followup_count, support_count), []).append(_merchant_reply(replies[reply_count], merchant_sources, photo_loader, recorded_purchase_markup))
            reply_count += 1
        elif kind == "merchant.support_handled" and handled_count < len(handled):
            entry = handled[handled_count]
            markup = (f'<article class="message support-handled"><h3>Merchant marked support handled</h3>'
                      f'<p class="meta">{_text(_recorded_time(entry.get("recorded_at")))} · through buyer message {entry["through_message"]}</p>'
                      f'<p class="verbatim" data-handling-source="{handled_count + 1}">{extra_sources.get(("handling", handled_count + 1), _text(entry["reason"]))}</p></article>')
            actions.setdefault((followup_count, entry["through_message"]), []).append(markup)
            handled_count += 1
    def at_boundary(buyer, support):
        return "".join(actions.get((buyer, support), []))
    original_replies = at_boundary(0, 0)
    followups = "".join(
        f'<article class="message"><div class="message-heading"><h3>Buyer follow-up {index}</h3><time>{_text(_recorded_time(entry.get("recorded_at")))}</time></div>{_email_context(entry.get("imported_email"), photo_loader)}<blockquote data-buyer-source="{index}">{buyer_sources[index]}</blockquote>{_superseded_review(entry, email_messages, merchant_numbers, ai_links)}{_superseded_resolution(entry)}</article>'
        + at_boundary(index, 0)
        for index, entry in enumerate(case.get("buyer_followups", []), 1))
    support_evidence = ""
    if current_reading:
        numbers = current_reading.get("open_questions", [])
        def source_words(source):
            return f'<p class="meta">{_text(source["label"])}</p><blockquote>{_text(source["quote"])}</blockquote>' + _ai_source_link(source, ai_links)
        passages = "".join(source_words(source) for index,source in enumerate(current_reading["sources"],1) if index not in numbers)
        question_words = "".join(source_words(current_reading["sources"][number-1])
                                 for number in numbers if type(number) is int and 1 <= number <= len(current_reading["sources"]))
        questions = '<h3>Questions to resolve · AI reading</h3>' + question_words if question_words else ""
        passage_heading = "<h3>Support source passages</h3>" if passages else ""
        support_evidence = f'<section class="review"><h2>AI reading · current support</h2><p>{_text(current_reading["summary"])}</p>{questions}{passage_heading}{passages}</section>'
    support = ""
    if case.get("post_payment_messages"):
        status = "Support follow-up open" if case.get("support_followup_open") else "Support follow-up handled"
        conversation = []
        for index, entry in enumerate(case["post_payment_messages"], 1):
            conversation.append(
                f'<article class="message"><h3>Buyer message after refund attempt {index}</h3>'
                f'<p class="meta">{_text(_recorded_time(entry.get("recorded_at")))} · Refund {_text(entry.get("payment_state", "unknown").lower())} when recorded</p>'
                f'{_email_context(entry.get("imported_email"), photo_loader)}<blockquote data-support-source="{index}">{extra_sources.get(("support", index), _text(entry["text"]))}</blockquote></article>'
                + at_boundary(len(case.get("buyer_followups", [])), index))
        support = f'<section><h2>{status}</h2>{support_evidence}{"".join(conversation)}</section>'
    event_labels = {
        "merchant.followup_opened": "Merchant next step saved",
        "merchant.followup_completed": "Merchant work completed",
        "buyer.post_payment_message": "Later buyer message recorded",
        "merchant.support_handled": "Buyer support handled",
        "merchant.reply_recorded": "External merchant reply recorded",
        "case.opened": "Case opened", "ai.reviewed": "Model reviewed the message",
        "merchant.reviewed": "Merchant reviewed the message",
        "merchant.review_reopened": "Merchant reopened the review",
        "purchase.observed": "Original purchase checked",
        "merchant.subject_changed": "Case renamed",
        "merchant.approved": "Refund approved", "merchant.closed_without_refund": "Case closed",
        "merchant.reopened_for_review": "Case reopened", "buyer.followup_added": "Buyer follow-up added",
        "refund.attempt_reserved": "Refund attempt started", "refund.reference_observed": "Refund reference received",
        "refund.response": "Provider response received", "refund.reconciled": "Refund status checked",
    }
    history = "".join(
        f'<li><div class="event-heading"><strong>{_text(event_labels.get(e.get("event"), e.get("event")))}</strong><time>{_text(_recorded_time(e.get("recorded_at")))}</time></div>'
        f'<p class="verbatim">{_text(e.get("detail"))}</p></li>' for e in case.get("history", []))
    active_targets = [*source_links.values(), *ai_links.values()]
    active_source_rules = (
        ", ".join("#" + target + ':target ~ mark[data-source-targets~="' + target + '"]'
                  for target in active_targets)
        + "{background:#285b48;color:#fff;box-shadow:0 2px 0 #183b33;box-decoration-break:clone;-webkit-box-decoration-break:clone}"
        if active_targets else "")
    saved_reply = ""
    reply = ((working_draft or {}).get("draft") or {}).get("reply")
    if (reply and reply.get("capture") == case["capture_id"]
            and isinstance(reply.get("text"), str) and reply["text"].strip()):
        try:
            basis = json.loads(reply.get("version", ""))
        except (ValueError, TypeError):
            basis = None
        current = [case["capture_id"], case["state"], case["approved_cents"],
                   case.get("refund_id"), len(case["history"])]
        label = ("Case changed · check this saved reply" if basis != current else
                 "Needs your check before sending" if reply.get("review_required") else "")
        check = f'<p class="meta">{label}</p>' if label else ""
        saved_reply = (f'<section class="saved-unsent-reply"><h2>Saved unsent reply</h2>{check}'
                       f'<blockquote>{_text(reply["text"])}</blockquote></section>')
    refund = f'<p class="reference">Refund {_text(case["refund_id"])}</p>' if case.get("refund_id") else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-src data:">
<title>Returnlight case {_text(case["id"][:8])}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;color:#183b33;background:#e9ede7;font:16px/1.55 system-ui,sans-serif}}
main{{max-width:960px;margin:32px auto;padding:40px 48px;background:#fff;box-shadow:0 16px 64px #183b3310}}
.brand-row,.message-heading,.event-heading{{display:flex;align-items:baseline;justify-content:space-between;gap:16px}}
.brand{{font:700 24px/1.2 Georgia,serif;letter-spacing:-.8px}}.badge{{font-size:12px;background:#edf1e9;border:1px solid #d9e0d3;border-radius:20px;padding:3px 10px}}
header{{border-bottom:1px solid #dce3dd;padding-bottom:28px}}.case-label{{margin:30px 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#62766b}}
h1{{font:36px/1.16 Georgia,serif;letter-spacing:-.6px;margin:0 0 12px}}.amount{{font-size:40px;line-height:1.15;font-weight:650;letter-spacing:-1.4px;margin:14px 0 8px}}.no-amount{{font-size:15px;color:#62766b;margin:14px 0 8px}}
.decision{{margin-top:20px;border-left:3px solid #96ad92;padding-left:16px}}.reference{{font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
.case-subject{{font-size:20px;font-weight:550;overflow-wrap:anywhere;margin:12px 0}}
h2{{font-size:20px;letter-spacing:-.3px;margin:0 0 20px}}h3{{font-size:14px;margin:0}}p{{margin:8px 0}}
section{{padding:28px 0;border-bottom:1px solid #dce3dd}}.message+.message{{border-top:1px solid #e7ebe6;margin-top:22px;padding-top:22px}}
.merchant-reply{{border-left:3px solid #96ad92;padding-left:18px;margin-left:20px}}
.recorded-reply-documents,.selected-document-passages{{overflow-wrap:anywhere}}
.selected-original-files{{margin-top:24px;padding-top:20px;border-top:1px solid #dce3dd}}.selected-original-file{{margin-top:12px}}
time,.meta,dt{{font-size:12px;color:#62766b}}time{{font-variant-numeric:tabular-nums}}blockquote{{margin:12px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}}
.review{{background:#f4f6f0;padding:22px 24px;margin:28px 0;border:0;border-radius:8px}}.review h2{{margin-bottom:12px}}.review h3{{color:#62766b;margin-top:14px}}
.source-link{{color:#285b48;text-underline-offset:3px;font-size:14px}}.source-point{{display:inline-block;width:0;height:1em;scroll-margin-top:24px}}[data-work-source] .source-point,[data-document-passage] .source-point{{scroll-margin-top:140px}}.source-point:target{{outline:none}}mark.kept-source{{background:#e2edd9;color:#203d2c}}
{active_source_rules}
.review mark{{background:#e2edd9;color:#203d2c}}.review blockquote{{white-space:pre-wrap;border-left:2px solid #96ad92;padding-left:16px}}.verbatim{{white-space:pre-wrap;overflow-wrap:anywhere}}
details{{border-top:1px solid #dce3dd;padding:20px 0}}summary{{cursor:pointer;font-size:16px;font-weight:650;list-style-position:outside;margin-left:16px}}
.past-review{{padding:12px 0 0;margin-top:12px;border:0}}.past-review summary{{font-size:12px;color:#62766b}}
dl{{display:grid;grid-template-columns:1fr 1fr;gap:18px 28px;margin:24px 0 4px}}dd{{margin:3px 0 0;font-size:14px;font-weight:550;overflow-wrap:anywhere}}
ol{{padding-left:20px;margin:24px 0 0}}li{{padding:0 0 20px 8px;break-inside:avoid}}li p{{font-size:13px;color:#526b62}}li strong{{font-size:14px}}
.purchase-table{{width:100%;border-collapse:collapse;margin-top:18px;font-size:14px}}.purchase-table th,.purchase-table td{{padding:12px 0;border-bottom:1px solid #e7ebe6;text-align:left;vertical-align:top}}.purchase-table thead th{{font-size:12px;color:#62766b;font-weight:500}}.purchase-table tbody th{{font-weight:550;overflow-wrap:anywhere}}.purchase-table th:last-child,.purchase-table td:last-child{{text-align:right;white-space:nowrap;padding-left:18px;width:1%}}.item-detail{{display:block;font-size:12px;color:#62766b;font-weight:400;margin-top:3px}}.purchase-total{{display:flex;justify-content:space-between;gap:16px;font-size:13px;margin-top:14px}}
footer{{display:flex;justify-content:space-between;gap:16px;border-top:1px solid #dce3dd;padding-top:18px;font-size:12px;color:#62766b}}
@media(max-width:600px){{body{{background:#fff}}main{{margin:0;padding:24px 22px;box-shadow:none}}h1{{font-size:30px}}.amount{{font-size:36px}}.message-heading,.event-heading{{display:block}}.message-heading time,.event-heading time{{display:block;margin-top:4px}}dl{{grid-template-columns:1fr}}footer{{display:block}}footer time{{display:block;margin-top:4px}}.review{{padding:20px}}}}
@media print{{body{{background:white}}main{{max-width:none;margin:0;padding:0;box-shadow:none}}article,dl>div{{break-inside:avoid}}details> *{{display:block}}details::details-content{{content-visibility:visible;display:block}}}}
.photo-evidence figure{{margin:16px 0}}.photo-evidence img{{max-width:100%;height:auto}}.photo-evidence figcaption{{font-size:12px;overflow-wrap:anywhere}}.document-evidence iframe{{display:block;width:100%;height:540px;border:1px solid #dce3dd;margin:12px 0}}\n@media(max-width:600px){{.document-evidence iframe{{display:none}}.document-evidence{{padding:16px;border:1px solid #dce3dd}}}}\n</style></head><body><main>
<header><div class="brand-row"><span class="brand">Returnlight</span><span class="badge">Sandbox</span></div>
<p class="case-label">Case {_text(case["id"][:8].upper())} · Private record</p><h1>{_text(title)}</h1>
{subject_label}{amount_label}{amount_display}{refund}{decision}</header>
{saved_reply}
<section><h2>Buyer conversation</h2><article class="message"><div class="message-heading"><h3>Original buyer message</h3><time>{_text(_recorded_time(opened))}</time></div>{email_context}<blockquote data-buyer-source="0">{buyer_sources[0]}</blockquote></article>{original_replies}{followups}</section>
{_purchase_record(case, extra_sources)}
{_selected_document_record(case, extra_sources, photo_loader)}
{_refund_check_record(case, extra_sources)}
{''.join('<details><summary>Earlier payment purchase · ' + _text(p['capture_id']) + '</summary>' + _purchase_record({'purchase_context':p}) + '</details>' for p in case.get('previous_purchase_contexts', []))}
{support}
{evidence}
<details><summary>Payment references</summary><dl>{facts}</dl></details>
<details><summary>Case activity · {len(case.get("history", []))} events</summary><ol>{history}</ol></details>
<footer><span>Private case record</span><time>Saved {_text(_recorded_time(generated_at))}</time></footer>
</main></body></html>"""


def reply_email_choices(case: dict) -> list[dict]:
    """Address choices from recorded email headers; no inferred buyer identity."""
    from email import policy
    from email.parser import Parser
    import hashlib
    import json
    contexts = [case.get("imported_email")]
    contexts += [entry.get("imported_email") for entry in case.get("buyer_followups", [])]
    contexts += [entry.get("imported_email") for entry in case.get("post_payment_messages", [])]
    choices = []
    for number, context in enumerate(contexts):
        if not context:
            continue
        header = context.get("reply_to", context["sender"])
        try:
            parsed = Parser(policy=policy.default).parsestr("To: " + header + "\n\n")["To"]
            addresses = list(parsed.addresses) if parsed and not parsed.defects else []
            if len(addresses) > 16 or any(not item.username or not item.domain for item in addresses):
                addresses = []
        except (ValueError, TypeError, AttributeError):
            addresses = []
        basis = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        recipients = []
        for address in addresses:
            if any(ord(char) < 32 or ord(char) == 127 for char in str(address)):
                continue
            if address.addr_spec not in [item["address"] for item in recipients]:
                recipients.append({"address": address.addr_spec, "label": str(address)})
        subject = context["subject"]
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        choices.append({"source": number, "basis": basis, "sender": context["sender"],
                        "reply_to": header, "subject": subject, "date": context.get("date"),
                        "recipients": recipients})
    return list(reversed(choices))


def recorded_reply_document(document, photo_loader=None):
    """Return the exact earlier document, independent of current working details."""
    from .returnlight import _validate_reply_documents
    _validate_reply_documents([document])
    if document["kind"] == "original":
        if photo_loader is None:
            raise ValueError("The saved original document is unavailable.")
        reference = document["file"]
        data = photo_loader(reference)
        import hashlib
        from .returnlight_photos import photo_format
        if (len(data) != reference["bytes"] or hashlib.sha256(data).hexdigest() != reference["sha256"]
                or photo_format(data) != (reference["mime"], reference["width"], reference["height"])):
            raise ValueError("The recorded original document changed.")
        return reference["name"], reference["mime"], data
    return "purchase-details.html", "text/html", render_recorded_purchase_details(document["snapshot"])


def checked_reply_documents(case, payload, *, original_document=None):
    """One checked document path for the unsent email and reported reply record."""
    from .returnlight import CaseChanged, CaseError
    documents = []
    include_purchase = payload.get("include_purchase_details", False)
    if type(include_purchase) is not bool:
        raise CaseError("Choose whether to include the checked purchase document.")
    if include_purchase:
        snapshot = freeze_purchase_document(case, payload.get("purchase_details_sha256"))
        documents.append(({"kind":"purchase_summary", "snapshot":snapshot}, render_recorded_purchase_details(snapshot)))
    include_original = payload.get("include_reply_document", False)
    if type(include_original) is not bool:
        raise CaseError("Choose whether to include the checked original document.")
    if include_original:
        if original_document is None:
            raise CaseChanged("Original document changed. Open and check the current file again.")
        reference, original_bytes = original_document
        from .returnlight_photos import validate_photo
        import hashlib
        validate_photo(reference)
        if (payload.get("reply_document_id") != reference["id"]
                or payload.get("reply_document_sha256") != reference["sha256"]
                or len(original_bytes) != reference["bytes"]
                or hashlib.sha256(original_bytes).hexdigest() != reference["sha256"]):
            raise CaseChanged("Original document changed. Open and check the current file again.")
        documents.append(({"kind":"original", "file":dict(reference)}, original_bytes))
    return documents


def render_reply_email(case: dict, payload: dict, *, original_document: tuple[dict, bytes] | None = None) -> bytes:
    """Create a checked unsent email file; this never submits or records a send."""
    import json
    from email import policy
    from email.message import EmailMessage
    from .returnlight import CaseChanged, CaseError
    expected = [case["capture_id"], case["state"], case["approved_cents"],
                case.get("refund_id"), len(case["history"])]
    try:
        version = json.loads(payload.get("version", ""))
    except (ValueError, TypeError):
        version = None
    if type(version) is not list or len(version) != len(expected) or any(
            type(actual) is not type(wanted) or actual != wanted
            for actual, wanted in zip(version, expected)):
        raise CaseChanged("Case changed. Check the current conversation before preparing the email draft.")
    if payload.get("confirmed") is not True:
        raise CaseError("Check the recipient and subject before preparing the email draft.")
    text = payload.get("text")
    if type(text) is not str or not text.strip() or len(text) > 3000 or "\x00" in text:
        raise CaseError("The unsent reply must contain 1 to 3000 characters.")
    choice = next((item for item in reply_email_choices(case)
                   if type(payload.get("source")) is int and item["source"] == payload["source"]
                   and item["basis"] == payload.get("basis")), None)
    recipient = next((item for item in choice["recipients"]
                      if item["address"] == payload.get("recipient")), None) if choice else None
    if not recipient:
        raise CaseError("Choose a recorded reply address and check the email context again.")
    contexts = [case.get("imported_email")]
    contexts += [entry.get("imported_email") for entry in case.get("buyer_followups", [])]
    contexts += [entry.get("imported_email") for entry in case.get("post_payment_messages", [])]
    context = contexts[choice["source"]]
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = recipient["label"]
    message["Subject"] = choice["subject"]
    message["X-Unsent"] = "1"
    if context.get("message_id"):
        message["In-Reply-To"] = context["message_id"]
        references = list(dict.fromkeys([*context.get("reply_references", []), context["message_id"]]))[-32:]
        message["References"] = " ".join(references)
    message.set_content(text)
    for document, data in checked_reply_documents(case, payload, original_document=original_document):
        reference = document.get("file")
        mime = reference["mime"] if reference else "text/html"
        name = reference["name"] if reference else "purchase-details.html"
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()
