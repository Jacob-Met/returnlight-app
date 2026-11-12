"""Explicit, source-bound PDF passages. Extraction and full pages stay local."""
from __future__ import annotations

import copy
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .returnlight_photos import MAX_PHOTO_BYTES, photo_contexts, validate_photo

MAX_PAGE_TEXT = 40000
MAX_PASSAGE_TEXT = 4000
MAX_PASSAGES = 8
EXTRACTOR = "pypdf-7.0.1-layout-v1"
_ID = re.compile(r"[0-9a-f]{32}")
_FIELDS = {"id", "file", "page", "page_text", "start", "end", "text", "selected_at", "extractor"}
_EXTRACTIONS = threading.BoundedSemaphore(2)


def _error(message):
    from .returnlight import CaseError
    return CaseError(message)



def validated_document_sources(case):
    sources = case.get("document_sources", [])
    if type(sources) is not list or len(sources) > MAX_PASSAGES:
        raise _error("Invalid saved source documents.")
    seen = set()
    for reference in sources:
        try:
            validate_photo(reference)
        except (ValueError, TypeError) as exc:
            raise _error("Invalid source document file.") from exc
        if reference["mime"] != "application/pdf" or reference["id"] in seen:
            raise _error("Invalid saved source document.")
        seen.add(reference["id"])
    return copy.deepcopy(sources)

def validated_document_passages(case):
    """Validate historical selections without claiming the original is still available."""
    validated_document_sources(case)
    passages = case.get("document_passages", [])
    if type(passages) is not list or len(passages) > MAX_PASSAGES:
        raise _error("Invalid saved document passages.")
    ids = set()
    for item in passages:
        if type(item) is not dict or set(item) != _FIELDS:
            raise _error("Invalid saved document passage.")
        try:
            validate_photo(item["file"])
        except (ValueError, TypeError) as exc:
            raise _error("Invalid document passage file.") from exc
        if (item["file"]["mime"] != "application/pdf"
                or type(item["id"]) is not str or not _ID.fullmatch(item["id"])
                or item["id"] in ids
                or type(item["page"]) is not int or not 1 <= item["page"] <= 10000
                or type(item["page_text"]) is not str or not 1 <= len(item["page_text"]) <= MAX_PAGE_TEXT
                or type(item["start"]) is not int or type(item["end"]) is not int
                or not 0 <= item["start"] < item["end"] <= len(item["page_text"])
                or type(item["text"]) is not str or not item["text"].strip()
                or len(item["text"]) > MAX_PASSAGE_TEXT
                or item["text"] != item["page_text"][item["start"]:item["end"]]
                or item["extractor"] != EXTRACTOR):
            raise _error("Invalid saved document quotation.")
        try:
            if type(item["selected_at"]) is not str or len(item["selected_at"]) > 40:
                raise ValueError()
            date = datetime.fromisoformat(item["selected_at"].replace("Z", "+00:00"))
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError()
            item["page_text"].encode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise _error("Invalid document passage metadata.") from exc
        ids.add(item["id"])
    return copy.deepcopy(passages)


def document_files(case, draft=None):
    """Original PDFs already attached, recorded or explicitly saved in this case."""
    references = [photo for context in photo_contexts(case) if context
                  for photo in context.get("photos", [])]
    references += [document["file"] for reply in case.get("merchant_replies", [])
                   for document in reply.get("documents", []) if document.get("kind") == "original"]
    references += validated_document_sources(case)
    references += [item["file"] for item in validated_document_passages(case)]
    if draft and draft.get("reply_document"):
        references.append(draft["reply_document"])
    found = {}
    for reference in references:
        validate_photo(reference)
        if reference["mime"] == "application/pdf":
            found[(reference["id"], reference["sha256"])] = dict(reference)
    return list(found.values())


def extract_pdf_page(data, page):
    """Bound untrusted PDF parsing in a separate process, without network or OCR."""
    if type(page) is not int or not 1 <= page <= 10000:
        raise _error("Choose a PDF page between 1 and 10000.")
    if type(data) is not bytes or not 24 <= len(data) <= MAX_PHOTO_BYTES:
        raise _error("Choose an original PDF of at most 8 MiB.")
    if not _EXTRACTIONS.acquire(blocking=False):
        raise _error("Another document is being read. Try this page again shortly.")
    try:
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).with_name("returnlight_document_text_worker.py")), str(page)],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _error("This PDF page could not be read within the local limit. Open the original document.") from exc
        if result.returncode or len(result.stdout) > 500000:
            raise _error("This PDF page could not be read within the local limit. Open the original document.")
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise _error("This PDF page could not be read. Open the original document.") from exc
        if value.get("error"):
            raise _error(value["error"])
        if (set(value) != {"text", "page_count"} or type(value["text"]) is not str
                or not value["text"].strip() or len(value["text"]) > MAX_PAGE_TEXT
                or type(value["page_count"]) is not int or not page <= value["page_count"] <= 10000):
            raise _error("This PDF page has no usable text. Open the original document.")
        return value
    finally:
        _EXTRACTIONS.release()


class DocumentPassageStore:
    """Short-lived server-owned previews; clients submit offsets, never source text."""

    def __init__(self):
        self._previews = {}
        self._lock = threading.Lock()


    def save_source(self, case_path, payload, photos):
        """Keep a reading original without changing any outgoing attachment."""
        import base64
        import binascii
        import hashlib
        from .returnlight import _locked, _load, _require_case_revision, _event, _save
        from .returnlight_photos import photo_format
        upload = set(payload) == {"case_revision", "filename", "data_base64"}
        removal = set(payload) == {"case_revision", "remove"}
        if not upload and not removal:
            raise _error("Choose an original PDF for support reading.")
        if upload:
            filename, encoded = payload["filename"], payload["data_base64"]
            if type(filename) is not str or not filename.strip() or len(filename) > 200:
                raise _error("The original document needs a filename of at most 200 characters.")
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename):
                raise _error("The original document filename is invalid.")
            if type(encoded) is not str or len(encoded) > 4 * ((MAX_PHOTO_BYTES + 2) // 3):
                raise _error("Choose an original PDF of at most 8 MiB.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise _error("The original document could not be read.") from exc
            if not data.startswith(b"%PDF-") or photo_format(data) is None:
                raise _error("Choose an original PDF of at most 8 MiB.")
        elif type(payload["remove"]) is not str or not _ID.fullmatch(payload["remove"]):
            raise _error("Choose a source document to remove.")
        with _locked(case_path):
            case = _load(case_path)
            _require_case_revision(case, payload["case_revision"])
            sources = validated_document_sources(case)
            if upload:
                digest = hashlib.sha256(data).hexdigest()
                reference = next((ref for ref in sources if ref["name"] == filename and ref["sha256"] == digest), None)
                if reference is not None:
                    try:
                        photos.read(reference)
                    except (OSError, ValueError):
                        sources.remove(reference)
                    else:
                        return case, reference
                if len(sources) >= MAX_PASSAGES:
                    raise _error("Keep up to eight source PDFs. Remove an unused source file before adding another.")
                reference = photos.put(data, filename)
                sources.append(reference)
                detail = "Merchant kept an original PDF for support reading."
            else:
                token = payload["remove"]
                if any(item["file"]["id"] == token for item in validated_document_passages(case)):
                    raise _error("Remove this document's selected words before removing its source file.")
                remaining = [ref for ref in sources if ref["id"] != token]
                if len(remaining) == len(sources):
                    raise _error("That source document is no longer kept here.")
                sources = remaining
                reference = None
                detail = "Merchant removed an unused source PDF from support reading."
            case["document_sources"] = sources
            _event(case, "document.source_changed", detail)
            _save(case_path, case)
            return case, reference

    def preview(self, case_path, file_id, page, photos, working_drafts):
        case, work = working_drafts.detail(case_path)
        reference = next((item for item in document_files(case, work["draft"]) if item["id"] == file_id), None)
        if reference is None:
            raise _error("Choose an original PDF already kept in this case.")
        try:
            data = photos.read(reference)
        except (OSError, ValueError) as exc:
            raise _error("The original document is unavailable or changed. Keep the original file again.") from exc
        value = extract_pdf_page(data, page)
        preview = {"preview_id": secrets.token_hex(16), "file": reference, "page": page,
                   "page_count": value["page_count"], "text": value["text"], "extractor": EXTRACTOR,
                   "case_revision": len(case["history"])}
        with self._lock:
            now = time.monotonic()
            self._previews = {key: item for key, item in self._previews.items() if now - item["stored_at"] <= 600}
            if len(self._previews) >= 64:
                self._previews.pop(next(iter(self._previews)))
            self._previews[preview["preview_id"]] = {
                "stored_at": now, "case_path": str(Path(case_path).resolve()), "preview": copy.deepcopy(preview)}
        return preview

    def save(self, case_path, payload, photos, working_drafts):
        from .returnlight import _locked, _load, _require_case_revision, _event, _save
        removing = set(payload) == {"remove", "case_revision"}
        if not removing and set(payload) != {"preview_id", "start", "end", "case_revision"}:
            raise _error("Select exact words from the checked document page.")
        reference = None
        if not removing:
            token = payload["preview_id"]
            with self._lock:
                cached = copy.deepcopy(self._previews.get(token)) if type(token) is str else None
            if (not cached or time.monotonic() - cached["stored_at"] > 600
                    or cached["case_path"] != str(Path(case_path).resolve())):
                raise _error("Read the original document page again before keeping these words.")
            preview = cached["preview"]
            if preview["case_revision"] != payload["case_revision"]:
                raise _error("The case changed. Read the document page again.")
            start, end = payload["start"], payload["end"]
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(preview["text"]) or end - start > MAX_PASSAGE_TEXT
                    or not preview["text"][start:end].strip()):
                raise _error("Select between 1 and 4000 characters from this page.")
            reference = preview["file"]
            # The working copy has its own version; validate its current association too.
            current, work = working_drafts.detail(case_path)
            if reference not in document_files(current, work["draft"]):
                raise _error("The saved original changed. Read its current document again.")
            try:
                photos.read(reference)
            except (OSError, ValueError) as exc:
                raise _error("The original document is unavailable or changed. Keep the original file again.") from exc
        target = Path(case_path)
        with _locked(target):
            case = _load(target)
            _require_case_revision(case, payload["case_revision"])
            if reference is not None:
                current_work = working_drafts._read(target, case)
                if reference not in document_files(case, current_work["draft"]):
                    raise _error("The saved original changed. Read its current document again.")
            passages = validated_document_passages(case)
            if removing:
                token = payload["remove"]
                if type(token) is not str or not _ID.fullmatch(token):
                    raise _error("Choose a saved document passage to remove.")
                remaining = [item for item in passages if item["id"] != token]
                if len(remaining) == len(passages):
                    raise _error("That document passage is no longer selected.")
                case["document_passages"] = remaining
                detail = "Merchant removed a selected original-document passage from support reading."
            else:
                if any(item["file"] == reference and item["page"] == preview["page"]
                       and item["start"] == start and item["end"] == end
                       and item["page_text"] == preview["text"] for item in passages):
                    return case
                if len(passages) >= MAX_PASSAGES:
                    raise _error("Keep up to eight document passages. Remove one before adding another.")
                item = {"id": secrets.token_hex(16), "file": copy.deepcopy(reference), "page": preview["page"],
                        "page_text": preview["text"], "start": start, "end": end,
                        "text": preview["text"][start:end], "selected_at": datetime.now(timezone.utc).isoformat(),
                        "extractor": EXTRACTOR}
                case["document_passages"] = passages + [item]
                validated_document_passages(case)
                detail = "Merchant kept selected words from an original document for support reading."
            _event(case, "document.passage_changed", detail)
            _save(target, case)
            return case
