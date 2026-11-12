"""Job email / plain text -> structured invoice draft.

Two extractors share one output type (`Extraction`) and one validator:

* `RulesExtractor`  - deterministic regex/heuristic parser. No network, no model.
* `LLMExtractor`    - pluggable: you inject `complete(prompt) -> str`. No live LLM
                      call is made anywhere in this repo; tests use a fake.
* `HybridExtractor` - rules first, falls back to the LLM when confidence is low.

Every result has a 0..1 `confidence` and field-level `issues`
(severity: error | warning | info). Errors block invoice creation in the agent
until a human fixes them.
"""
from __future__ import annotations

import ast
import json
import math
import re
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any, Callable, Optional, Protocol

# Subset of PayPal-supported currency codes.
# https://developer.paypal.com/api/rest/reference/currency-codes/
PAYPAL_CURRENCIES = {
    "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY",
    "MYR", "MXN", "TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF",
    "THB", "USD",
}
# Currencies PayPal treats as having no decimal places (same reference page).
ZERO_DECIMAL_CURRENCIES = {"JPY", "HUF", "TWD"}
DOLLAR_CURRENCIES = {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN", "TWD"}

SYMBOLS = {
    "US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD",
    "NZ$": "NZD", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
}

SEVERITY_PENALTY = {"error": 0.25, "warning": 0.10, "info": 0.0}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SYM = "|".join(re.escape(s) for s in sorted(SYMBOLS, key=len, reverse=True))
_CODE = "|".join(sorted(PAYPAL_CURRENCIES))
# Consume a complete number-like token first. _dec checks comma grouping, so
# malformed text cannot be silently shortened to its first valid numeric prefix.
_NUM = r"[+-]?(?:\d(?:[\d,]*\d)?(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_NO_SUFFIX = r"(?!\d|[\d.,]*[kKmM]\b|[eE]|[.,]\d)"
MONEY_RE = re.compile(
    rf"(?:(?P<sign>[+-])?(?P<pre>{_SYM}|\b(?:{_CODE})\b)\s?(?P<num>{_NUM}){_NO_SUFFIX}"
    rf"|(?<![\w.,])(?P<num2>{_NUM}){_NO_SUFFIX}\s?(?P<post>\b(?:{_CODE})\b|€|£))"
)
_MONEY_CANDIDATE_RE = re.compile(rf"(?:{_SYM}|\b(?:{_CODE})\b)\s*(?:[+\-\d(]|\.\d)")
_GROUPED_NUMBER_RE = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[eE][+-]?\d+)?")

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_Q_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
_Q = (
    r"(?:(?:~\s*|approx\.?\s*|about\s+|around\s+|roughly\s+)?"
    + _Q_NUMBER + r"(?:\s*(?:-|–|to)\s*" + _Q_NUMBER + r")?"
    r"|(?:a\s+few|a\s+couple(?:\s+of)?|several|some|"
    + "|".join(_WORD_NUMS) + r")\b)"
)
_UNITS = (
    r"(?:hours?|hrs?|h|days?|pages?|words?|units?|revisions?|sessions?|items?|"
    r"pcs?|photos?|articles?|posts?|videos?|weeks?|months?|screens?|episodes?|workshops?|files?|panels?|guides?)"
)
_HOUR_UNIT = r"(?:hours?|hrs?|h)"
_REQUEST_QUANTITY_PREFIX = re.compile(
    rf"^(?:please\s+)?(?:invoice|bill)(?:\s+(?:me|us))?\s+(?:the\s+)?"
    rf"(?={_Q}\s*(?:(?:[A-Za-z][\w-]*\s+){{0,4}}{_UNITS}\b|(?:x|×|\*)\s*<M>\s+))", re.I
)
# A named billing recipient is request framing, not the work quantity.
# Keep the following explicit quantity/unit intact; do not infer missing hours.
_AGREED_QUANTITY_PREFIX = re.compile(rf"^we(?:['’]re|\s+are)\s+going\s+with\s+(?={_Q}\s)", re.I)
_COMPLETED_QUANTITY_PREFIX = re.compile(rf"^(?:(?:the\s+)?(?:agreed\s+)?completed\s+work\s+(?:was|is|included)|we\s+completed)\s+(?={_Q}\s)", re.I)
_NAMED_INVOICE_RECIPIENT = re.compile(
    rf"^(?:please\s+)?(?:invoice|bill)\s+"
    rf"(?P<recipient>[A-Za-z][A-Za-z .'’\-]{{0,80}})(?:\s+at\s+(?P<email>{EMAIL_RE.pattern})|\s*<(?P<angle>{EMAIL_RE.pattern})>)?\s+for\s+",
    re.I
)
_NAMED_REQUEST_QUANTITY_PREFIX = re.compile(
    _NAMED_INVOICE_RECIPIENT.pattern + rf"(?={_Q}\s*{_UNITS}\b)", re.I
)
_HOURLY_RATE_SUFFIX = re.compile(
    rf"^\s*(?:(?:{_CODE})\b\s*)?(?:/\s*|per\s+){_HOUR_UNIT}\b", re.I
)
_COUNTED_REQUEST_RATE_RE = re.compile(
    rf"^(?:please\s+)?(?:invoice|bill)(?:\s+(?:me|us))?\s+(?:the\s+)?"
    rf"(?P<qty>{_Q})\s+(?P<desc>[^<>]+?)\s*(?:@|\bat\b)\s*<M>"
    rf"\s*(?:(?:{_CODE})\b\s*)?(?:/|\bper\s+)(?P<unit>[A-Za-z]+)\b",
    re.I,
)


def _count_unit_key(value: str) -> str:
    """Compare ordinary singular/plural count units without changing quantities."""
    value = value.lower().rstrip(".")
    if re.fullmatch(_HOUR_UNIT, value, re.I):
        return "hour"
    if len(value) > 3 and value.endswith("ies"):
        return value[:-3] + "y"
    if re.search(r"(?:ches|shes|xes|zes|sses)$", value):
        return value[:-2]
    return value[:-1] if value.endswith("s") and not value.endswith("ss") else value


_LEADING_QUANTITY_UNIT = re.compile(rf"^{_Q}\s*(?P<unit>{_UNITS})\b", re.I)
_RATE_UNIT_SUFFIX = re.compile(
    rf"^\s*(?:(?:{_CODE})\b\s*)?(?:/\s*|per\s+)(?P<unit>[A-Za-z]+)\b", re.I
)
_INVOICE_CONTEXT_PREFIX = re.compile(
    r"^(?:for|regarding)\s+[^,.;!?<>\n]{1,120},\s*"
    r"(?=(?:please\s+)?(?:invoice|bill)\s+)", re.I,
)
_PRICED_LIST_BOUNDARY_RE = re.compile(
    rf"(?:,\s+(?:(?:and|plus)\s+)?|\s+(?:and|plus)\s+)"
    rf"(?={_Q}\s|(?:(?:a|an|the)\s+)?(?:fixed|flat|agreed)\s+(?:{_SYM}|(?:{_CODE})\b)"
    rf"|(?:(?:the|a|an)\s+)?(?:[A-Za-z][\w'’\-]*\s+){{0,10}}(?:fee|charge)\s+"
    rf"(?:is|was|of)\s+(?:a\s+)?(?:(?:fixed|flat)\s+)?(?:{_SYM}|(?:{_CODE})\b))",
    re.I,
)
_EXPLICIT_RATE_TAIL_RE = re.compile(
    r"\s*(?:(?:/\s*|per\s+)(?P<unit>[A-Za-z]+)|each)?\s*[.!]?\s*", re.I,
)
_EXPLICIT_ITEM_BOUNDARY_RE = re.compile(rf"(?:\s*,?\s+\b(?:and|plus)\s+|\s*;\s*)(?={_Q}\s)", re.I)
_PRICE_FIRST_ITEM_RE = re.compile(rf"^(?P<qty>{_Q})\s*(?:x|×|\*)\s*<M>\s+"
               r"(?!(?:per|each)\b)(?P<desc>[^\W\d_][\w &'’()/-]*?)[.!]?$", re.I)
_STATEMENT_ITEM_PATTERNS = [
    re.compile(rf"^(?:the\s+)?(?P<desc>.+?)\s+(?:was|were|is|are)\s+(?P<qty>{_Q})\s*(?P<unit>{_UNITS})\b\s*(?:@|\bat\b|x|×)\s*<M>", re.I),
    re.compile(r"^(?:the\s+)?(?P<desc>.+?)\s+(?:was|were|is|are)\s+(?:a\s+)?flat(?:\s+fee(?:\s+of)?)?\s+<M>", re.I),
]
_ITEM_PATTERNS = [
    *_STATEMENT_ITEM_PATTERNS,
    # Explicit hours with the work after the rate: "7 hours at $95/hr for editing".
    re.compile(rf"^(?P<qty>{_Q})\s*(?P<unit>{_HOUR_UNIT})\b\s*(?:@|\bat\b|x|×)\s*<M>"
               rf"(?:\s*(?:{_CODE})\b)?(?:\s*(?:/\s*|per\s+){_HOUR_UNIT}\b)?"
               r"(?:\s+(?:for|of)\s+(?P<desc>.+?))?[.!]?$", re.I),
    # Explicit multiplication with the price before the work description.
    # A rate-only suffix is not a work name and needs human clarification.
    _PRICE_FIRST_ITEM_RE,
    # "3 x Logo concepts @ $250 each"
    re.compile(rf"^(?P<qty>{_Q})\s*(?:x|×|\*)\s*(?P<desc>.+?)\s*(?:@|\bat\b|\bfor\b)\s*<M>", re.I),
    # "12 hours of copy editing at £45/hr"
    re.compile(rf"^(?P<qty>{_Q})\s*(?P<unit>{_UNITS})\b\s+(?:of\s+)?(?P<desc>.+?)\s*(?:@|\bat\b|\bfor\b|x|×)\s*<M>", re.I),
    # A counted noun before "of" names the unit without a fixed vocabulary.
    re.compile(rf"^(?P<qty>{_Q})\s+(?P<unit>[A-Za-z]+)\s+of\s+(?P<desc>.+?)\s*(?:@|\bat\b|\bfor\b)\s*<M>", re.I),
    # "4 product photos at $75 each" / "a few revisions at $40 each"
    re.compile(rf"^(?P<qty>{_Q})\s+(?P<desc>.+?)\s*(?:@|\bat\b)\s*<M>", re.I),
    # "Proofreading: 3 hrs @ £40/hr"
    re.compile(rf"^(?P<desc>.+?)\s*[:–—-]\s*(?P<qty>{_Q})\s*(?P<unit>[A-Za-z]+\.?)?\s*(?:@|\bat\b|x|×)\s*<M>", re.I),
    # "Website build — €2,400.00"  (qty 1; separator must touch the price)
    re.compile(r"^(?P<desc>.+?)\s*(?:[:–—-]|@|\bat\b|\bfor\b)\s*<M>", re.I),
]
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")
_PAID_RE = re.compile(
    r"\b(deposit|already paid|paid upfront|advance payment|prepaid|have paid|we paid|retainer paid)\b", re.I
)
_PAYMENT_KIND = r"(?:deposit|retainer|advance payment|payment)"
_PAYMENT_AMOUNT = (
    rf"(?:(?:(?:a|an|the) )?<M>(?: {_PAYMENT_KIND})?"
    rf"|(?:(?:a|an|the) )?{_PAYMENT_KIND} of <M>)"
)
_PAID_CONFIRMED_RE = re.compile(
    rf"(?:(?:(?:we|i) (?:have |had )?(?:already )?paid|already paid|paid upfront|prepaid)"
    rf":? {_PAYMENT_AMOUNT}"
    rf"|{_PAYMENT_AMOUNT} (?:(?:has|have|had) (?:already )?been (?:paid|received)"
    rf"|(?:was|were) (?:already )?(?:paid|received)|(?:already )?paid)(?: and confirmed)?"
    rf"|{_PAYMENT_KIND} (?:already )?paid: <M>)"
    r"(?: (?:upfront|in advance|yesterday|today|last week|last month)"
    r"| (?:by|via) (?:bank transfer|wire transfer|cash|check|cheque))*[.!]?", re.I
)
def _confirmed_payment_statement(line: str) -> bool:
    """Use the same complete received-payment wording in both extractors."""
    prices = list(MONEY_RE.finditer(line))
    if len(prices) != 1:
        return False
    price = prices[0]
    statement = re.sub(r"\s+", " ", line[:price.start()] + "<M>" + line[price.end():])
    return _PAID_CONFIRMED_RE.fullmatch(statement.strip()) is not None


_TOTAL_RE = re.compile(r"\b(total|subtotal|budget|balance|altogether)\b", re.I)
# A summary word can also be part of a work description ("Budget planning").
# Only complete labels identify totals when a priced item has been parsed.
# Currency, tax and payment metadata may appear on either side of the label.
_SUMMARY_METADATA = (
    r"(?:the|our|your|grand|net|gross|overall|estimated|agreed|approved|final|"
    r"invoice|project|account|current|outstanding|remaining|amount|labor|labour|"
    r"materials?|services?|expenses?|annual|yearly|quarterly|monthly|weekly|daily|"
    r"pre[- ]tax|post[- ]tax|cost|price|charges?|fees?|cap|estimate|"
    r"total|subtotal|budget|balance|"
    rf"{_CODE}|{_SYM}|"
    r"tax|vat|included|excluded|inc\.?|incl\.?|excl\.?|"
    r"due|payable|owed|owing|today|tomorrow|now|immediately|on|upon|receipt|"
    r"to\s+(?:pay|invoice|bill|be\s+(?:paid|invoiced|billed)))"
)
_SUMMARY_LABEL_RE = re.compile(
    rf"(?:{_SUMMARY_METADATA}\s+)*"
    r"(?:total|sub[ -]?total|budget|balance|altogether)"
    rf"(?:\s+{_SUMMARY_METADATA})*"
    r"(?:\s+(?:for|of|before|after|including|excluding|incl\.?|excl\.?|on|"
    r"as\s+of)\s+.+)?",
    re.I,
)
_BILLING_RE = re.compile(
    r"\b(?:send|email|address|forward)\s+(?:the\s+|your\s+|all\s+)?invoices?\s+to\s+(" + EMAIL_RE.pattern + ")",
    re.I,
)
_DOC_CCY_RE = re.compile(
    rf"\b(?:prices?|amounts?|rates?|figures?|billed|bill|invoice)\s+(?:are\s+|is\s+|us\s+)?in\s+({_CODE})\b",
    re.I,
)
# A complete positive invoice-relative instruction fixes the start date.
# Zero days is not receipt-based, and is not silently mapped to due-on-receipt.
_INVOICE_RELATIVE_DUE_RE = re.compile(
    r"(?:^|(?<=[.!?;\n]))[ \t]*(?:payment\s+(?:is\s+)?due|payable)"
    r"\s+(?:within\s+)?([1-9]\d{0,2})\s+days?\s+(?:after|from|of)\s+(?:the\s+)?invoice(?:\s+date)?"
    r"[ \t]*(?=$|[.!?;\n])", re.I,
)
_DUE_PATTERNS = [
    re.compile(r"\bnet\s*[- ]?\s*(\d{1,3})\b", re.I),
    re.compile(r"\bdue\s+(?:with)?in\s+(\d{1,3})\s+days\b", re.I),
    re.compile(r"\b(?:pay|paid|payment)\b[^.\n]{0,30}?\bwithin\s+(\d{1,3})\s+days\b", re.I),
    re.compile(r"\bwithin\s+(\d{1,3})\s+days\b", re.I),
    re.compile(r"\b(\d{1,3})[- ]day\s+(?:payment\s+)?terms?\b", re.I),
    _INVOICE_RELATIVE_DUE_RE,
]
_DUE_ON_RECEIPT_RE = re.compile(r"\b(?:due\s+(?:up)?on\s+receipt|payable\s+immediately)\b", re.I)
_SIGNOFF_RE = re.compile(
    r"^(thanks|thank you|thx|best|regards|cheers|best regards|kind regards|warm regards|sincerely)[,!.]?\s*$", re.I
)
_HEADER_KEYS = {"from", "to", "cc", "subject", "date", "reply-to"}


@dataclass
class Issue:
    field: str
    severity: str  # error | warning | info
    message: str

    def to_dict(self) -> dict:
        return {"field": self.field, "severity": self.severity, "message": self.message}


@dataclass
class LineItem:
    desc: str
    qty: Optional[Decimal]
    unit_price: Decimal
    currency: Optional[str] = None
    unit: Optional[str] = None

    def amount(self) -> Decimal:
        return (self.qty or Decimal(0)) * self.unit_price

    def to_dict(self) -> dict:
        return {
            "desc": self.desc,
            "qty": None if self.qty is None else str(self.qty),
            "unit_price": str(self.unit_price),
            "currency": self.currency,
            "unit": self.unit,
        }


_CALENDAR_DUE_PREFIX = r"\b(?:(?:payment\s+(?:is\s+)?)?due(?:\s+date)?\s*:?\s*(?:(?:on|by)\s+)?|pay\s+(?:by|on)\s+)"
_CALENDAR_DUE_RE = re.compile(
    _CALENDAR_DUE_PREFIX +
    r"(\d{4}-\d{2}-\d{2}|[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})\b", re.I
)
_AMBIGUOUS_DUE_RE = re.compile(
    _CALENDAR_DUE_PREFIX + r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b", re.I
)


_MONTH_END_PREFIX = _CALENDAR_DUE_PREFIX + r"(?:at\s+)?(?:the\s+)?"
_MONTH_END_EVIDENCE_RE = re.compile(
    _MONTH_END_PREFIX + r"(?:end[-\s]+of\b|month[-\s]+end\b|EOM\b)", re.I
)
_MONTH_END_DATE_RE = re.compile(
    _MONTH_END_PREFIX + r"end[-\s]+of[-\s]+([A-Za-z]+)\.?,?\s+(\d{4})\b", re.I
)


def _month_end_dates(text: str) -> tuple[list[str], bool]:
    """Resolve only a named month and year; relative month ends need review."""
    current = "\n".join(line for line in text.splitlines()
                        if not line.lstrip().startswith(">"))
    dates = []
    unresolved = False
    for evidence in _MONTH_END_EVIDENCE_RE.finditer(current):
        match = _MONTH_END_DATE_RE.match(current, evidence.start())
        resolved = None
        if match:
            month_name, year = match.groups()
            for fmt in ("%B %Y", "%b %Y"):
                try:
                    first = datetime.strptime(f"{month_name} {year}", fmt).date()
                    resolved = date(first.year, first.month,
                                    monthrange(first.year, first.month)[1]).isoformat()
                    break
                except ValueError:
                    pass
        if resolved is None:
            unresolved = True
        elif resolved not in dates:
            dates.append(resolved)
    return dates, unresolved


def calendar_due_dates(text: str) -> list[str]:
    """Read explicitly labelled calendar terms without using today's date."""
    dates = []
    for match in _CALENDAR_DUE_RE.finditer(text):
        raw = match.group(1)
        value = raw
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
                try:
                    value = datetime.strptime(raw.replace(",", ""), fmt).date().isoformat()
                    break
                except ValueError:
                    pass
        if value not in dates:
            dates.append(value)
    for value in _month_end_dates(text)[0]:
        if value not in dates:
            dates.append(value)
    return dates



# These are literal invoice-note instructions, separate from model generation.
# The full model prompt/schema remains unchanged.
DEFAULT_INVOICE_NOTE = "Thank you for your business."
_NOTE_REQUEST_RE = re.compile(
    r"^(?:please\s+)?(?:put|include|add|show|quote)\s+(?P<note>.+?)\s+"
    r"(?:on|in)\s+(?:the|this|your)\s+invoice\b(?P<tail>.*)$", re.I,
)
_NOTE_LABEL_RE = re.compile(r"^(?:invoice\s+notes?|billing\s+instructions?)\s*:\s*(?P<note>.+)$", re.I)
_NOTE_REFERENCE_RE = re.compile(
    r"^(?P<instruction>(?:please\s+)?use\s+)?(?P<label>purchase\s+order|p\.?o\.?|client\s+reference|project\s+reference|cost\s+cent(?:er|re))"
    r"(?:\s+(?:number|no\.?))?(?:\s*[:#]\s*|"
    r"\s+(?=[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*[.!]?$))(?P<value>.+)$", re.I,
)
_NOTE_HISTORY_RE = re.compile(r"^(?:On .+ wrote:|-+\s*(?:Original Message|Forwarded message)\s*-+)$", re.I)
_NOTE_QUALIFIER_RE = re.compile(r"\b(?:if|unless|either|instead|maybe|might)\b", re.I)


def _invoice_metadata_lines(line: str) -> list[str]:
    """Detach only complete trailing invoice instructions, preserving exact quotes.

    Splitting arbitrary sentences could hide a condition on the work. A trailing
    group is separate only when every sentence is a supported payment term or
    literal invoice note, with no monetary amount or percentage of its own.
    """
    if line.lstrip().startswith(">") or "|" in line:
        return [line]
    boundaries = list(re.finditer(r"(?<=[.!?;])\s+", line))
    if not boundaries:
        return [line]
    starts = [0] + [boundary.end() for boundary in boundaries]
    ends = [boundary.start() for boundary in boundaries] + [len(line)]
    parts = [line[start:end] for start, end in zip(starts, ends)]

    def metadata(part: str) -> bool:
        if MONEY_RE.search(part) or "%" in part:
            return False
        if any(pattern.fullmatch(part) for pattern in
               (_NOTE_REQUEST_RE, _NOTE_LABEL_RE, _NOTE_REFERENCE_RE)):
            return True
        term = part.strip().rstrip(".!?").strip()
        if _CALENDAR_DUE_RE.fullmatch(term):
            return True
        term = re.sub(r"^(?:payment\s+)?terms\s*:?\s*", "", term, flags=re.I)
        return bool(_DUE_ON_RECEIPT_RE.fullmatch(term)
                    or any(pattern.fullmatch(term) for pattern in _DUE_PATTERNS))

    for index in range(1, len(parts)):
        if all(metadata(part) for part in parts[index:]):
            return [line[:ends[index - 1]], *parts[index:]]
    return [line]


def source_invoice_note(text: str) -> tuple[str, list[Issue], list[str]]:
    """Copy explicit current invoice instructions; never ask the model to invent them."""
    _, body = _split_headers(text)
    notes, quotes, issues = [], [], []
    for raw in (clause for line in body.splitlines() for clause in _invoice_metadata_lines(line)):
        if raw.lstrip().startswith(">"):
            continue
        line = _BULLET_RE.sub("", raw).strip()
        if _NOTE_HISTORY_RE.fullmatch(line):
            break
        request = _NOTE_REQUEST_RE.fullmatch(line)
        label = _NOTE_LABEL_RE.fullmatch(line)
        reference = _NOTE_REFERENCE_RE.fullmatch(line)
        if request:
            note = request.group("note").strip()
            tail = request.group("tail").strip()
            if _NOTE_QUALIFIER_RE.search(tail):
                issues.append(Issue("note", "error",
                    "The invoice instruction has a condition. Confirm the note before creating the invoice."))
            elif tail and not re.fullmatch(r"[.!]?(?:\s*(?:so|for|to)\b.*)?", tail, re.I):
                issues.append(Issue("note", "error",
                    "Check the full client instruction and confirm the invoice note."))
        elif label:
            note = label.group("note").strip()
        elif reference:
            if reference.group("instruction") and not re.fullmatch(
                    r"[A-Za-z0-9._/-]+[.!]?", reference.group("value").strip()):
                issues.append(Issue("note", "error",
                    "Confirm the complete reference instruction before creating the invoice."))
            label_text = reference.group("label")
            if re.fullmatch(r"p\.?o\.?", label_text, re.I):
                label_text = "Purchase order"
            note = label_text[:1].upper() + label_text[1:] + " " + reference.group("value").strip()
        else:
            continue
        if len(note) >= 2 and (note[0], note[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}:
            note = note[1:-1]
        if re.match(r"purchase\s+order\b", note, re.I):
            note = "Purchase order" + re.sub(r"^purchase\s+order", "", note, count=1, flags=re.I)
        if note not in notes:
            notes.append(note)
            quotes.append(line)
    return "\n".join(notes) if notes else DEFAULT_INVOICE_NOTE, issues, quotes


@dataclass
class Extraction:
    client_name: Optional[str]
    client_email: Optional[str]
    currency: Optional[str]
    line_items: list[LineItem]
    due_days: Optional[int]
    amount_paid: Decimal = Decimal(0)
    issues: list[Issue] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "rules"
    due_date: Optional[str] = None
    note: str = DEFAULT_INVOICE_NOTE

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def currencies(self) -> list[str]:
        return sorted({li.currency for li in self.line_items if li.currency})

    def total(self) -> Decimal:
        return sum((li.amount() for li in self.line_items), Decimal(0))

    def invoice_note(self) -> str:
        note = self.note
        if self.amount_paid > 0 and len(self.currencies()) == 1:
            note += (" " if note else "") + (
                f"Customer-reported prior payment: {self.currency} {self.amount_paid}"
                "; not independently verified.")
        return note

    def to_dict(self) -> dict:
        return {
            "client_name": self.client_name,
            "client_email": self.client_email,
            "currency": self.currency,
            "line_items": [li.to_dict() for li in self.line_items],
            "due_days": self.due_days,
            **({"due_date": self.due_date} if self.due_date is not None else {}),
            "amount_paid": str(self.amount_paid),
            "total": str(self.total()),
            "confidence": self.confidence,
            "issues": [i.to_dict() for i in self.issues],
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Extraction":
        items = []
        for li in d.get("line_items") or []:
            qty = li.get("qty")
            items.append(
                LineItem(
                    desc=str(li.get("desc") or "").strip(),
                    qty=None if qty in (None, "") else _dec(qty),
                    unit_price=_dec(li.get("unit_price", 0)),
                    currency=(li.get("currency") or d.get("currency") or None),
                    unit=li.get("unit"),
                )
            )
        ex = cls(
            client_name=d.get("client_name"),
            client_email=d.get("client_email"),
            currency=d.get("currency"),
            line_items=items,
            due_days=None if d.get("due_days") is None else int(d["due_days"]),
            amount_paid=_dec(d.get("amount_paid") or 0),
            issues=[Issue(**i) for i in d.get("issues") or []],
            confidence=float(d.get("confidence") or 0.0),
            source=d.get("source", "dict"),
            due_date=d.get("due_date") or None,
            note=d.get("note", DEFAULT_INVOICE_NOTE),
        )
        return ex


class Extractor(Protocol):
    def extract(self, text: str) -> Extraction: ...


# ----------------------------------------------------------------- helpers

def _dec(v: Any) -> Decimal:
    literal = str(v).strip()
    if "," in literal and not _GROUPED_NUMBER_RE.fullmatch(literal):
        raise ValueError(f"ambiguous number grouping: {v!r}")
    try:
        value = Decimal(literal.replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValueError(f"not a number: {v!r}")
    if not value.is_finite():
        raise ValueError(f"not a finite number: {v!r}")
    # No accepted invoice can represent more than 28 integer digits in the
    # existing Decimal context. Refuse before rules summaries or deposit sums
    # perform arithmetic, including newly recognized exponent notation.
    if value.adjusted() > 27:
        raise ValueError(f"number exceeds supported invoice arithmetic range: {v!r}")
    return value


def quantize(value: Decimal, currency: Optional[str]) -> Decimal:
    exp = Decimal("1") if currency in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def compute_confidence(issues: list[Issue]) -> float:
    score = 1.0 - sum(SEVERITY_PENALTY.get(i.severity, 0.0) for i in issues)
    return round(max(0.0, min(1.0, score)), 2)


def parse_money(m: re.Match) -> tuple[Optional[str], Decimal, str]:
    """Return (currency_code, amount, raw_currency_token)."""
    tok = m.group("pre") or m.group("post") or ""
    num = m.group("num") or m.group("num2")
    number_group = "num" if m.group("num") else "num2"
    before = m.string[:m.start(number_group)]
    after = m.string[m.end(number_group):]
    # Unsupported grouping must not leave a valid-looking prefix/suffix price.
    # Require review instead of guessing the sender's numeric locale.
    if (re.search(r"\d(?:[^\S\r\n]|['’])+$", before)
            or re.match(r"[^\S\r\n]+\d|['’]\d", after)):
        raise ValueError("unsupported number grouping; confirm the complete amount")
    code = SYMBOLS.get(tok, tok.upper() if tok.upper() in PAYPAL_CURRENCIES else None)
    sign = m.group("sign")
    if sign and num.startswith(("+", "-")):
        raise ValueError("conflicting amount signs")
    return code, _dec((sign or "") + num), tok


def parse_qty(raw: str) -> tuple[Optional[Decimal], Optional[str]]:
    """Return (qty, ambiguity_reason). qty None means unknowable."""
    s = raw.strip().lower()
    if re.fullmatch(r"a\s+few|several|some", s):
        return None, f"vague quantity '{raw.strip()}'"
    if re.fullmatch(r"a\s+couple(?:\s+of)?", s):
        return Decimal(2), f"'{raw.strip()}' interpreted as 2"
    if s in _WORD_NUMS:
        return Decimal(_WORD_NUMS[s]), None
    approx = re.match(r"^(?:~\s*|approx\.?\s*|about\s+|around\s+|roughly\s+)", s)
    numeric = s[approx.end():] if approx else s
    rng = re.fullmatch(rf"({_Q_NUMBER})\s*(?:-|–|to)\s*({_Q_NUMBER})", numeric)
    if rng:
        try:
            lo = _dec(rng.group(1))
        except ValueError as error:
            return None, f"unreadable quantity '{raw.strip()}' ({error})"
        return lo, f"range '{raw.strip()}' - used lower bound {lo}; confirm with client"
    n = re.fullmatch(_Q_NUMBER, numeric)
    if not n:
        return None, f"unreadable quantity '{raw.strip()}'"
    try:
        q = _dec(n.group(0))
    except ValueError as error:
        return None, f"unreadable quantity '{raw.strip()}' ({error})"
    if approx:
        return q, f"approximate quantity '{raw.strip()}' - used {q}"
    return q, None


def _is_invoice_total_label(description: str) -> bool:
    # Subtotals, caps and remaining balances need not equal the full invoice.
    # A stated full total disagreement must use the existing human-review gate.
    return bool(
        re.search(r"\btotal\b", description, re.I)
        and not re.search(r"\b(?:sub[ -]?total|budget|balance)\b", description, re.I)
    )


def _is_summary_label(description: str) -> bool:
    # Formatting and currency annotations do not turn a total into priced work.
    label = re.sub(r"[*_`()]", " ", description)
    label = re.sub(r"\s+", " ", label).strip(" :;.–—-")
    return _SUMMARY_LABEL_RE.fullmatch(label) is not None


def _split_headers(text: str) -> tuple[dict[str, str], str]:
    lines = text.replace("\r\n", "\n").split("\n")
    headers: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z-]+):\s*(.*)$", lines[i])
        if not m or m.group(1).lower() not in _HEADER_KEYS:
            break
        headers[m.group(1).lower()] = m.group(2).strip()
        i += 1
    return headers, "\n".join(lines[i:])


def _parse_address(value: str) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    em = EMAIL_RE.search(value)
    email = em.group(0).lower() if em else None
    name = value
    if em:
        name = value[: em.start()] + value[em.end():]
    name = re.sub(r"[<>\"]", " ", name)
    paren = re.search(r"\(([^)]+)\)", name)  # "佐藤 健 (Ken Sato)" -> prefer latin alias
    if paren:
        name = paren.group(1)
    name = re.sub(r"\s+", " ", name).strip(" ,")
    return (name or None), email


def _signature_name(body: str) -> Optional[str]:
    lines = [l.strip() for l in body.split("\n")]
    for i, line in enumerate(lines):
        if _SIGNOFF_RE.match(line):
            for nxt in lines[i + 1:]:
                if not nxt:
                    continue
                if len(nxt.split()) <= 4 and not re.search(r"[\d@]", nxt):
                    return nxt
                break
    return None


# --------------------------------------------------------------- validation

def validate(ex: Extraction, source_text: Optional[str] = None) -> list[Issue]:
    """Checks shared by every extractor. Returns NEW issues (does not mutate)."""
    out: list[Issue] = []

    def finite(value: Any) -> bool:
        return (isinstance(value, (Decimal, int)) and not isinstance(value, bool)
                and (not isinstance(value, Decimal) or value.is_finite()))

    def money(value: Any, currency: Optional[str], field: str, *, positive: bool) -> bool:
        if not finite(value):
            out.append(Issue(field, "error", "Amount must be a finite number."))
            return False
        if value < 0 or (positive and value == 0):
            out.append(Issue(field, "error", "Amount must be positive." if positive else "Prior payment cannot be negative."))
            return False
        if currency not in PAYPAL_CURRENCIES:
            return False  # the currency issue is recorded separately
        try:
            rounded = quantize(Decimal(value), currency)
        except DecimalException:
            out.append(Issue(field, "error", "Amount is too large to represent at invoice precision."))
            return False
        if rounded != value:
            message = (f"{currency} does not support decimals on PayPal." if currency in ZERO_DECIMAL_CURRENCIES
                       else f"Amount has fractions smaller than a {currency} currency unit; review it before drafting.")
            out.append(Issue(field, "error", message))
            return False
        return True

    if not isinstance(ex.note, str):
        out.append(Issue("note", "error", "The invoice note must be text."))
    elif len(ex.note) > 4000:
        out.append(Issue("note", "error", "Keep the invoice note to at most 4000 characters."))
    if not ex.client_email:
        out.append(Issue("client_email", "error", "No client email found; PayPal needs a recipient email to send an invoice."))
    elif not EMAIL_RE.fullmatch(ex.client_email):
        out.append(Issue("client_email", "error", f"Malformed email {ex.client_email!r}."))
    elif source_text is not None and ex.client_email.lower() not in source_text.lower():
        out.append(Issue("client_email", "error", f"{ex.client_email} does not appear in the source text (possible hallucination)."))
    if not ex.client_name:
        out.append(Issue("client_name", "warning", "No client name found."))
    if not ex.line_items:
        out.append(Issue("line_items", "error", "No priced line items found."))
    valid_amounts = True
    currency_amounts: dict[str, list[Decimal]] = {}
    for n, li in enumerate(ex.line_items):
        qty_ok = finite(li.qty)
        if not qty_ok:
            out.append(Issue(f"line_items[{n}].qty", "error", "Quantity is required and must be a finite number."))
        elif not 0 < li.qty <= 1000000:
            out.append(Issue(f"line_items[{n}].qty", "error", "Quantity must be positive and no greater than 1000000."))
            qty_ok = False
        elif Decimal(li.qty).quantize(Decimal("0.00001")) != li.qty:
            out.append(Issue(f"line_items[{n}].qty", "error", "Quantity supports at most five decimal places."))
            qty_ok = False
        if not li.currency:
            out.append(Issue(f"line_items[{n}].currency", "error", "Currency unknown for this item."))
        elif li.currency not in PAYPAL_CURRENCIES:
            out.append(Issue(f"line_items[{n}].currency", "error", f"{li.currency} is not a supported invoice currency."))
        price_ok = money(li.unit_price, li.currency, f"line_items[{n}].unit_price", positive=True)
        if qty_ok and price_ok:
            # Refuse implicit sub-unit rounding, including fractional quantities.
            # This is Returnlight's exact-value policy, not a claimed PayPal rejection.
            with localcontext() as ctx:
                ctx.prec = max(ctx.prec, len(Decimal(li.qty).as_tuple().digits)
                               + len(Decimal(li.unit_price).as_tuple().digits) + 1)
                amount = Decimal(li.qty) * Decimal(li.unit_price)
            amount_ok = money(amount, li.currency, f"line_items[{n}].amount", positive=True)
            valid_amounts = valid_amounts and amount_ok
            if amount_ok:
                currency_amounts.setdefault(li.currency, []).append(amount)
        else:
            valid_amounts = False
    currency_totals: dict[str, Decimal] = {}
    for currency, amounts in currency_amounts.items():
        with localcontext() as ctx:
            ctx.prec = max(ctx.prec, max(len(amount.as_tuple().digits) for amount in amounts)
                           + len(str(len(amounts))) + 1)
            total = sum(amounts, Decimal(0))
        currency_totals[currency] = total
        if not money(total, currency, f"currency_totals[{currency}]", positive=True):
            valid_amounts = False
    ccys = ex.currencies()
    if len(ccys) > 1:
        out.append(Issue("currency", "warning",
                         f"Multiple currencies {ccys}; a PayPal invoice has one currency_code, so this will be split into {len(ccys)} invoices."))
    if not ex.currency:
        out.append(Issue("currency", "error", "Invoice currency is required."))
    elif ex.currency not in PAYPAL_CURRENCIES:
        out.append(Issue("currency", "error", f"{ex.currency} is not a PayPal-supported currency."))
    elif ccys and ex.currency not in ccys:
        out.append(Issue("currency", "error", f"Invoice currency {ex.currency} does not match item currencies {ccys}; review rather than convert the amounts."))
    if ex.due_days is not None and not (0 <= ex.due_days <= 365):
        out.append(Issue("due_days", "error", f"Implausible payment term: {ex.due_days} days."))
    if source_text is not None:
        current = "\n".join(line for line in source_text.splitlines()
                            if not line.lstrip().startswith(">"))
        month_ends, unresolved_month_end = _month_end_dates(current)
        if unresolved_month_end:
            out.append(Issue("due_date", "error",
                             "Month-end payment terms need a named month and year. Choose an exact due date."))
        elif month_ends:
            relative = _DUE_ON_RECEIPT_RE.search(current) or any(
                pattern.search(current) for pattern in _DUE_PATTERNS)
            if (ex.due_date not in month_ends
                    or len(calendar_due_dates(current)) != 1 or relative):
                out.append(Issue("due_date", "error",
                                 "Confirm one exact due date for the source month-end payment terms."))
    if ex.due_date is not None:
        try:
            if not isinstance(ex.due_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ex.due_date):
                raise ValueError()
            date.fromisoformat(ex.due_date)
        except ValueError:
            out.append(Issue("due_date", "error", "Choose a real calendar due date in YYYY-MM-DD format."))
        if ex.due_days is not None:
            out.append(Issue("due_date", "error", "Choose either an exact due date or a number of days, not both."))
        if source_text is not None and ex.due_date not in calendar_due_dates(source_text):
            out.append(Issue("due_date", "error", "The proposed due date has no matching calendar term in the source."))
    paid_ok = money(ex.amount_paid, ex.currency, "amount_paid", positive=False)
    if paid_ok and ex.amount_paid > 0:
        if isinstance(ex.note, str) and len(ex.note) <= 4000 and len(ex.invoice_note()) > 4000:
            out.append(Issue("note", "error",
                "Shorten the invoice note to leave room for the reported prior payment."))
        if len(ccys) > 1:
            out.append(Issue("amount_paid", "error", "Prior payment reported on a multi-currency job; assign it manually."))
        elif valid_amounts and ex.currency in currency_totals and ex.amount_paid >= currency_totals[ex.currency]:
            out.append(Issue("amount_paid", "error", f"Reported payment {ex.amount_paid} >= invoice total {currency_totals[ex.currency]}."))
    return out



_RECIPIENT_CONFLICT_MESSAGE = (
    "Conflicting explicit invoice recipients or delivery addresses; "
    "confirm the bill-to before drafting."
)


_BILLING_REVIEW_MESSAGE = (
    "Explicit billing instruction is conditional or unclear; "
    "confirm the bill-to name and email before drafting."
)


def _resolve_recipient(live: str, sender_name: str | None, sender_email: str | None):
    """Resolve bill-to instructions before, and independently of, item parsing.

    Recognize named invoice requests and explicit Client/Bill to blocks. A
    malformed explicit block is a review error, never permission to use From.
    """
    issues: list[Issue] = []
    recipients: list[tuple[str, str | None]] = []
    deliveries = {m.group(1).rstrip(".").lower() for m in _BILLING_RE.finditer(live)}
    # Find explicit request spans in the live document, including bullets,
    # greetings and wrapped prose. Recipient identity cannot depend on whether
    # the item parser happens to normalize the same framing.
    normalized = " ".join(live.split())
    for delivery in re.finditer(
        r"\b(?:send|email|address|forward)\s+(?:the\s+|your\s+|all\s+)?invoices?\s+to\s+",
        normalized, re.I,
    ):
        remainder = normalized[delivery.end():]
        first_sentence = re.split(r"(?<=[.!?])\s+", remainder, maxsplit=1)[0]
        named_delivery = re.match(
            rf"(?P<name>[A-Za-z][A-Za-z0-9 .'’&\-]{{0,80}}?),\s*"
            rf"(?P<email>{EMAIL_RE.pattern})\s*(?:[.!](?=\s|$)|$)",
            remainder, re.I,
        )
        if named_delivery or ("," in first_sentence and EMAIL_RE.search(first_sentence)):
            previous = list(re.finditer(r"[.!?]\s+", normalized[:delivery.start()]))
            context_start = previous[-1].end() if previous else 0
            context = normalized[context_start:delivery.start()] + first_sentence
            if not named_delivery or _QUALIFIED_WORK_RE.search(context) or re.search(
                r"\b(?:after|before|once|subject|pending|provided|until|except)\b",
                normalized[context_start:delivery.start()], re.I,
            ):
                issues.append(Issue("client_email", "error", _BILLING_REVIEW_MESSAGE))
            else:
                recipients.append((named_delivery["name"].strip(), named_delivery["email"]))
    for request_start in re.finditer(r"\b(?:please\s+)?(?:invoice|bill)\s+", normalized, re.I):
        remainder = normalized[request_start.start():]
        first_sentence = re.split(r"(?<=[.!?])\s+", remainder, maxsplit=1)[0]
        previous = list(re.finditer(r"[.!?]\s+", normalized[:request_start.start()]))
        context_start = previous[-1].end() if previous else 0
        context = normalized[context_start:request_start.start()] + first_sentence
        direct = re.match(
            rf"^(?:please\s+)?(?:invoice|bill)\s+(?P<email>{EMAIL_RE.pattern})(?=\s|[.!?]|$)",
            remainder, re.I,
        )
        if direct:
            if _QUALIFIED_WORK_RE.search(context):
                issues.append(Issue("client_email", "error", _BILLING_REVIEW_MESSAGE))
            else:
                deliveries.add(direct["email"].rstrip(".").lower())
            continue
        if re.search(r",\s*(?:billing|invoice)\s+e-?mail\b", first_sentence, re.I):
            billing = re.match(
                rf"^(?:please\s+)?(?:invoice|bill)\s+"
                rf"(?P<name>[A-Za-z][A-Za-z0-9 .'’&\-]{{0,80}}?),\s*"
                rf"(?:billing|invoice)\s+e-?mail(?:\s+address)?\s*:?\s*"
                rf"(?P<email>{EMAIL_RE.pattern})"
                rf"(?:\s*,?\s+in\s+(?:{'|'.join(sorted(PAYPAL_CURRENCIES))}))?"
                rf"\s*(?:[.!](?=\s|$)|$)", remainder, re.I,
            )
            # Keep conditions with their instruction, rather than turning
            # "if approved, invoice..." into an unconditional destination.
            if not billing or _QUALIFIED_WORK_RE.search(context):
                issues.append(Issue("client_email", "error", _BILLING_REVIEW_MESSAGE))
            else:
                recipients.append((billing["name"].strip(), billing["email"]))
            continue
        match = _NAMED_INVOICE_RECIPIENT.match(remainder)
        if match:
            recipient_name = match["recipient"].strip()
            self_reference = re.fullmatch(
                rf"(?:me|us)(?:\s+in\s+(?:{'|'.join(sorted(PAYPAL_CURRENCIES))}))?",
                recipient_name, re.I,
            )
            if not self_reference:
                recipients.append((recipient_name, match["email"] or match["angle"]))
        elif re.search(r"\bfor\b", re.split(r"(?<=[.!?])\s+", remainder, maxsplit=1)[0], re.I) and not re.match(
            rf"^(?:please\s+)?(?:invoice|bill)\s+(?:for\b|me\b|us\b|to\b|{_Q}\s*{_UNITS}\b)",
            remainder, re.I,
        ):
            issues.append(Issue("client_email", "error",
                                "Named invoice request could not be resolved; use a clear Bill to name and email before drafting."))
    lines = live.splitlines()
    for index, line in enumerate(lines):
        label = re.match(r"^\s*(?:bill[\s-]*to\b\s*:?|client\s*:)\s*(.*)$", line, re.I)
        if not label:
            continue
        value = label[1].strip()
        next_index = index + 1
        if not value and next_index < len(lines):
            value = lines[next_index].strip()
            next_index += 1
        address = re.fullmatch(
            rf"(?P<name>[A-Za-z][A-Za-z .'’&\-]{{0,80}}?)"
            rf"(?:\s*<(?P<angle>{EMAIL_RE.pattern})>|\s+at\s+(?P<at>{EMAIL_RE.pattern})"
            rf"|\s*,\s*(?P<comma>{EMAIL_RE.pattern})[.!]?)?",
            value,
        )
        if not address:
            issues.append(Issue("client_email", "error",
                                "Explicit bill-to or client block could not be resolved; supply a clear name and email before drafting."))
            continue
        recipient_email = address["angle"] or address["at"] or address["comma"]
        if next_index < len(lines):
            following = re.fullmatch(
                rf"\s*(?:(?:email|email address)\s*:\s*)?(?P<email>{EMAIL_RE.pattern})\s*",
                lines[next_index], re.I,
            )
            if following:
                if recipient_email and recipient_email.lower() != following["email"].lower():
                    issues.append(Issue("client_email", "error", "Conflicting email addresses in the bill-to block; confirm the recipient before drafting."))
                recipient_email = following["email"]
        recipients.append((address["name"].strip(), recipient_email))

    if any(issue.message == _BILLING_REVIEW_MESSAGE for issue in issues):
        return None, None, issues

    named = {name.casefold(): name for name, _ in recipients}
    explicit_emails = deliveries | {email.lower() for _, email in recipients if email}
    if len(named) > 1 or len(explicit_emails) > 1:
        issues.append(Issue("client_email", "error", _RECIPIENT_CONFLICT_MESSAGE))
        return None, None, issues
    name = next(iter(named.values()), sender_name)
    email = next(iter(explicit_emails), sender_email)
    if named and not explicit_emails and (name or "").casefold() != (sender_name or "").casefold():
        issues.append(Issue("client_email", "error",
                            "Named invoice recipient differs from the sender but has no explicit email; confirm the bill-to before drafting."))
    elif (name, email) != (sender_name, sender_email):
        issues.append(Issue("client_email", "info",
                            "Using the explicit bill-to or invoice delivery instruction instead of the request sender."))
    return name, email, issues


# ------------------------------------------------------------ rules extractor

_NOCHARGE_WORK_PATTERN = (
    r"\b(?:no[-\s]+(?:(?:extra|additional)[-\s]+)?(?:charge|cost|fee)|not[-\s]+(?:(?:to(?:[-\s]+be)?|be)[-\s]+)?(?:charge|charged|chargeable|bill|billed|billable|invoice|invoiced|payable)|without[-\s]+(?:(?:any|a)[-\s]+)?(?:charge|cost)|pro[-\s]+bono|gratis)\b"
)

_NONCHARGEABLE_WORK_PATTERN = (
    r"\b(?:waived|waiver|complimentary|free|included|non[- ]?billable|non[- ]?chargeable)\b|"
    + _NOCHARGE_WORK_PATTERN
)

_QUALIFIED_WORK_RE = re.compile(
    r"\b(?:optional|optionally|conditional|unless|exclude|excluded|excluding|cancelled|canceled)\b"
    r"|\b(?:if|do not|don't|not needed|not required)\b"
    r"|" + _NONCHARGEABLE_WORK_PATTERN, re.I,
)


def _work_sections(lines: list[str]) -> dict[int, str]:
    """Retain standalone source headings without changing priced item text."""
    sections: dict[int, str] = {}
    section = ""
    for index, line in enumerate(lines):
        if (not MONEY_RE.search(line)
                and re.fullmatch(r"\s*(?:#{1,6}\s+.+|[^:\n]+:)\s*", line)):
            section = line
        sections[index] = section
    return sections


def _discount_label(text: str) -> bool:
    """Recognize standalone adjustments, preserving work about discounts."""
    return bool(re.fullmatch(
        r"\s*(?:(?:please\s+)?(?:apply|deduct|subtract)\s+|less\s+)?"
        r"(?:the\s+)?(?:(?:loyalty|courtesy|agreed|promotional|early[ -]payment|project|invoice)\s+)*"
        r"(?:discount|credit)(?:\s*\([^\n]*\))?"
        r"(?:\s+(?:applied|agreed|of))?"
        r"(?:\s+(?:to|on|for)\s+(?:this|the)\s+(?:invoice|project|work|total))?"
        r"\s*[:—–.!-]?\s*", text, re.I))


def _tax_label(text: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:plus\s+)?(?:VAT|GST|sales\s+tax|tax)\s*[:—–-]?\s*", text, re.I))


def _stated_total_issues(
    ex: Extraction, stated_totals: list[tuple[Optional[str], Decimal, bool]],
) -> list[Issue]:
    """Use the same source-summary comparison for rules and AI proposals."""
    issues: list[Issue] = []
    for ccy, amt, explicit_total in stated_totals:
        if ex.line_items and (ccy in (None, ex.currency)) and len(ex.currencies()) <= 1:
            if amt not in (ex.total(), ex.total() - ex.amount_paid):
                severity = "error" if explicit_total else "warning"
                issues.append(Issue("line_items", severity, f"Stated total/budget {amt} differs from computed total {ex.total()}. Review and correct the invoice fields before drafting."))
        elif not ex.line_items:
            issues.append(Issue("line_items", "info", f"Message mentions a total/budget of {ccy or ''} {amt} but no itemised work."))
    return issues


_WORK_QUALIFICATION_MESSAGE = (
    "Source work is optional, conditional or excluded; review the "
    "client's instruction before including this charge."
)


class RulesExtractor:
    name = "rules"

    def __init__(self, default_due_days: int = 30):
        self.default_due_days = default_due_days

    def extract(self, text: str) -> Extraction:
        headers, body = _split_headers(text)
        live = "\n".join(l for l in body.split("\n") if not l.lstrip().startswith(">"))  # ignore quoted thread
        issues: list[Issue] = []

        name, email = _parse_address(headers.get("from", ""))
        _, to_email = _parse_address(headers.get("to", ""))
        name, email, recipient_issues = _resolve_recipient(live, name, email)
        issues += recipient_issues
        recipient_conflict = any(
            issue.message in {_RECIPIENT_CONFLICT_MESSAGE, _BILLING_REVIEW_MESSAGE} for issue in recipient_issues
        )
        if not email and not recipient_conflict:
            found = [e.lower() for e in EMAIL_RE.findall(live) if e.lower() != (to_email or "")]
            if found:
                email = found[0]
                issues.append(Issue("client_email", "info", f"Client email {email} taken from message body."))
        if not name and not recipient_conflict:
            name = _signature_name(live)
            if name:
                issues.append(Issue("client_name", "info", "Client name taken from signature."))

        doc_ccy_m = _DOC_CCY_RE.search(live)
        doc_ccy = doc_ccy_m.group(1).upper() if doc_ccy_m else None

        items, paid, stated_totals, item_issues = self._line_items(live, doc_ccy)
        issues += item_issues

        ccy_counts = Counter(li.currency for li in items if li.currency)
        currency = ccy_counts.most_common(1)[0][0] if ccy_counts else doc_ccy

        due_days = self._due_days(live)
        dates = calendar_due_dates(live)
        due_date = dates[0] if dates else None
        if len(dates) > 1:
            issues.append(Issue("due_date", "error", "The source contains different due dates; confirm the intended date before drafting."))
        if _AMBIGUOUS_DUE_RE.search(live):
            issues.append(Issue("due_date", "error", "An ambiguous numeric due date needs confirmation. Choose a date using YYYY-MM-DD."))
        if due_days is None and due_date is None and not _month_end_dates(live)[1]:
            due_days = self.default_due_days
            issues.append(Issue("due_days", "warning", f"No payment terms found; defaulted to Net {self.default_due_days}."))

        note, note_issues, _ = source_invoice_note(text)
        issues += note_issues
        ex = Extraction(name, email, currency, items, due_days, paid, source=self.name, due_date=due_date, note=note)
        if paid > 0:
            issues.append(Issue("amount_paid", "info", f"Client reports {paid} already paid; will be recorded against the invoice after approval."))
        issues += _stated_total_issues(ex, stated_totals)
        ex.issues = issues + validate(ex, text)
        ex.confidence = compute_confidence(ex.issues)
        return ex

    def _due_days(self, text: str) -> Optional[int]:
        if _DUE_ON_RECEIPT_RE.search(text):
            return 0
        for pat in _DUE_PATTERNS:
            m = pat.search(text)
            if m:
                return int(m.group(1))
        return None

    def _line_items(self, body: str, doc_ccy: Optional[str]):
        # A normal email paragraph can put its total after the priced work.
        # Split only an explicit total statement at a sentence boundary; never
        # split decimal points. Compound work is split below only when each
        # clause independently states a supported quantity and one unit price.
        lines = [
            clause
            for raw in body.split("\n")
            for framed in _invoice_metadata_lines(raw)
            for line in self._independent_invoice_sentences(framed)
            for clause in (
                [line] if "|" in line else re.split(
                    r"(?<=[.!?;])\s+(?=(?:the\s+)?(?:grand\s+|invoice\s+)?total\b)",
                    line, flags=re.I,
                )
            )
        ]
        # A complete payment-terms sentence is separate from the preceding work.
        # Keep qualifiers and additional work attached so compound admission still
        # refuses them; the due-date parser reads the unchanged original body.
        lines = [clause for line in lines for clause in re.split(
            r"(?<=[.!?;])\s+(?=(?:(?:payment\s+)?terms\s*:?\s*)?"
            r"(?:net\s+\d{1,3}(?:\s+days)?|due\s+on\s+receipt)\s*[.!]?\s*$)",
            line, flags=re.I,
        )]
        # A full calendar payment sentence is not part of the work name.
        # Only detach a complete supported term; trailing conditions stay attached.
        calendar_lines = []
        for line in lines:
            boundary = re.search(
                r"(?<=[.!?;])\s+(?=(?:payment\s+)?due\b|pay\s+(?:by|on)\b)",
                line, re.I,
            )
            if boundary and _CALENDAR_DUE_RE.fullmatch(
                    line[boundary.end():].strip().rstrip(".!").strip()):
                calendar_lines.extend((line[:boundary.start()], line[boundary.end():]))
            else:
                calendar_lines.append(line)
        lines = [clause for line in calendar_lines for clause in self._explicit_compound_lines(line)]
        items: list[LineItem] = []
        issues: list[Issue] = []
        stated: list[tuple[Optional[str], Decimal, bool]] = []
        paid = Decimal(0)
        payments: list[tuple[Optional[str], Decimal]] = []

        t_items, consumed, t_issues, t_totals = self._table(lines, doc_ccy)
        items += t_items
        issues += t_issues
        stated += t_totals

        sections = _work_sections(lines)
        for idx, raw in enumerate(lines):
            if idx in consumed:
                continue
            line = _BULLET_RE.sub("", raw).strip()
            if not line:
                continue
            percentage = re.search(r"\d+(?:\.\d+)?\s*%", line)
            if percentage and _tax_label(line[:percentage.start()] + line[percentage.end():]):
                issues.append(Issue("line_items", "error",
                    f"Tax percentage requires review: {line!r}. It has not been applied automatically. Confirm the agreed final line-item amounts before drafting."))
                continue
            if percentage and (_discount_label(line[:percentage.start()]) or _discount_label(line[:percentage.start()] + line[percentage.end():])):
                issues.append(Issue("line_items", "error",
                    f"Discount or credit requires review: {line!r}. Confirm the agreed net line-item amounts before drafting."))
                continue
            monies = list(MONEY_RE.finditer(line))
            if not monies:
                if _MONEY_CANDIDATE_RE.search(line):
                    issues.append(Issue("line_items", "error", f"Ambiguous monetary amount; review the entire priced line: {line!r}"))
                continue
            try:
                ccy, amt, tok = parse_money(monies[0])
            except ValueError as error:
                issues.append(Issue("line_items", "error", f"Unreadable monetary amount ({error}); review line: {line!r}"))
                continue
            if tok == "$" and doc_ccy and doc_ccy in DOLLAR_CURRENCIES:
                ccy = doc_ccy
            if (_discount_label(line[:monies[0].start()])
                    or _discount_label(line[:monies[0].start()] + line[monies[0].end():])):
                issues.append(Issue("line_items", "error",
                    f"Discount or credit requires review: {line!r}. It has not been added as work or applied automatically. Confirm the agreed net line-item amounts before drafting."))
                continue
            if _PAID_RE.search(line):
                # A payment label is not evidence that money was received. Keep
                # uncertain language out of amount_paid and require the existing
                # review flow before a draft can queue an external-payment record.
                # Match the complete statement, not a paid phrase embedded in
                # a condition, quotation, request or unsettled payment report.
                if _confirmed_payment_statement(line):
                    payments.append((ccy, amt))
                    try:
                        valid_payment = amt >= 0 and quantize(amt, ccy) == amt
                    except DecimalException:
                        valid_payment = False
                    if not valid_payment:
                        issues.append(Issue("amount_paid", "error", f"Prior payment {ccy} {amt} must be nonnegative and exactly representable in its currency; review it before drafting."))
                else:
                    issues.append(Issue("amount_paid", "error",
                                        f"Prior payment is not unambiguously confirmed; review the amount already received: {line!r}"))
                continue
            item, reason = self._item_from_line(line, monies[0])
            if _TOTAL_RE.search(line) and (item is None or _is_summary_label(item.desc)):
                stated.append((ccy, amt, _is_invoice_total_label(line[:monies[0].start()])))
                continue
            if item is None:
                # A single optional amount followed by an explicit instruction
                # to leave it out is not an omitted agreed charge. Merely
                # conditional/qualified prose still requires human review.
                if (len(monies) == 1
                        and _QUALIFIED_WORK_RE.search(sections[idx] + " " + line)
                        and re.search(r"[.!?]\s+leave\s+(?:that|it)\s+out(?:\s+for\s+now)?[.!]?\s*$",
                                      line, re.I)):
                    continue
                issues.append(Issue("line_items", "error",
                                    f"A source amount was not included in the invoice; review it before drafting: {line!r}"))
                continue
            item.currency = ccy or doc_ccy
            if tok == "$" and doc_ccy and doc_ccy in DOLLAR_CURRENCIES and doc_ccy != "USD":
                item.currency = doc_ccy
                issues.append(Issue(f"line_items[{len(items)}].currency", "warning", f"'$' interpreted as {doc_ccy} per the email."))
            if len(monies) > 1:
                issues.append(Issue("line_items", "error",
                                    f"Multiple monetary amounts on one priced line; review and separate every fee before drafting: {line!r}"))
            if reason:
                sev = "error" if item.qty is None else "warning"
                issues.append(Issue(f"line_items[{len(items)}].qty", sev, reason))
            if _QUALIFIED_WORK_RE.search(sections[idx] + " " + line):
                issues.append(Issue(
                    f"line_items[{len(items)}].unit_price", "error",
                    _WORK_QUALIFICATION_MESSAGE,
                ))
            items.append(item)
        if payments:
            with localcontext() as ctx:
                ctx.prec = max(ctx.prec, max(len(amount.as_tuple().digits) for _, amount in payments)
                               + len(str(len(payments))) + 1)
                paid = sum((amount for _, amount in payments), Decimal(0))
            item_currencies = {item.currency for item in items}
            payment_currencies = {currency for currency, _ in payments}
            if len(item_currencies) != 1 or payment_currencies != item_currencies:
                issues.append(Issue("amount_paid", "error", f"Prior payment currencies {sorted(payment_currencies)} do not match a single item currency {sorted(item_currencies, key=str)}; assign payments manually without currency conversion."))
        return items, paid, stated, issues

    @staticmethod
    def _explicit_review_amount(part: str) -> bool:
        """One local optional charge or reported payment needs a human decision."""
        prices = list(MONEY_RE.finditer(part))
        if len(prices) != 1 or re.search(
                r"\b(?:all|both|above|below|these|those|entire|whole|"
                r"previous|prior|following)\b", part, re.I):
            return False
        masked = part[:prices[0].start()] + "<M>" + part[prices[0].end():]
        return bool(
            re.match(r"^(?:the\s+)?optional\s+<M>\s+\w", masked, re.I)
            or re.match(
                r"^(?:accounts?\s+(?:say|said)\s+(?:they\s+)?|(?:we|i|they)\s+)"
                r"(?:have\s+)?(?:paid|transferred|sent)\s+<M>(?:\s|[.!;]|$)",
                masked, re.I)
        )

    @classmethod
    def _independent_invoice_sentences(cls, paragraph: str) -> list[str]:
        """Separate a complete work paragraph only when each sentence has a role.

        Unpriced conditions and unclear monetary sentences keep the original
        paragraph intact. Optional work and payment reports stay verbatim for
        review; separating them never makes them approved charges or payments.
        """
        if "|" in paragraph or paragraph.lstrip().startswith(">"):
            return [paragraph]
        parts = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        if len(parts) < 2:
            return [paragraph]
        result: list[str] = []
        work_count = 0
        for index, part in enumerate(parts):
            prices = list(MONEY_RE.finditer(part))
            if not prices:
                # Only a leading, explicit recipient request is neutral framing.
                # Do not detach an adjacent instruction about the quoted work.
                if (index != 0 or not EMAIL_RE.search(part)
                        or not re.search(r"\b(?:invoice|bill)\b", part, re.I)
                        or _QUALIFIED_WORK_RE.search(part)
                        or re.search(r"\b(?:only|after|before|once|subject|pending|"
                                     r"provided|until|except|rather|instead)\b", part, re.I)):
                    return [paragraph]
                result.append(part)
                continue
            if cls._explicit_review_amount(part):
                result.append(part)
                continue
            if _QUALIFIED_WORK_RE.search(part):
                return [paragraph]
            fee = cls._explicit_standalone_fee_line(part)
            candidates = [fee] if fee is not None else cls._explicit_compound_lines(part)
            for candidate in candidates:
                monies = list(MONEY_RE.finditer(candidate))
                if len(monies) != 1:
                    return [paragraph]
                try:
                    item, reason = cls._item_from_line(candidate, monies[0])
                except ValueError:
                    return [paragraph]
                if item is None or item.qty is None or reason or item.unit_price <= 0:
                    return [paragraph]
                if fee is None:
                    parsed = cls._item_parts(candidate, monies[0])
                    if parsed is None or not re.match(rf"^{_Q}\s", parsed[0], re.I):
                        return [paragraph]
                    if not cls._explicit_item_tail(candidate, monies[0], parsed[0], item):
                        return [paragraph]
                result.append(candidate)
                work_count += 1
        return result if work_count else [paragraph]

    @staticmethod
    def _explicit_standalone_fee_line(line: str) -> Optional[str]:
        """Normalize an explicit named charge without dropping qualifications."""
        if _QUALIFIED_WORK_RE.search(line) or _PAID_RE.search(line):
            return None
        prices = list(MONEY_RE.finditer(line))
        if len(prices) != 1:
            return None
        price = prices[0]
        named = None
        # A single named fee can precede its price as a complete statement.
        # Keep the whole tail constrained: a rate, condition or second charge
        # must not become an inferred one-time item.
        if re.fullmatch(r"\s*(?:,\s*(?:also\s+)?charged\s+once)?[.!]?\s*",
                        line[price.end():], re.I):
            named = re.fullmatch(
                r"\s*(?:(?:the|a|an)\s+)?(?:(?:agreed|approved)\s+)?"
                r"(?P<desc>[^\W\d_][\w &'’()/-]*?\b(?:fee|charge))"
                r"\s+(?:is|was|of)\s*(?:a\s+)?(?:(?:flat|fixed)\s+)?", line[:price.start()], re.I,
            )
        if named is None and re.fullmatch(
            r"\s*(?:(?:there(?:\s+is|['’]s)\s+(?:also\s+)?)|(?:(?:please\s+)?(?:add|include)\s+))?"
            r"(?:(?:a|an|the)\s+)?(?:(?:separate|additional|fixed|flat)\s+)?",
            line[:price.start()], re.I,
        ):
            named = re.fullmatch(
                r"\s+(?P<desc>[^\W\d_][\w &'’()/-]*?\b(?:fee|charge)"
                r"(?:\s+for\s+[^\W\d_][^\d\W]*(?:[ '’()/-]+[^\W\d_][^\d\W]*)*)?)"
                r"(?:\s+(?:also\s+)?applies)?[.!]?\s*", line[price.end():], re.I,
            )
        if named is None and re.fullmatch(
            r"\s*(?:(?:a|an|the)\s+)?(?:agreed|fixed|flat|one[- ]time)\s+",
            line[:price.start()], re.I,
        ) and not re.search(
            r"\b(?:payment|paid|transfer|hourly|pending|proposed|estimated|"
            r"subject|only|after|before|until)\b", line[price.end():], re.I,
        ):
            named = re.fullmatch(
                r"\s+(?P<desc>[^\W\d_][\w &'’()/-]*?)[.!]?\s*",
                line[price.end():], re.I,
            )
        if not named or len(named["desc"].split()) > 12 or re.search(
            r"\b(?:per|each|hours?|daily|weekly|monthly|annual|yearly|recurring|"
            r"deposit|retainer|discount|credit|refund(?:able)?|tax|vat|gst|and|or|plus|"
            r"total|subtotal|budget|balance)\b",
            named["desc"], re.I,
        ):
            return None
        return f"{named['desc']} at {price.group(0)}"

    @classmethod
    def _explicit_flat_fee_lines(cls, line: str) -> Optional[list[str]]:
        """Read an explicitly separate flat fee after quantified work."""
        joined = re.fullmatch(
            r"(?P<work>.+?)\s*,?\s+plus\s+(?:a\s+)?(?:separate\s+)?(?P<fee>.+)",
            line, re.I,
        )
        fixed = False
        if not joined:
            joined = re.fullmatch(
                r"(?P<work>.+?)\s*,?\s+(?:and|plus)\s+a\s+fixed\s+(?P<fee>.+)",
                line, re.I,
            )
            fixed = joined is not None
        if not joined:
            return None
        work, fee = joined["work"].strip(), joined["fee"].strip()
        if fee.lower().startswith("fixed "):
            fixed = True
            fee = fee[6:].lstrip()
        normalized = _REQUEST_QUANTITY_PREFIX.sub("",
            _NAMED_REQUEST_QUANTITY_PREFIX.sub("", work))
        if not re.match(rf"^{_Q}\s", normalized, re.I):
            return None
        # A clearly named no-charge fee can be separated for review without
        # authorizing it: retain its exact wording so the shared item qualifier
        # blocks that row. Never remove qualifications from the required work.
        fee_for_structure = re.sub(_NOCHARGE_WORK_PATTERN, "", fee, flags=re.I)
        if re.search(
            r"\b(?:if|unless|optional|conditional|estimated|potential|maybe|might|cancelled|canceled|recurring|recurrence|hourly|daily|weekly|monthly|annually|annual|yearly)\b"
            r"|\bmay\s+be\b|\bwhen\s+(?:approved|required|needed)\b|\bup\s+to\b"
            r"|" + _NONCHARGEABLE_WORK_PATTERN,
            normalized + " " + fee_for_structure, re.I,
        ):
            return None
        first_prices = list(MONEY_RE.finditer(work))
        fee_prices = list(MONEY_RE.finditer(fee))
        if len(first_prices) != 1 or len(fee_prices) != 1 or fee_prices[0].start() != 0:
            return None
        price = fee_prices[0]
        fee_description = fee[price.end():]
        for_work = re.match(r"\s+for\s+(?:(?:a|an|the)\s+)?", fee_description, re.I)
        if for_work:
            fee_description = " " + fee_description[for_work.end():]
        named = re.fullmatch(
            (r"\s+(?P<desc>[^\W\d_][\w &'’()/-]*)(?P<tail>[.!].*)?" if fixed or for_work else
             r"\s+(?P<desc>[^\W\d_][\w &'’()/-]*?\bfee)(?P<tail>.*)"),
            fee_description, re.I,
        )
        if not named or len(named["desc"].split()) > 12:
            return None
        if (fixed or for_work) and re.search(r"\b(?:and|or|plus)\b", named["desc"], re.I):
            return None
        if for_work and re.match(rf"^{_Q}\s", named["desc"], re.I):
            return None
        if re.search(
            r"\b(?:per|each|hours?|hrs?|days?|weeks?|months?|rate|tax|vat|gst|"
            r"deposit|retainer|discount|credit|refund(?:able)?)\b",
            named["desc"], re.I,
        ):
            return None
        tail = (named["tail"] or "").strip()
        if tail not in ("", ".", "!"):
            if not tail.startswith((".", "!")):
                return None
            terms = tail[1:].strip().rstrip(".!")
            terms = re.sub(r"^payment(?:\s+is)?\s+", "", terms, flags=re.I)
            if not (any(pattern.fullmatch(terms) for pattern in _DUE_PATTERNS)
                    or _DUE_ON_RECEIPT_RE.fullmatch(terms)):
                return None
        try:
            item, reason = cls._item_from_line(work, first_prices[0])
            _, amount, _ = parse_money(price)
        except ValueError:
            return None
        if item is None or item.qty is None or reason or amount <= 0:
            return None
        # Payment terms still come from the original body, not these item clauses.
        return [work, f"{named['desc']} at {price.group(0)}"]

    @classmethod
    def _explicit_statement_lines(cls, line: str) -> Optional[list[str]]:
        """Split semicolon statements only when every work amount is explicit."""
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 2 or _QUALIFIED_WORK_RE.search(line):
            return None
        for part in parts:
            monies = list(MONEY_RE.finditer(part))
            if len(monies) != 1:
                return None
            price = monies[0]
            masked = part[:price.start()] + "<M>" + part[price.end():]
            if not any(pattern.match(masked) for pattern in _STATEMENT_ITEM_PATTERNS):
                return None
            if not re.fullmatch(rf"\s*(?:(?:/\s*|per\s+){_UNITS}|each)?\s*[.!]?\s*", part[price.end():], re.I):
                return None
            try:
                item, reason = cls._item_from_line(part, price)
            except ValueError:
                return None
            if item is None or item.qty is None or reason:
                return None
        return parts

    @staticmethod
    def _explicit_item_tail(candidate: str, price: re.Match, normalized: str, item: LineItem) -> bool:
        """Keep a complete price-first item or the same explicit counted rate."""
        if _PRICE_FIRST_ITEM_RE.fullmatch(normalized):
            return True
        tail = _EXPLICIT_RATE_TAIL_RE.fullmatch(candidate[price.end():])
        return bool(tail and (
            not tail["unit"] or item.unit
            and _count_unit_key(tail["unit"]) == _count_unit_key(item.unit)
        ))

    @classmethod
    def _explicit_priced_list_lines(cls, line: str) -> Optional[list[str]]:
        """Admit a complete comma list only when every clause names its charge."""
        if _QUALIFIED_WORK_RE.search(line) or _PAID_RE.search(line):
            return None
        # Whitespace after the comma prevents splitting grouped monetary values.
        # A fixed fee remains a named one-time charge, never a tax or payment.
        parts = _PRICED_LIST_BOUNDARY_RE.split(line)
        if len(parts) < 2:
            return None
        result = []
        for part in parts:
            candidate = part.strip()
            fee = cls._explicit_standalone_fee_line(candidate)
            if fee is not None:
                price = list(MONEY_RE.finditer(fee))[0]
                # The existing fixed-fee grammar establishes quantity one.
                candidate = f"1 x {price.group(0)} {fee[:price.start()].removesuffix('at ').strip()}"
            monies = list(MONEY_RE.finditer(candidate))
            if len(monies) != 1:
                return None
            parsed = cls._item_parts(candidate, monies[0])
            if parsed is None or not re.match(rf"^{_Q}\s", parsed[0], re.I):
                return None
            try:
                item, reason = cls._item_from_line(candidate, monies[0])
            except ValueError:
                return None
            if item is None or item.qty is None or reason or item.unit_price <= 0:
                return None
            if not cls._explicit_item_tail(candidate, monies[0], parsed[0], item):
                return None
            result.append(candidate)
        return result

    @classmethod
    def _explicit_compound_lines(cls, line: str) -> list[str]:
        """Separate only fully stated priced items with explicit boundaries."""
        if "|" in line or _PAID_RE.search(line):
            return [line]
        fee = cls._explicit_standalone_fee_line(line)
        if fee is not None:
            return [fee]
        statements = cls._explicit_statement_lines(line)
        if statements is not None:
            return statements
        priced_list = cls._explicit_priced_list_lines(line)
        if priced_list is not None:
            return priced_list
        flat_fee = cls._explicit_flat_fee_lines(line)
        if flat_fee is not None:
            return flat_fee
        parts = _EXPLICIT_ITEM_BOUNDARY_RE.split(line)
        if len(parts) < 2:
            return [line]
        for part in parts:
            candidate = _BULLET_RE.sub("", part).strip()
            monies = list(MONEY_RE.finditer(candidate))
            if len(monies) != 1:
                return [line]
            parsed = cls._item_parts(candidate, monies[0])
            if parsed is None:
                return [line]
            normalized, _, _ = parsed
            if not re.match(rf"^{_Q}\s", normalized, re.I):
                return [line]
            # Either the price finishes an explicit quantity/rate clause, or
            # the complete shared price-first grammar names the work after it.
            # Item qualification is still checked against each original clause.
            try:
                item, reason = cls._item_from_line(candidate, monies[0])
            except ValueError:
                return [line]
            if item is None or item.qty is None or reason:
                return [line]
            if not cls._explicit_item_tail(candidate, monies[0], normalized, item):
                return [line]
        return parts

    @staticmethod
    def _item_parts(line: str, m: re.Match):
        """Keep interpretation and source quotations on the same item grammar."""
        s = line[:m.start()] + "<M>" + line[m.end():]
        context = _INVOICE_CONTEXT_PREFIX.match(s)
        if context and not (
            _QUALIFIED_WORK_RE.search(context[0])
            or re.search(
                r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                r"only|after|before|once|subject|pending|provided|until|except|"
                r"estimated|proposed|approximately|about|around|roughly)\b",
                context[0], re.I,
            )
        ):
            s = s[context.end():]
        s = _NAMED_INVOICE_RECIPIENT.sub("", s)
        request = _COUNTED_REQUEST_RATE_RE.match(s)
        if request:
            noun = re.search(r"([A-Za-z]+)$", request["desc"].strip())
            if noun and _count_unit_key(noun[1]) == _count_unit_key(request["unit"]):
                # The rate noun corroborates a counted work request. A client
                # called "Three Rivers" billed per day is not three river units.
                s = s[request.start("qty"):]
        s = _COMPLETED_QUANTITY_PREFIX.sub("", _REQUEST_QUANTITY_PREFIX.sub("", s))
        s = _AGREED_QUANTITY_PREFIX.sub("", s)
        for pattern in _ITEM_PATTERNS:
            match = pattern.search(s)
            if not match:
                continue
            if _EXPLICIT_ITEM_BOUNDARY_RE.search(s[match.end():]):
                # A second quantity after this rate is still separate work, even
                # when its missing price prevented complete compound splitting.
                continue
            desc = (match.group("desc") or match.groupdict().get("unit") or "").strip().rstrip(" :–—-,")
            if pattern is _PRICE_FIRST_ITEM_RE and _EXPLICIT_ITEM_BOUNDARY_RE.search(desc):
                # An unpriced second quantity must not become part of the first
                # work name when complete compound admission has refused it.
                continue
            if desc and len(desc.split()) <= 12:
                return s, match, desc
        return None

    @staticmethod
    def _item_from_line(line: str, m: re.Match) -> tuple[Optional[LineItem], Optional[str]]:
        _, price, _ = parse_money(m)
        parts = RulesExtractor._item_parts(line, m)
        if parts is None:
            return None, None
        s, pm, desc = parts
        gd = pm.groupdict()
        qty, reason = (Decimal(1), None) if gd.get("qty") is None else parse_qty(gd["qty"])
        unit = gd.get("unit")
        rate = _RATE_UNIT_SUFFIX.match(s.split("<M>", 1)[1])
        leading_unit = _LEADING_QUANTITY_UNIT.match(s)
        counted_noun = re.search(rf"\b(?P<unit>{_UNITS})$", desc, re.I)
        if counted_noun is None and rate:
            noun = re.search(r"\b(?P<unit>[A-Za-z]+)$", desc, re.I)
            if noun and _count_unit_key(noun["unit"]) == _count_unit_key(rate["unit"]):
                counted_noun = noun
        quantity_unit = unit or (leading_unit["unit"] if leading_unit else None) or (counted_noun["unit"] if counted_noun else None)
        if unit is None and counted_noun is not None and gd.get("qty") is not None:
            unit = counted_noun["unit"]
        def normalized_unit(value):
            return _count_unit_key(value)
        if quantity_unit and rate and normalized_unit(quantity_unit) != normalized_unit(rate["unit"]):
            qty = None
            reason = "Quantity unit and rate period differ; confirm the quantity and unit price without guessing a conversion."
        elif gd.get("qty") is None and _HOURLY_RATE_SUFFIX.match(s.split("<M>", 1)[1]):
            qty = None
            reason = "Hourly rate found without a supported quantity; confirm the hours before drafting."
        elif gd.get("qty") is None and rate:
            qty = None
            reason = "Unit rate found without a supported quantity; confirm the count before drafting."
        desc = desc[0].upper() + desc[1:]
        return LineItem(desc=desc, qty=qty, unit_price=price, unit=unit), reason

    @staticmethod
    def _table(lines: list[str], doc_ccy: Optional[str]):
        items: list[LineItem] = []
        consumed: set[int] = set()
        issues: list[Issue] = []
        totals: list[tuple[Optional[str], Decimal, bool]] = []
        rows = [(i, [c.strip() for c in l.strip().strip("|").split("|")]) for i, l in enumerate(lines) if l.count("|") >= 2]
        header = None
        sections = _work_sections(lines)
        for i, cells in rows:
            low = [c.lower() for c in cells]
            if header is None:
                if any(c in ("qty", "quantity", "hours", "hrs") for c in low):
                    def col(*names):
                        return next((k for k, c in enumerate(low) if any(n in c for n in names)), None)
                    header = {
                        "desc": col("item", "description", "service", "task") or 0,
                        "qty": col("qty", "quantity", "hours", "hrs"),
                        "price": col("unit price", "price", "rate", "unit"),
                    }
                    consumed.add(i)
                continue
            consumed.add(i)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            try:
                desc = cells[header["desc"]]
                raw_price = cells[header["price"]] if header["price"] is not None else ""
                raw_qty = cells[header["qty"]] if header["qty"] is not None else "1"
            except IndexError:
                issues.append(Issue("line_items", "warning", f"Malformed table row: {lines[i].strip()!r}"))
                continue
            if _tax_label(desc) and re.search(r"\d+(?:\.\d+)?\s*%", raw_price):
                issues.append(Issue("line_items", "error",
                    f"Tax percentage requires review: {lines[i].strip()!r}. Confirm the agreed final line-item amounts before drafting."))
                continue
            if _discount_label(desc):
                issues.append(Issue("line_items", "error",
                    f"Discount or credit requires review: {lines[i].strip()!r}. Confirm the agreed net line-item amounts before drafting."))
                continue
            mm = MONEY_RE.search(raw_price)
            if mm:
                try:
                    ccy, price, tok = parse_money(mm)
                except ValueError as error:
                    issues.append(Issue("line_items", "error", f"Unreadable table amount ({error}): {raw_price!r}"))
                    continue
                if tok == "$" and doc_ccy and doc_ccy in DOLLAR_CURRENCIES and doc_ccy != "USD":
                    ccy = doc_ccy
            else:
                bare = re.fullmatch(_NUM, raw_price)
                if not bare:
                    issues.append(Issue("line_items", "error", f"No unambiguous price in table row: {lines[i].strip()!r}"))
                    continue
                try:
                    ccy, price = None, _dec(bare.group(0))
                except ValueError as error:
                    issues.append(Issue("line_items", "error", f"Unreadable table amount ({error}): {raw_price!r}"))
                    continue
            if _is_summary_label(desc):
                totals.append((ccy or doc_ccy, price, _is_invoice_total_label(desc)))
                continue
            qty, reason = parse_qty(raw_qty)
            if reason:
                issues.append(Issue(f"line_items[{len(items)}].qty", "error" if qty is None else "warning", reason))
            if _QUALIFIED_WORK_RE.search(sections[i] + " " + desc):
                issues.append(Issue(
                    f"line_items[{len(items)}].unit_price", "error",
                    _WORK_QUALIFICATION_MESSAGE,
                ))
            items.append(LineItem(desc=desc, qty=qty, unit_price=price, currency=ccy or doc_ccy))
        return items, consumed, issues, totals


# ------------------------------------------------------------- LLM extractor

LLM_PROMPT = """You extract invoice data from a freelancer's job email.
Return ONLY a JSON object, no prose, matching:
{"client_name": string|null, "client_email": string|null, "currency": ISO-4217 string|null,
 "line_items": [{"desc": string, "qty": number|null, "unit_price": number, "currency": string|null, "unit": string|null}],
 "due_days": integer|null, "due_date": "YYYY-MM-DD"|null, "amount_paid": number, "confidence": number 0..1,
 "issues": [{"field": string, "severity": "error"|"warning"|"info", "message": string}]}
Rules: never invent an email address or price that is not in the text; use null and add an
issue instead. Quoted earlier messages (lines starting with '>') are history, not scope.
Extract the current client's name and email, including the current From header
when it identifies the client; an explicit bill-to instruction takes priority.
Do not omit an explicit recipient or use one from quoted earlier messages.
desc names the work; put counts, unit rates and currency in their separate fields.
Copy each stated unit rate exactly; never divide a quoted hourly rate by the quantity.
qty is the stated number of units, including number words. Do not use 1 for
multiple hours or hide the quantity only in desc. Use 1 only for a single flat fee.
Omit optional or explicitly excluded work; it is not part of the agreed invoice.
Copy the stated quantity unit (for example hours, pages or days) into unit; use null
when no unit is stated. Never replace hours with a generic item count.
The counted unit is the noun being billed, not a duration or size modifier.
Do not convert the length of a session into hours or days unless that is how it is priced.
A flat fee has quantity 1 and its stated amount as unit_price. Do not calculate
unit_price from a combined total.
Include every agreed separately priced service or fee, even on another line.
Check the entire email for work amounts missing from line_items before returning.
Totals, prior payments and excluded work are not additional invoice items.
If a deposit/partial payment was already made, put it in amount_paid.
Otherwise amount_paid is 0. A quoted fee is not a prior payment.

Net N means due_days N. Copy explicit payment terms, including terms on the work line.
Calendar deadlines use due_date (YYYY-MM-DD) and due_days null; relative terms use due_days and due_date null. Do not infer an absent or ambiguous calendar date.

EMAIL:
<<<
{text}
>>>"""



def source_references(ex: Extraction, text: str) -> dict:
    """Quote normalized values without changing an invoice or trusting model citations.

    References contain original text, not Python offsets. The browser locates
    that exact quotation in its unchanged source using its own UTF-16 offsets.
    An empty reference is deliberate: inferred, conflicting or repeated evidence
    must not turn into a source link.
    """
    _, body = _split_headers(text)
    live = "\n".join(line for line in body.split("\n")
                     if not line.lstrip().startswith(">"))
    references = {"line_items": [{"qty": None} for _ in ex.line_items],
                  "payment_terms": None}
    document_currency = _DOC_CCY_RE.search(live)
    document_currency = document_currency.group(1).upper() if document_currency else None
    candidates = []
    for raw in live.splitlines():
        line = _BULLET_RE.sub("", raw).strip()
        for price in MONEY_RE.finditer(line):
            clause, _ = _price_clause(line, price.start())
            clause = clause.strip()
            # Find the price again after trimming; require exactly one price so
            # a nearby fee cannot supply a different work item's quantity.
            monies = list(MONEY_RE.finditer(clause))
            if len(monies) != 1:
                continue
            try:
                currency, _, token = parse_money(monies[0])
                item, reason = RulesExtractor._item_from_line(clause, monies[0])
            except ValueError:
                continue
            if token == "$" and document_currency in DOLLAR_CURRENCIES:
                currency = document_currency
            if item is None or item.qty is None or reason is not None:
                continue
            parts = RulesExtractor._item_parts(clause, monies[0])
            raw_quantity = parts[1].groupdict().get("qty") if parts else None
            if not raw_quantity:
                continue
            match = parts[1]
            if match.groupdict().get("unit"):
                raw_quantity = parts[0][match.start("qty"):match.end("unit")]
            else:
                multiplier = re.match(r"\s*(?:x|×|\*)", parts[0][match.end("qty"):])
                if multiplier:
                    raw_quantity = parts[0][match.start("qty"):match.end("qty") + multiplier.end()]
            candidates.append((item, currency or document_currency, raw_quantity))

    for index, item in enumerate(ex.line_items):
        matches = [
            quote for candidate, currency, quote in candidates
            if re.search(r"(?<!\w)" + re.escape(item.desc) + r"(?!\w)",
                         candidate.desc, re.I)
            and candidate.qty == item.qty and candidate.unit_price == item.unit_price
            and currency == (item.currency or ex.currency)
            and (not item.unit or candidate.unit and
                 re.sub(r"^(?:hours?|hrs?|h)$", "hour", candidate.unit.lower()).rstrip("s")
                 == re.sub(r"^(?:hours?|hrs?|h)$", "hour", item.unit.lower()).rstrip("s"))
        ]
        if len(matches) == 1:
            references["line_items"][index]["qty"] = {
                "text": matches[0], "value": str(item.qty),
            }

    terms = []
    # Bare "within N days" may describe delivery. The same explicit-payment
    # patterns are already used when grounding a model's proposed terms.
    for pattern in (pattern for index, pattern in enumerate(_DUE_PATTERNS) if index != 3):
        for match in pattern.finditer(live):
            terms.append((match.start(), match.end(), "days", str(int(match.group(1)))))
    terms += [(match.start(), match.end(), "days", "0")
              for match in _DUE_ON_RECEIPT_RE.finditer(live)]
    for pattern in (_CALENDAR_DUE_RE, _MONTH_END_DATE_RE):
        for match in pattern.finditer(live):
            dates = calendar_due_dates(match.group(0))
            if len(dates) == 1:
                terms.append((match.start(), match.end(), "date", dates[0]))
    # Payment wording can match both "payment ... within" and "due within".
    # A contained match is one quotation; two separate instructions are not.
    terms = [term for term in terms if not any(
        other[:2] != term[:2] and other[0] <= term[0] and other[1] >= term[1]
        for other in terms)]
    terms = list(dict.fromkeys(terms))
    expected = ("date", ex.due_date) if ex.due_date else ("days", str(ex.due_days))
    if len(terms) == 1 and terms[0][2:] == expected:
        start, end, _, value = terms[0]
        line_start = live.rfind("\n", 0, start) + 1
        line_end = live.find("\n", end)
        context = live[line_start:line_end if line_end >= 0 else len(live)]
        if (not _QUALIFIED_WORK_RE.search(context)
                and not _AMBIGUOUS_DUE_RE.search(live)
                and not _month_end_dates(live)[1]):
            references["payment_terms"] = {"text": live[start:end], "value": value}
    note, note_issues, note_quotes = source_invoice_note(text)
    if ex.note == note and note != DEFAULT_INVOICE_NOTE and not note_issues and len(note_quotes) == 1:
        references["note"] = {"text": note_quotes[0], "value": note}
    return references


def _strip_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    return m.group(1) if m else s


# High-stakes invoice values require evidence beyond a model confidence score.
# A global list of source prices is insufficient: models can swap two line-item
# prices or reuse a quoted deposit as a work rate. Require each price on a
# non-quoted source line that actually names the associated work.
_COMMON_ITEM_WORDS = frozenset({
    "the", "for", "of", "and", "with", "your", "this", "that", "from",
    "hours", "hour", "items", "item", "each", "project", "service",
})


def _price_clause(line: str, offset: int) -> tuple[str, int]:
    """Match a price to its own deliverable, not the neighboring one."""
    start, end = 0, len(line)
    # Qualifications in a separate sentence must not merge two otherwise
    # distinct work/rate clauses here. Keep qualifiers attached within the
    # same sentence; sentence boundaries already delimit source evidence.
    sentence_start, sentence_end = 0, len(line)
    for boundary in re.finditer(r"[.!?]\s+(?=[A-Z])", line, re.I):
        if boundary.end() <= offset:
            sentence_start = boundary.end()
        elif boundary.start() >= offset:
            sentence_end = boundary.start()
            break
    qualified_sentence = bool(_QUALIFIED_WORK_RE.search(line[sentence_start:sentence_end]))
    boundaries = list(re.finditer(
        r";|[.!?]\s+(?=[A-Z])|,\s+(?=\d+\s*x\b)|\s+and\s+(?=\d+\s*x\b)", line, re.I
    ))
    # A separately priced flat fee is its own clause, not another rate for the
    # preceding quantified work. Require an actual price immediately after the
    # explicit separator; a service name containing "plus" is not a boundary.
    boundaries.extend(match for match in re.finditer(
        r"\s*,?\s+plus\s+(?:a\s+)?(?:separate\s+)?", line, re.I
    ) if MONEY_RE.match(line, match.end()))
    # A second explicitly fixed charge has its own work/rate evidence. Do not
    # split qualified work or payment adjustments: that could detach a warning
    # from the charge it qualifies or turn a deposit into a service.
    if not qualified_sentence:
        for match in re.finditer(
            r"\s*,?\s+and\s+(?:a\s+)?(?:fixed|flat|one[- ]time)\s+(?:fee\s+(?:of\s+)?)?",
            line, re.I,
        ):
            tail = line[match.end():]
            if (MONEY_RE.match(tail) and len(list(MONEY_RE.finditer(tail))) == 1
                    and not re.search(
                        r"\b(?:per|each|recurring|recurrence|hourly|daily|weekly|monthly|annually|annual|yearly|"
                        r"deposit|retainer|discount|credit|refund|tax|vat|gst|total|balance|budget|payment)\b",
                        tail, re.I)):
                # Retain "a fixed/flat fee" with its own charge. Models can
                # repeat that source wording in desc; removing it here creates
                # a false mismatch even when every word is grounded.
                separator = re.compile(r"\s*,?\s+and\s+", re.I).match(line, match.start())
                if separator:
                    boundaries.append(separator)
    if not qualified_sentence:
        boundaries.extend(re.finditer(
            rf"\s*,?\s+(?:and|plus)\s+(?={_Q}\s)", line, re.I,
        ))
        # Use the same complete-list admission as Rules before separating
        # source evidence. An incomplete or qualified list stays intact.
        sentence = line[sentence_start:sentence_end]
        if RulesExtractor._explicit_priced_list_lines(sentence) is not None:
            boundaries.extend(_PRICED_LIST_BOUNDARY_RE.finditer(
                line, sentence_start, sentence_end,
            ))
    for boundary in sorted(boundaries, key=lambda match: match.start()):
        if boundary.end() <= offset:
            start = boundary.end()
        elif boundary.start() >= offset:
            end = boundary.start()
            break
    return line[start:end], offset - start


def _source_qty_matches(qty: Decimal | None, clause: str) -> bool:
    """Conservative quantity evidence: never promote a vague estimate."""
    if qty is None:
        return False
    masked = MONEY_RE.sub(" PRICE ", clause)
    if re.search(
        r"(?i)\b(?:a\s+few|several|some|about|approx(?:imately)?|"
        r"around|roughly)\b|~\s*\d", masked
    ):
        return False
    # An exact counted unit at the start remains evidence when the work
    # description between it and the price is longer than the short prose form.
    leading_word = re.match(
        rf"^\s*[-*•]?\s*(?P<count>{'|'.join(_WORD_NUMS)})\s+(?:{_UNITS})\b",
        masked, re.I,
    )
    if leading_word:
        return qty == Decimal(_WORD_NUMS[leading_word["count"].lower()])
    patterns = (
        r"(?<!\w)(\d+(?:\.\d+)?)\s*(?:x\b|×|hours?\b|hrs?\b|units?\b|pages?\b)",
        r"^\s*[-*•]?\s*(\d+(?:\.\d+)?)\s+(?!PRICE\b)",
    )
    for pattern in patterns:
        m = re.search(pattern, masked, re.I)
        if m:
            try:
                return Decimal(m.group(1)) == qty
            except (InvalidOperation, TypeError):
                return False
    # Phrases like "two logo variants at $50" carry an exact count.
    word_counts = {
        "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    }
    for word, count in word_counts.items():
        if re.search(
            rf"(?i)\b{word}\s+(?:[\w-]+\s+){{1,5}}(?:at|for|@)\s+PRICE",
            masked,
        ):
            return qty == Decimal(count)
    # No quantity: a single unit may be inferred, but never a multiple.
    return qty == Decimal(1)


def _ground_model_unit(item: LineItem, clause: str, index: int) -> list[Issue]:
    """Preserve an explicit unit only beside the already grounded quantity."""
    def normalized(value: str) -> str:
        return _count_unit_key(value)

    units = []
    # Work can sit between its count and unit ("three editing hours"). Stop
    # at prices and rate words so "three items at USD 70 per hour" cannot
    # borrow the rate's unit as the counted unit.
    quantity_text = MONEY_RE.sub(" § ", clause)
    quantity_unit = (rf"(?<![\w.])(?P<qty>{_Q})\s*"
                     rf"(?:(?!(?:at|for|per|each)\b)[\w-]+\s+){{0,4}}?"
                     rf"(?P<unit>{_UNITS})\b")
    for match in re.finditer(quantity_unit, quantity_text, re.I):
        quantity, reason = parse_qty(match["qty"])
        if reason is None and quantity == item.qty:
            units.append(match["unit"])
    prices = list(MONEY_RE.finditer(clause))
    if len(prices) == 1:
        candidate, reason = RulesExtractor._item_from_line(clause, prices[0])
        if candidate is not None:
            if candidate.qty == item.qty and candidate.unit:
                units.append(candidate.unit)
    if not units:
        return []
    unique = {normalized(unit) for unit in units}
    rates = re.findall(rf"(?:/|\bper\s+)(?P<unit>{_UNITS})\b", clause, re.I)
    if len(unique) != 1 or any(normalized(rate) not in unique for rate in rates):
        return [Issue(f"line_items[{index}].unit", "error",
                      "Quantity unit and rate period differ or are ambiguous; review them without guessing a conversion.")]
    if item.unit and normalized(item.unit) not in unique:
        return [Issue(f"line_items[{index}].unit", "error",
                      "AI unit differs from the stated quantity unit; review the work and unit together.")]
    if not item.unit:
        item.unit = units[0]
    return []


def _work_word(word: str) -> str:
    """Conservative grammatical forms, not synonyms or fuzzy service matching."""
    if word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 6 and word.endswith("ation"):
        return word[:-3]  # translation -> translat; translated -> translat
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    return word


def _ground_model_terms(ex: Extraction, source: str) -> list[Issue]:
    """Recover a missing term only from one explicit current payment instruction."""
    current = "\n".join(line for line in source.splitlines()
                        if not line.lstrip().startswith(">"))
    # Bare "within N days" can describe delivery rather than payment.
    patterns = [pattern for index, pattern in enumerate(_DUE_PATTERNS) if index != 3]
    terms = {int(match.group(1)) for pattern in patterns for match in pattern.finditer(current)}
    if _DUE_ON_RECEIPT_RE.search(current):
        terms.add(0)
    dates = calendar_due_dates(current)
    if len(terms) > 1 or (terms and dates):
        return [Issue("due_days", "error",
                      "The source contains different payment terms; confirm one before drafting.")]
    if not terms or dates or _month_end_dates(current)[1]:
        return []
    term = next(iter(terms))
    if ex.due_date is not None or (ex.due_days is not None and ex.due_days != term):
        return [Issue("due_days", "error",
                      "The AI payment term differs from the explicit source term; review it before drafting.")]
    if ex.due_days is None:
        # Conditional or proposed terms need a human choice.
        if any(_QUALIFIED_WORK_RE.search(line) for line in current.splitlines()
               if any(pattern.search(line) for pattern in patterns)):
            return [Issue("due_days", "error",
                          "The source qualifies its payment terms; confirm them before drafting.")]
        ex.due_days = term
        return [Issue("due_days", "info",
                      f"Net {term} recovered from the explicit source payment instruction.")]
    return []


def _ground_model_prices(ex: Extraction, source: str) -> list[Issue]:
    """Bind each AI invoice item to a current quoted work price and quantity.

    The synthetic library interface remains optional; both real model entry
    points select this stronger policy. Evidence is local to a deliverable:
    project totals, deposits and other items cannot authorize a substituted
    unit price or a fabricated prior payment.
    """
    current = "\n".join(line for line in source.splitlines()
                        if not line.lstrip().startswith(">"))
    doc = _DOC_CCY_RE.search(current)
    doc_ccy = doc.group(1).upper() if doc else None
    candidates: list[tuple[Decimal, str, str, int]] = []
    payments: list[tuple[Decimal, str]] = []
    payment_issues: list[Issue] = []
    source_lines = current.splitlines()
    sections = _work_sections(source_lines)
    for line_no, line in enumerate(source_lines, 1):
        for match in MONEY_RE.finditer(line):
            clause, price_offset = _price_clause(line, match.start())
            if _confirmed_payment_statement(clause):
                try:
                    currency, amount, symbol = parse_money(match)
                    if symbol == "$" and doc_ccy in DOLLAR_CURRENCIES:
                        currency = doc_ccy
                    if not currency or amount < 0 or quantize(amount, currency) != amount:
                        raise ValueError("unsupported received payment")
                except (ValueError, DecimalException):
                    payment_issues.append(Issue(
                        "amount_paid", "error",
                        "The confirmed prior payment needs a valid amount and currency; review it before drafting.",
                    ))
                else:
                    payments.append((amount, currency))
                continue
            # Ignore a summary even if it appears on the same line as work.
            if re.match(
                r"(?i)^\s*[-*•]?\s*(?:total|subtotal|balance|budget|"
                r"amount\s+due|deposit\s+due|already\s+paid)\s*(?::|=|is\b)",
                clause,
            ):
                continue
            labels = list(re.finditer(
                r"(?i)\b(?:total|subtotal|balance|budget|deposit|retainer|"
                r"payment|paid|amount\s+due)\b", clause[:price_offset]
            ))
            if labels and not re.search(
                r"(?i)@|\b(?:at|per)\b",
                clause[labels[-1].end():price_offset],
            ):
                continue
            try:
                currency, amount, symbol = parse_money(match)
            except ValueError:
                continue
            if symbol == "$" and doc_ccy in DOLLAR_CURRENCIES:
                currency = doc_ccy
            if currency:
                candidates.append((amount, currency, clause, line_no))

    issues: list[Issue] = payment_issues
    for index, item in enumerate(ex.line_items):
        def description_words(clause: str) -> set[str]:
            # Models sometimes repeat the quantity as "(5 hours)" while the
            # source writes "five hours". Ignore only a complete annotation
            # independently supported by this candidate's quantity AND unit.
            # Numbers within work names (Logo 5, Phase 2) remain significant.
            def annotation(match: re.Match) -> str:
                count, unit = match.groups()
                unit = unit.lower().rstrip("s")
                if (item.qty == Decimal(count)
                        and _source_qty_matches(item.qty, clause)
                        and re.search(
                            rf"\b(?:\d+(?:\.\d+)?|one|two|three|four|five|six|"
                            rf"seven|eight|nine|ten|eleven|twelve)\s+{re.escape(unit)}s?\b",
                            clause, re.I,
                        )):
                    return " "
                return match.group(0)

            description = re.sub(
                r"\(\s*(\d+(?:\.\d+)?)\s+(hours?|hrs?|units?|pages?)\s*\)",
                annotation, item.desc, flags=re.I,
            )
            leading_count = re.match(r"^(\d+(?:\.\d+)?)\s+", description)
            if (leading_count and item.qty == Decimal(leading_count[1])
                    and _source_qty_matches(item.qty, clause)):
                description = description[leading_count.end():]
            # A model may echo a rate in desc. That price is not work identity:
            # compare it separately after identifying the service, so "Audio
            # USD 50" cannot choose between differently priced audio services.
            description = MONEY_RE.sub(" ", description)
            return {
                _work_word(word)
                for word in re.findall(r"[a-z0-9]+", description.lower())
                if word not in _COMMON_ITEM_WORDS
            }
        # Match the work independently of the number: choosing a candidate by
        # price first can certify the wrong service when two descriptions share
        # a generic word (e.g. "Audio mixing" and "Audio mastering").
        # A shortened description such as "Audio" is also ambiguous and must
        # not be allowed to choose a price by coincidence.
        matching_work = []
        for i, (_, _, clause, _) in enumerate(candidates):
            desc_words = description_words(clause)
            words = {
                _work_word(word)
                for word in re.findall(r"[a-z0-9]+", clause.lower())
            }
            if desc_words and desc_words.issubset(words):
                matching_work.append(i)
        if len(matching_work) != 1:
            issues.append(Issue(
                f"line_items[{index}].unit_price", "error",
                "AI work description does not uniquely identify one priced "
                "source clause; review the service and rate together.",
            ))
            continue
        matched = matching_work[0]
        price, currency, source_clause, source_line = candidates[matched]
        # A quoted rate does not authorize optional or excluded work.
        if _QUALIFIED_WORK_RE.search(sections[source_line - 1] + " " + source_clause):
            issues.append(Issue(
                f"line_items[{index}].unit_price", "error",
                _WORK_QUALIFICATION_MESSAGE,
            ))
            continue
        if price != item.unit_price or currency != (item.currency or ex.currency):
            issues.append(Issue(
                f"line_items[{index}].unit_price", "error",
                "AI price/currency differs from the unique source work-item rate.",
            ))
            continue
        _, _, clause, line_no = candidates.pop(matched)
        if not _source_qty_matches(item.qty, clause):
            issues.append(Issue(
                f"line_items[{index}].qty", "error",
                "AI quantity lacks an exact count beside the priced work item.",
            ))
        else:
            issues.extend(_ground_model_unit(item, clause, index))
        issues.append(Issue(
            f"line_items[{index}].unit_price", "info",
            f"AI price is quoted with matching work wording on source line {line_no}. "
            "Verify quantity and context manually.",
        ))

    # Every remaining current amount needs a disposition. Do not silently
    # accept a partial invoice merely because the rules parser also missed it.
    # Known totals/payments were filtered above; excluded work stays excluded.
    for _, _, clause, line_no in candidates:
        if _QUALIFIED_WORK_RE.search(sections[line_no - 1] + " " + clause):
            continue
        issues.append(Issue("line_items", "error",
            f"A source amount on line {line_no} is not accounted for in the AI "
            f"proposal; review its work or purpose before drafting: {clause.strip()}"))

    if (ex.client_email and ex.client_email.lower() in source.lower()
            and ex.client_email.lower() not in current.lower()):
        issues.append(Issue(
            "client_email", "error", "AI recipient appears only in quoted history.",
        ))
    if payments:
        with localcontext() as ctx:
            ctx.prec = max(ctx.prec, max(len(amount.as_tuple().digits) for amount, _ in payments)
                           + len(str(len(payments))) + 1)
            already_paid = sum((amount for amount, _ in payments), Decimal(0))
        currencies = {currency for _, currency in payments}
        if len(currencies) != 1 or currencies != set(ex.currencies()):
            issues.append(Issue(
                "amount_paid", "error",
                "The confirmed prior payment currency differs from the invoice; assign it manually.",
            ))
        elif already_paid != ex.amount_paid:
            issues.append(Issue(
                "amount_paid", "error",
                "AI prior payment differs from the amount explicitly confirmed as received; review it before drafting.",
            ))
    elif ex.amount_paid > 0:
        issues.append(Issue(
            "amount_paid", "error",
            "AI prior payment was not independently confirmed as already received.",
        ))
    return issues



def _missing_model_work(ex: Extraction, live: str, doc_ccy: Optional[str]) -> list[Issue]:
    """Notice omitted explicit work; never infer a complete invoice from prose."""
    if re.search(r"\b(?:if|unless|optional|instead|alternative|estimated|potential|"
                 r"maybe|might|exclude|excluding|cancelled|canceled|previous|earlier)\b"
                 r"|\b(?:do not|don't|up to|may be)\b", live, re.I):
        return []
    known, _, _, issues = RulesExtractor()._line_items(live, doc_ccy)
    if any(issue.severity in ("error", "warning") for issue in issues):
        return []
    known = [item for item in known if item.qty is not None and item.unit_price is not None
             and item.qty > 0 and item.unit_price > 0 and item.currency]
    result = []
    for currency in sorted({item.currency for item in known}):
        source_items = [item for item in known if item.currency == currency]
        try:
            source_total = quantize(sum((item.qty * item.unit_price for item in source_items), Decimal(0)), currency)
            proposed = quantize(sum((item.qty * item.unit_price for item in ex.line_items
                            if (item.currency or ex.currency) == currency), Decimal(0)), currency)
        except (DecimalException, ValueError):
            # Unsupported source arithmetic is not evidence of missing work.
            continue
        if proposed >= source_total:
            continue
        names = ", ".join(item.desc[:80] for item in source_items[:4])
        result.append(Issue("line_items", "error",
            f"Priced work in the current email totals {currency} {quantize(source_total, currency)}, "
            f"but the AI proposal contains {currency} {quantize(proposed, currency)}. "
            f"Review all listed work before drafting: {names}."))
    return result



class LLMExtractor:
    """Model-agnostic: inject any `complete(prompt) -> str` (OpenAI, Anthropic, local...).

    Output is never trusted blindly: it goes through the same `validate()` as the rules
    extractor plus a grounding check (email must literally appear in the source text).
    """

    name = "llm"

    def __init__(self, complete: Callable[[str], str], *, ground_prices: bool = False, check_missing_work: bool = False):
        self.complete = complete
        # Real operator entrypoints opt in. Existing library consumers retain
        # their generic structured-value contract until they select this policy.
        self.ground_prices = ground_prices
        self.check_missing_work = check_missing_work

    def extract(self, text: str) -> Extraction:
        raw = self.complete(LLM_PROMPT.replace("{text}", text))
        try:
            data = json.loads(_strip_fence(raw))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            model_issues = data.pop("issues", []) or []
            model_conf = float(data.pop("confidence", 0.5))
            if not math.isfinite(model_conf) or not 0 <= model_conf <= 1:
                raise ValueError("model confidence must be a finite number from 0 to 1")
            # Notes are sourced locally; extra model fields cannot supply instructions.
            data.pop("note", None)
            ex = Extraction.from_dict(data)
            ex.issues = [Issue(**i) for i in model_issues if isinstance(i, dict) and {"field", "severity", "message"} <= set(i)]
        except (ValueError, TypeError, KeyError) as e:
            bad = Extraction(None, None, None, [], None, source=self.name,
                             issues=[Issue("*", "error", f"LLM output unusable: {e}")])
            bad.confidence = 0.0
            return bad
        ex.source = self.name
        ex.note, note_issues, _ = source_invoice_note(text)
        ex.issues += note_issues
        if self.ground_prices and not ex.client_email:
            # Recover only an explicit current billing instruction. Never use
            # the sender or an arbitrary address to fill a model omission.
            _, body = _split_headers(text)
            live = "\n".join(line for line in body.split("\n")
                             if not line.lstrip().startswith(">"))
            source_name, source_email, recipient_issues = _resolve_recipient(live, None, None)
            clauses = [part for part in re.split(r"(?<=[.!?])\s+|\n", live)
                       if (source_email and source_email.lower() in part.lower())
                       or (not MONEY_RE.search(part) and re.search(
                           r"\b(?:if|unless|optional|only|subject|pending|provided|until|approved|approval)\b", part, re.I))]
            qualified = any(_QUALIFIED_WORK_RE.search(part) or re.search(
                r"\b(?:if|unless|after|before|once|subject|pending|provided|until|except)\b",
                part, re.I) for part in clauses)
            if (source_email and clauses and not qualified
                    and not any(issue.severity == "error" for issue in recipient_issues)):
                ex.client_email = source_email
                if not ex.client_name:
                    ex.client_name = source_name
                ex.issues.append(Issue("client_email", "warning",
                    "AI omitted the recipient email. Using the explicit current billing instruction; verify it before drafting."))
        if ex.client_email:
            ex.client_email = ex.client_email.lower()
        for li in ex.line_items:
            li.currency = li.currency or ex.currency
        if ex.currency is None and ex.currencies():
            ex.currency = Counter(li.currency for li in ex.line_items).most_common(1)[0][0]
        term_issues = _ground_model_terms(ex, text) if self.ground_prices else []
        v = validate(ex, text) + term_issues
        # A structured model response can still omit work. Reuse the established
        # source-total classification; do not treat every source number as a fee.
        # Validate values first so an unusable quantity/amount is never summed.
        if not any(issue.severity == "error" for issue in v):
            _, body = _split_headers(text)
            live = "\n".join(line for line in body.split("\n")
                             if not line.lstrip().startswith(">"))
            doc_match = _DOC_CCY_RE.search(live)
            doc_ccy = doc_match.group(1).upper() if doc_match else None
            _, _, stated_totals, source_issues = RulesExtractor()._line_items(live, doc_ccy)
            v.extend(_stated_total_issues(ex, stated_totals))
            if self.ground_prices:
                # A model cannot remove the source's human adjustment decision.
                v.extend(issue for issue in source_issues if issue.severity == "error"
                         and issue.message.startswith((
                             "Discount or credit requires review:",
                             "Tax percentage requires review:",
                         )))
            if self.check_missing_work:
                v.extend(_missing_model_work(ex, live, doc_ccy))
        if self.ground_prices:
            v.extend(_ground_model_prices(ex, text))
        if any(i.field == "client_email" and "hallucination" in i.message for i in v):
            ex.client_email = None
        ex.issues += v
        ex.confidence = min(round(model_conf, 2), compute_confidence(ex.issues))
        return ex


class HybridExtractor:
    """Rules first (free, deterministic); LLM only when rules confidence < threshold."""

    name = "hybrid"

    def __init__(self, primary: Extractor, fallback: Optional[Extractor] = None, threshold: float = 0.75):
        self.primary, self.fallback, self.threshold = primary, fallback, threshold

    @staticmethod
    def human_decision_required(first: Extraction) -> bool:
        # Explicit AI requests also cannot grant consent or choose a recipient.
        if any(issue.severity == "error" and (issue.field == "note" or issue.message in {
            _WORK_QUALIFICATION_MESSAGE, _RECIPIENT_CONFLICT_MESSAGE, _BILLING_REVIEW_MESSAGE,
        }) for issue in first.issues):
            return True
        # Complete priced work with only local optional/payment decisions does
        # not need model reconstruction. Preserve every original error and its
        # source quote for the ordinary explicit review; do not approve it here.
        errors = [issue for issue in first.issues if issue.severity == "error"]
        if (first.source != "rules" or not first.line_items or not errors
                or any(issue.severity == "error" for issue in validate(first))):
            return False
        prefix = "A source amount was not included in the invoice; review it before drafting: "
        for issue in errors:
            if issue.field != "line_items" or not issue.message.startswith(prefix):
                return False
            try:
                quote = ast.literal_eval(issue.message[len(prefix):])
            except (ValueError, SyntaxError):
                return False
            if not isinstance(quote, str) or not RulesExtractor._explicit_review_amount(quote):
                return False
        return True

    def primary_ready(self, first: Extraction) -> bool:
        return (first.confidence >= self.threshold or self.fallback is None
                or self.human_decision_required(first))

    def extract(self, text: str) -> Extraction:
        first = self.primary.extract(text)
        if self.primary_ready(first):
            return first
        second = self.fallback.extract(text)
        best, other = (second, first) if second.confidence > first.confidence else (first, second)
        best.issues.append(Issue("*", "info",
                                 f"Hybrid: chose {best.source} ({best.confidence}) over {other.source} ({other.confidence})."))
        if first.client_email and second.client_email and first.client_email != second.client_email:
            best.issues.append(Issue("client_email", "warning",
                                     f"Extractors disagree on email: {first.client_email} vs {second.client_email}."))
            best.confidence = compute_confidence(best.issues) if best.source == "rules" else min(best.confidence, compute_confidence(best.issues))
        return best


def split_by_currency(ex: Extraction) -> list[Extraction]:
    """One Extraction per currency (PayPal invoices carry a single currency_code)."""
    ccys = ex.currencies()
    if len(ccys) <= 1:
        return [ex]
    parts = []
    for c in ccys:
        parts.append(Extraction(
            ex.client_name, ex.client_email, c,
            [li for li in ex.line_items if li.currency == c], ex.due_days,
            Decimal(0), [i for i in ex.issues if i.field != "currency"], ex.confidence, ex.source, ex.due_date, ex.note,
        ))
    return parts
