"""Bounded, offline RFC 5322 import for client email exported as .eml.

The envelope and headers are UNVERIFIED claims, not authenticated sender identity.
Only a single human-readable body becomes invoice input. Attachments, embedded
messages, quoted HTML threads, and non-text MIME parts never become financial
facts. Nothing in this module opens a URL or touches a payment provider.
"""
from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_EML_BYTES = 2_097_152
MAX_BODY_BYTES = 128_000
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class _TextFromHtml(HTMLParser):
    _skip_tags = {"script", "style", "noscript", "template", "svg", "head", "form", "iframe", "blockquote"}
    _blocks = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "section", "article", "table"}
    _void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, *, preserve_quotes=False, preserve_links=False):
        super().__init__(convert_charrefs=True)
        self.preserve_quotes = preserve_quotes
        self.preserve_links = preserve_links
        self.links: list[tuple[int, str, int]] = []
        self.parts: list[tuple[str, int]] = []
        self.skipped: list[str] = []
        self.context: list[tuple[str, bool, bool]] = []

    def _append(self, text):
        # Mail-client wrappers often contain a blockquote for the same quoted
        # region. Do not invent an extra nesting level for that wrapper.
        depth = max(sum(block for _, block, _ in self.context),
                    int(any(wrapper for _, _, wrapper in self.context)))
        self.parts.append((text, depth))

    @staticmethod
    def _web_target(value):
        if not value:
            return None
        value = value.strip(" \t\r\n\f")
        if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                return value
        except ValueError:
            pass
        return None

    def _finish_links(self, context_index):
        while self.links and self.links[-1][0] >= context_index:
            _, target, start = self.links.pop()
            label = " ".join("".join(text for text, _ in self.parts[start:]).split())
            if label != target:
                self._append(" (" + target + ")" if label else target)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").lower()
        hidden_quote = any(label in classes for label in ("gmail_quote", "yahoo_quoted", "protonmail_quote"))
        if self.skipped:
            if tag not in self._void_tags:
                self.skipped.append(tag)
            return
        skip_tag = tag in self._skip_tags and not (self.preserve_quotes and tag == "blockquote")
        if skip_tag or (hidden_quote and not self.preserve_quotes):
            self.skipped.append(tag)
            return
        quote = self.preserve_quotes and (tag == "blockquote" or hidden_quote)
        if quote:
            self._append("\n")
        if tag not in self._void_tags:
            self.context.append((tag, self.preserve_quotes and tag == "blockquote",
                                 self.preserve_quotes and hidden_quote))
        if tag == "a" and self.preserve_links:
            target = self._web_target(attributes.get("href"))
            if target:
                self.links.append((len(self.context) - 1, target, len(self.parts)))
        if tag in self._blocks or quote:
            self._append("\n")
            if tag == "li":
                self._append("- ")
        elif tag in ("td", "th"):
            self._append(" ")

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
            return
        matching = next((i for i in range(len(self.context) - 1, -1, -1)
                         if self.context[i][0] == tag), None)
        if matching is not None:
            self._finish_links(matching)
        quote = matching is not None and any(self.context[matching][1:])
        if tag in self._blocks or quote:
            self._append("\n")
        elif tag in ("td", "th"):
            self._append(" ")
        if matching is not None:
            del self.context[matching:]

    def handle_data(self, data):
        if not self.skipped:
            self._append(data)

    def text(self):
        # Prefix each nonempty quoted line, including wrapped lines and inline
        # spans. These markers preserve quotation, not authenticated authorship.
        self._finish_links(0)
        lines, current = [], []
        line_depth = 0
        line_has_text = False
        for text, depth in self.parts:
            pieces = text.split("\n")
            for index, piece in enumerate(pieces):
                if piece.strip() and not line_has_text:
                    line_depth = depth
                    line_has_text = True
                current.append(piece)
                if index < len(pieces) - 1:
                    value = " ".join("".join(current).split())
                    if value:
                        lines.append("> " * line_depth + value)
                    current = []
                    line_depth = 0
                    line_has_text = False
        value = " ".join("".join(current).split())
        if value:
            lines.append("> " * line_depth + value)
        return "\n".join(lines)


def html_email_text(body: str, *, preserve_quotes: bool = False,
                    preserve_links: bool = False) -> str:
    """Read email text without loading active content or external resources.

    Invoice requests default to excluding older quoted terms. Refund case review
    can explicitly retain quoted content with neutral > markers. These preserve
    quotation boundaries, not verified speakers or message chronology. Optional
    web-link destinations remain text; no URL is fetched or authenticated.
    """
    reader = _TextFromHtml(preserve_quotes=preserve_quotes, preserve_links=preserve_links)
    reader.feed(body)
    reader.close()
    return reader.text()


def _body_parts(part):
    """Return only author-visible body candidates, not attached or forwarded MIME."""
    if part.defects:
        raise ValueError("Malformed MIME part; export the client email again")
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return []
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        return []
    if part.is_multipart():
        candidates = [candidate for child in part.iter_parts() for candidate in _body_parts(child)]
        if part.get_content_subtype() == "alternative":
            plain = [c for c in candidates if c.get_content_type() == "text/plain"]
            html = [c for c in candidates if c.get_content_type() == "text/html"]
            if len(plain) > 1 or len(html) > 1:
                raise ValueError("Ambiguous alternate email bodies; export a single message")
            return plain[:1] or html[:1]
        return candidates
    if content_type in ("text/plain", "text/html"):
        return [part]
    return []


def _mailboxes(message, name, *, required=False):
    values = message.get_all(name, [])
    if not values:
        if required:
            raise ValueError("Exported email is missing a From address")
        return []
    if len(values) != 1:
        raise ValueError("Repeated " + name + " headers in exported email")
    addresses = getaddresses([str(values[0])])
    if not addresses or any(not _EMAIL.fullmatch(address) for _, address in addresses):
        raise ValueError("Invalid " + name + " address in exported email")
    if len(addresses) > 20 or (required and len(addresses) != 1):
        raise ValueError("Ambiguous " + name + " addresses in exported email")
    for display_name, address in addresses:
        if any(hit.group(0).lower() != address.lower()
               for hit in _EMAIL.finditer(display_name)):
            raise ValueError(name + " display name contains a conflicting email address")
    return addresses


# These are separators generated by ordinary mail clients when the visible
# newest reply precedes older conversation content. Exclude the older thread
# so historical prices cannot become a new financial request.
_REPLY_SEPARATORS = (
    re.compile(r"^-{3,}\s*(?:original message|forwarded message)\s*-{3,}$", re.I),
    re.compile(r"^On\s+.{5,200}\s+wrote:\s*$", re.I),
)


_REPLY_HEADER = re.compile(r"^(From|Sent|Date|To|Cc|Subject):\s*(.*)$", re.I)


def _outlook_reply_header(lines: list[str], start: int) -> bool:
    """Recognize the complete English Outlook reply envelope, not a lone label.

    Outlook exports often omit the Original Message banner. Require ordered
    sender, sent date, recipient and subject fields, with actual email addresses
    in both address fields. A project note such as "From: draft to final" must
    not hide the current request. Empty spacing and wrapped address/date lines
    occur when an HTML message is rendered as text.
    """
    fields: list[tuple[str, str]] = []
    for line in lines[start:start + 16]:
        text = line.strip()
        if not text:
            continue
        match = _REPLY_HEADER.match(text)
        if match:
            name, value = match.group(1).lower(), match.group(2)
            expected = ("from",) if not fields else {
                "from": ("sent", "date"), "sent": ("to",), "date": ("to",),
                "to": ("cc", "subject"), "cc": ("subject",),
            }.get(fields[-1][0], ())
            if name not in expected:
                return False
            fields.append((name, value))
            if name == "subject":
                values = dict(fields)
                return (bool(_EMAIL.search(values["from"]))
                        and bool(_EMAIL.search(values["to"]))
                        and bool(values.get("sent", values.get("date", "")).strip()))
        elif fields and line[:1].isspace():
            name, value = fields[-1]
            fields[-1] = (name, value + " " + text)
        else:
            return False
    return False


def _latest_message_only(body: str) -> str:
    kept = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if (any(pattern.match(line.strip()) for pattern in _REPLY_SEPARATORS)
                or (line.lstrip().lower().startswith("from:")
                    and _outlook_reply_header(lines, index))):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _safe_header(value):
    # A folded or encoded header cannot create a second synthetic From/To line.
    return " ".join(str(value).replace("\x00", " ").split()).strip()[:500]


def parse_eml(raw: bytes) -> str:
    """Produce the existing RulesExtractor's From/To/Subject + text-body format."""
    if len(raw) > MAX_EML_BYTES:
        raise ValueError("Exported email exceeds the 2 MiB intake limit")
    if not raw or b"\x00" in raw:
        raise ValueError("Exported email is empty or contains binary NUL bytes")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if message.defects:
            raise ValueError("Malformed email headers; export the message again")
        from_addresses = _mailboxes(message, "from", required=True)
        to_addresses = _mailboxes(message, "to")
        reply_addresses = _mailboxes(message, "reply-to")
        sender_name, sender_address = from_addresses[0]
        if reply_addresses and (len(reply_addresses) != 1
                                or reply_addresses[0][1].lower() != sender_address.lower()):
            raise ValueError("Reply-To differs from From; verify the client and use a corrected plain text request")
        candidates = _body_parts(message)
        if len(candidates) != 1:
            raise ValueError("Expected one readable email body; attachments and forwards are not invoice requests")
        selected = candidates[0]
        body = selected.get_content()
        # Some transfer-encoding defects surface only upon payload decode.
        if selected.defects:
            raise ValueError("Malformed encoded email body; export the message again")
        if not isinstance(body, str):
            raise ValueError("Email body could not be decoded as text")
        if selected.get_content_type() == "text/html":
            body = html_email_text(body)
    except (LookupError, UnicodeError, KeyError) as exc:
        raise ValueError("Email body has an unsupported or invalid character encoding") from exc
    body = _latest_message_only(body.replace("\r\n", "\n").replace("\r", "\n"))
    if not body or "\x00" in body or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("Email body is empty, binary or too large after decoding")
    # These claims are unverified. The human must review the resolved recipient
    # and all invoice terms at the unchanged CREATE/SEND gates.
    from_value = (_safe_header(sender_name) + " <" + sender_address.lower() + ">"
                  if sender_name.strip() else sender_address.lower())
    to_value = ", ".join(address.lower() for _, address in to_addresses)
    subject = _safe_header(message.get("subject", ""))
    return f"From: {from_value}\nTo: {to_value}\nSubject: {subject}\n\n{body}\n"
