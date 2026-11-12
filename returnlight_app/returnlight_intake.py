"""Read an exported buyer email into the existing, editable case intake."""
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import re
from .email_input import html_email_text
from .returnlight import MAX_BUYER_TEXT_CHARS

MAX_EMAIL_BYTES = 10 * 1024 * 1024

def email_message_ids(value: str) -> list[str]:
    ids = re.findall(r"<[^<>@\s]{1,250}@[^<>@\s]{1,250}>", value)
    return [item for item in ids if len(item) <= 254 and
            all(33 <= ord(char) <= 126 for char in item)]

def email_thread_context(message) -> dict:
    own_headers = message.get_all("Message-ID", [])
    own = email_message_ids(str(own_headers[0])) if len(own_headers) == 1 else []
    refs = email_message_ids(str(message.get("References", "")))
    refs += email_message_ids(str(message.get("In-Reply-To", "")))
    refs = list(dict.fromkeys(reversed(refs)))[::-1]
    return {**({"message_id": own[0]} if len(own) == 1 else {}),
            **({"reply_references": refs[-32:]} if refs else {})}


def email_date_context(message) -> dict:
    """Retain one interpretable origin Date as message context, never receipt time."""
    dates = [value for name, value in message.raw_items() if name.lower() == "date"]
    if len(dates) != 1:
        return {}
    value = re.sub(r"[ \t]+", " ", re.sub(r"\r?\n[ \t]+", " ", dates[0])).strip(" ")
    if not value or len(value) > 998 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return {}
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return {}
    # Parsing does not authenticate the field or establish complete RFC conformance.
    # Preserve its text and offset, including the -0000 unknown-local-zone spelling.
    return {"date": value}


def _email_body_text(part):
    try:
        body = part.get_content().strip()
        reference_body = body
        if part.get_content_type() == "text/html":
            reference_body = html_email_text(body, preserve_quotes=True)
            body = html_email_text(body, preserve_quotes=True, preserve_links=True)
    except (LookupError, UnicodeError) as exc:
        raise ValueError("This email uses an unsupported text encoding. Paste the buyer message instead.") from exc
    return body, reference_body


def _quoted_attached_email(item, number):
    """Keep attached message words as quoted evidence, never current identity."""
    payload = item.get_payload()
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    attached = payload[0]
    if not attached.get("From") or not attached.get("Subject"):
        return None
    headers = {"From": str(attached["From"]), "Subject": str(attached["Subject"])}
    if any(not value.strip() or len(value) > 998 or
           any(ord(char) < 32 or ord(char) == 127 for char in value)
           for value in headers.values()):
        return None
    part = attached.get_body(preferencelist=("plain", "html"))
    if part is None or part.get_content_type() not in {"text/plain", "text/html"}:
        return None
    try:
        body, _ = _email_body_text(part)
    except ValueError:
        return None
    if not body:
        return None
    date = email_date_context(attached).get("date")
    lines = ["From: " + headers["From"]]
    if date:
        lines.append("Date: " + date)
    lines += ["Subject: " + headers["Subject"], "", *body.splitlines()]
    return "Attached email " + str(number) + " (quoted source)\n" + "\n".join(
        "> " + line if line else ">" for line in lines)


def import_buyer_email(raw: str | bytes, *, store_photo=None) -> dict:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > MAX_EMAIL_BYTES:
        raise ValueError("Choose an email no larger than 10 MB.")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.get("From") or not message.get("Subject"):
        raise ValueError("Choose an exported email with From and Subject headers.")
    headers = {"subject": str(message["Subject"]), "sender": str(message["From"])}
    if any(not value.strip() or len(value) > 998 or
           any(ord(char) < 32 or ord(char) == 127 for char in value)
           for value in headers.values()):
        raise ValueError("Email headers must be single lines of up to 998 characters. Paste the buyer message instead.")
    reply_headers = message.get_all("Reply-To", [])
    if reply_headers:
        reply_to = ", ".join(str(value) for value in reply_headers)
        if len(reply_to) > 998 or any(ord(char) < 32 or ord(char) == 127 for char in reply_to):
            raise ValueError("Reply-To must be a single line of up to 998 characters.")
        # Retain an explicitly empty or ambiguous header too: it must not silently
        # turn into permission to address the original sender.
        headers["reply_to"] = reply_to
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None or part.get_content_type() not in {"text/plain", "text/html"}:
        raise ValueError("This email has no readable message. Paste the buyer message instead.")
    body, reference_body = _email_body_text(part)
    if not 1 <= len(body) <= MAX_BUYER_TEXT_CHARS:
        raise ValueError("The buyer message must contain 1 to 15000 characters. The complete email has not been imported.")
    parts = list(message.walk())
    quoted_parts = set()
    for item in parts:
        if item.get_content_type() != "message/rfc822":
            continue
        quoted = _quoted_attached_email(item, len(quoted_parts) + 1)
        if quoted is not None:
            body += "\n\n" + quoted
            quoted_parts.add(id(item))
            if len(body) > MAX_BUYER_TEXT_CHARS:
                raise ValueError("The buyer message must contain 1 to 15000 characters. The complete email has not been imported.")
    attachments_omitted = 0
    photos = []
    for item in parts:
        if id(item) in quoted_parts:
            continue
        attachment = item is not part and (item.get_content_disposition() == "attachment" or
            (not item.is_multipart() and item.get_content_maintype() not in {"text", "multipart"}))
        if not attachment:
            continue
        photo = None
        content_type = item.get_content_type()
        supported = content_type in {"image/png", "image/jpeg", "application/pdf"} or (
            content_type == "application/octet-stream" and (item.get_filename() or "").lower().endswith(".pdf"))
        if store_photo and len(photos) < 6 and supported:
            photo = store_photo(item.get_payload(decode=True), item.get_filename())
        if photo:
            photos.append(photo)
        else:
            attachments_omitted += 1
    captures = set(re.findall(r"(?im)^\s*(?:PayPal\s+)?capture\s*(?:ID|reference)\s*:\s*([A-Za-z0-9]{8,40})\s*$", reference_body))
    orders = set(re.findall(r"(?im)^\s*PayPal\s+order\s*(?:ID|reference)\s*:\s*([A-Za-z0-9]{8,40})\s*$", reference_body))
    return {"order_id": next(iter(orders)) if len(orders) == 1 else "", "complaint": body, "capture_id": next(iter(captures)) if len(captures) == 1 else "",
            **headers, **email_date_context(message), **email_thread_context(message), "attachments_omitted": attachments_omitted, **({"photos": photos} if photos else {})}
