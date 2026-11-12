"""Explicitly saved unfinished work, separate from case evidence and decisions."""
from __future__ import annotations
import json
import os
import re
import secrets
from pathlib import Path
from .returnlight import CaseError, CaseChanged, _load, _locked, _save, checked_source_conditions

_VERSION = re.compile(r"^[a-f0-9]{32}$")
_MAX_BYTES = 40 * 1024

def _draft(value):
    if type(value) is not dict or not {"decision", "reply"} <= set(value) or set(value) - {"decision", "reply", "followup", "conditions", "reply_document"}:
        raise CaseError("Invalid unfinished work.")
    if value.get("reply_document") is not None:
        from .returnlight_photos import validate_photo
        try:
            validate_photo(value["reply_document"])
        except (ValueError, TypeError) as exc:
            raise CaseError("Invalid original document reference.") from exc
    decision, reply = value["decision"], value["reply"]
    if decision is not None:
        if (type(decision) is not dict or not {"capture", "reason", "amount", "revision", "active"} <= set(decision)
                or set(decision) - {"capture", "reason", "amount", "revision", "active", "followup_needed"}
                or ("followup_needed" in decision and type(decision["followup_needed"]) is not bool)
                or (decision["capture"] is not None and (type(decision["capture"]) is not str or len(decision["capture"]) > 40))
                or type(decision["reason"]) is not str or len(decision["reason"]) > 1000
                or type(decision["amount"]) is not str or len(decision["amount"]) > 256
                or type(decision["revision"]) is not int or decision["revision"] < 1
                or decision["active"] not in {"reason", "amount"}):
            raise CaseError("Invalid unfinished decision.")
    if reply is not None:
        if (type(reply) is not dict or not {"capture", "version", "text"} <= set(reply) or set(reply) - {"capture", "version", "text", "record_id", "review_required", "recorded_purchases"}
                or (reply["capture"] is not None and (type(reply["capture"]) is not str or len(reply["capture"]) > 40))
                or type(reply["version"]) is not str or len(reply["version"]) > 512
                or type(reply["text"]) is not str or len(reply["text"]) > 3000
                or ("review_required" in reply and type(reply["review_required"]) is not bool)
                or ("record_id" in reply and (type(reply["record_id"]) is not str or not _VERSION.fullmatch(reply["record_id"])))):
            raise CaseError("Invalid unfinished reply.")
        references = reply.get("recorded_purchases", [])
        if type(references) is not list or len(references) > 8:
            raise CaseError("Invalid earlier-summary references.")
        seen = set()
        for ref in references:
            if (type(ref) is not dict or set(ref) != {"reply_id", "document_index", "document_sha256"}
                    or type(ref["reply_id"]) is not str or not _VERSION.fullmatch(ref["reply_id"])
                    or type(ref["document_index"]) is not int or ref["document_index"] not in (0, 1)
                    or type(ref["document_sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", ref["document_sha256"])):
                raise CaseError("Invalid earlier-summary reference.")
            identity = (ref["reply_id"], ref["document_index"], ref["document_sha256"])
            if identity in seen:
                raise CaseError("Duplicate earlier-summary reference.")
            seen.add(identity)

    followup = value.get("followup")
    if followup is not None:
        if (type(followup) is not dict or set(followup) != {"capture", "text", "revision", "completed"}
                or (followup["capture"] is not None and (type(followup["capture"]) is not str or len(followup["capture"]) > 40))
                or type(followup["text"]) is not str or len(followup["text"]) > 1000
                or type(followup["revision"]) is not int or followup["revision"] < 1
                or type(followup["completed"]) is not bool):
            raise CaseError("Invalid unfinished follow-up note.")
    conditions = value.get("conditions")
    if conditions is not None:
        if (type(conditions) is not dict or set(conditions) != {"capture", "revision", "items"}
                or (conditions["capture"] is not None and (type(conditions["capture"]) is not str or len(conditions["capture"]) > 40))
                or type(conditions["revision"]) is not int or conditions["revision"] < 1
                or type(conditions["items"]) is not list or not 1 <= len(conditions["items"]) <= 8):
            raise CaseError("Invalid unfinished source conditions.")
        for item in conditions["items"]:
            message_identity = (type(item) is dict and set(item) == {"message_index", "source_start", "quote"}
                                and type(item["message_index"]) is int and item["message_index"] >= 0)
            reply_identity = (type(item) is dict and set(item) == {"reply_id", "source_start", "quote"}
                              and type(item["reply_id"]) is str and _VERSION.fullmatch(item["reply_id"]))
            work_identity = (type(item) is dict and set(item) == {"work_seq", "source_start", "quote"}
                             and type(item["work_seq"]) is int and item["work_seq"] > 0)
            if (not (message_identity or reply_identity or work_identity)
                    or type(item["source_start"]) is not int or item["source_start"] < 0
                    or type(item["quote"]) is not str or not item["quote"].strip() or len(item["quote"]) > 1000):
                raise CaseError("Invalid unfinished source condition.")
    return value

def _already_recorded(reply, case):
    if not reply:
        return False
    try:
        basis = json.loads(reply["version"])
    except (ValueError, TypeError):
        return False
    return any(entry["text"] == reply["text"] and entry["capture_id"] == reply["capture"]
               and (reply.get("record_id") == entry["id"] or basis == [entry["capture_id"], entry["payment_state"],
                    entry["approved_cents"], entry.get("refund_id"), entry["case_revision"]])
               for entry in case.get("merchant_replies", []))

def _reply_after_initial_payment_link(reply, case):
    """Keep this case's saved words when its first payment is linked, unchecked."""
    if not reply or reply["capture"] == case["capture_id"]:
        return reply
    try:
        basis = json.loads(reply["version"])
    except (ValueError, TypeError):
        return reply
    if type(basis) is not list or len(basis) != 5 or type(basis[4]) is not int:
        return reply
    later = case["history"][basis[4]:]
    links = [event for event in later if event.get("event") == "merchant.payment_linked"
             and event.get("previous_capture_id") == reply["capture"]
             and event.get("capture_id") == case["capture_id"]]
    if len(links) != 1 or any(event.get("event") == "merchant.payment_corrected" for event in later):
        return reply
    return {**reply, "capture": case["capture_id"], "review_required": True}


def _remaining_decision(decision, case):
    if not decision:
        return None
    later = [(index + 1, event.get("event")) for index, event in enumerate(case["history"])
             if index + 1 > decision["revision"]]
    if (decision["capture"] != case["capture_id"] or case["state"] not in {"OPEN", "REVIEWED", "APPROVED"}
            or case.get("request_id") or case.get("refund_id")
            or any(event == "merchant.closed_without_refund" for _, event in later)):
        return None
    approvals = [revision for revision, event in later if event == "merchant.approved"]
    if approvals or case["state"] == "APPROVED":
        # Approval consumes the amount, not an alternative explanation the
        # merchant has not submitted. Withdrawal cannot revive the old amount.
        if not decision["reason"].strip():
            return None
        return {**decision, "amount": "", "active": "reason",
                "revision": max([decision["revision"], *approvals])}
    return decision

def _recorded_followup(draft, case):
    if not draft:
        return False
    # A note is consumed by either explicit action: save the next step or complete it.
    return any(item.get("event") in {"merchant.followup_completed", "merchant.followup_opened"}
               and item.get("detail") == draft["text"].strip()
               for item in case["history"][draft["revision"]:])

def _remaining_conditions(draft, case):
    if not draft:
        return None
    if (draft["capture"] != case["capture_id"] or draft["revision"] > len(case["history"])
            or case["state"] not in {"OPEN", "REVIEWED"} or case.get("request_id") or case.get("refund_id")
            or any(event.get("event") in {"buyer.followup_recorded", "merchant.reviewed",
                                        "merchant.approved", "merchant.closed_without_refund"}
                   for event in case["history"][draft["revision"]:])):
        return None
    try:
        checked_source_conditions(case, draft["items"])
    except CaseError:
        return None
    return draft


def _check_recorded_purchases(reply, case):
    from .returnlight_record import recorded_purchase_details_reply
    for ref in (reply or {}).get("recorded_purchases", []):
        recorded_purchase_details_reply(case, ref["reply_id"], str(ref["document_index"]), ref["document_sha256"])


def _visible_work(draft, case):
    visible = dict(draft)
    if _already_recorded(visible["reply"], case):
        visible["reply"] = None
    visible["reply"] = _reply_after_initial_payment_link(visible["reply"], case)
    try:
        _check_recorded_purchases(visible["reply"], case)
    except CaseError:
        # Keep the merchant's words even when a referenced source is no longer usable.
        visible["reply"] = {key: value for key, value in visible["reply"].items() if key != "recorded_purchases"}
        visible["reply"]["review_required"] = True

    visible["decision"] = _remaining_decision(visible["decision"], case)
    if _recorded_followup(visible.get("followup"), case):
        visible["followup"] = None
    if "conditions" in visible:
        visible["conditions"] = _remaining_conditions(visible["conditions"], case)
    return visible

class WorkingDraftStore:
    def __init__(self, directory):
        self.directory = Path(directory) / "working-drafts"

    def _path(self, case_path):
        if self.directory.is_symlink() or (self.directory.exists() and not self.directory.is_dir()):
            raise CaseError("Saved-work directory is unavailable.")
        path = self.directory / case_path.name
        if path.is_symlink():
            raise CaseError("Saved work cannot be a symbolic link.")
        return path

    def _read(self, case_path, case):
        path = self._path(case_path)
        if not path.exists():
            return {"version": None, "draft": {"decision": None, "reply": None}}
        try:
            if path.stat().st_size > _MAX_BYTES:
                raise ValueError("size")
            value = json.loads(path.read_text(encoding="utf-8"))
            if (type(value) is not dict or set(value) != {"schema", "case_id", "version", "draft"}
                    or value["schema"] != "returnlight.working-draft.v1" or value["case_id"] != case["id"]
                    or type(value["version"]) is not str or not _VERSION.fullmatch(value["version"])):
                raise ValueError("format")
            _draft(value["draft"])
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise CaseError("Saved unfinished work could not be read. The case record is unchanged.") from exc
        return {"version": value["version"], "draft": value["draft"]}

    def detail(self, case_path):
        with _locked(case_path):
            case = _load(case_path)
            # A damaged working copy must not prevent reading the actual case.
            try:
                draft = self._read(case_path, case)
                visible = _visible_work(draft["draft"], case)
                draft = {**draft, "draft": visible}
            except CaseError as exc:
                draft = {"version": None, "draft": {"decision": None, "reply": None}, "error": str(exc)}
            return case, draft

    def retire_completed(self, case_path):
        with _locked(case_path):
            case = _load(case_path)
            current = self._read(case_path, case)
            remaining = _visible_work(current["draft"], case)
            if remaining == current["draft"]:
                return
            saved = {"schema": "returnlight.working-draft.v1", "case_id": case["id"],
                     "version": secrets.token_hex(16), "draft": remaining}
            _save(self._path(case_path), saved)

    def require_reply_review(self, case_path, identity):
        """Invalidate only an existing saved reply; retain no provider facts."""
        from .returnlight import _refund_observation_identity
        with _locked(case_path):
            case = _load(case_path)
            if _refund_observation_identity(case) != identity:
                raise CaseChanged("The original refund changed. Check it again before reviewing the saved reply.")
            current = self._read(case_path, case)
            remaining = _visible_work(current["draft"], case)
            reply = remaining.get("reply")
            if not reply or reply["capture"] != case["capture_id"] or not reply["text"].strip() or reply.get("review_required"):
                return {**current, "draft": remaining}
            remaining["reply"] = {**reply, "review_required": True}
            saved = {"schema": "returnlight.working-draft.v1", "case_id": case["id"],
                     "version": secrets.token_hex(16), "draft": remaining}
            _save(self._path(case_path), saved)
            return {"version": saved["version"], "draft": remaining}

    def save(self, case_path, payload):
        if set(payload) != {"case_revision", "draft_version", "draft"}:
            raise CaseError("Invalid save-work request.")
        draft = _draft(payload["draft"])
        version = payload["draft_version"]
        if version is not None and (type(version) is not str or not _VERSION.fullmatch(version)):
            raise CaseError("Reload the saved work before changing it.")
        with _locked(case_path):
            case = _load(case_path)
            if type(payload["case_revision"]) is not int or payload["case_revision"] != len(case["history"]):
                raise CaseChanged("The case changed. Refresh it before saving unfinished work; your words remain here.")
            _check_recorded_purchases(draft["reply"], case)
            if _already_recorded(draft["reply"], case):
                raise CaseChanged("This exact reply is already recorded. Review the current case before saving new unfinished words.")
            for part in (draft["decision"], draft["reply"], draft.get("followup"), draft.get("conditions")):
                if part is not None and part["capture"] != case["capture_id"]:
                    raise CaseChanged("This unfinished work belongs to a different payment.")
            if draft["decision"] is not None:
                if (_remaining_decision(draft["decision"], case) != draft["decision"] or case.get("request_id") or case.get("refund_id")
                        or draft["decision"]["revision"] > len(case["history"])):
                    raise CaseChanged("The case no longer accepts an unfinished decision.")
            followup = draft.get("followup")
            if followup is not None and (followup["revision"] > len(case["history"]) or _recorded_followup(followup, case)):
                raise CaseChanged("This follow-up note is already recorded or belongs to a newer case.")
            conditions = draft.get("conditions")
            if conditions is not None and _remaining_conditions(conditions, case) != conditions:
                raise CaseChanged("The source conditions changed or were already reviewed. Select current source words again.")
            current = self._read(case_path, case)
            document = current["draft"].get("reply_document")
            if "reply_document" in draft and draft["reply_document"] != document:
                raise CaseError("Use the original-document action to change the saved file.")
            if "reply_document" in current["draft"]:
                draft = {**draft, "reply_document": document}
            if "conditions" not in draft and current["draft"].get("conditions") is not None:
                draft = {**draft, "conditions": _remaining_conditions(current["draft"]["conditions"], case)}
            # Older clients know only reply/decision. Preserve the additional
            # note unless a client explicitly supplies its replacement/discard.
            if "followup" not in draft and current["draft"].get("followup") is not None:
                draft = {**draft, "followup": current["draft"]["followup"]}
            # Exact retries are harmless, including a lost successful response.
            if current["draft"] == draft:
                return current
            if current["version"] != version:
                raise CaseChanged("Saved work changed in another window. Your words remain here; reopen the case before saving over it.")
            path = self._path(case_path)
            self.directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.directory, 0o700)
            saved = {"schema": "returnlight.working-draft.v1", "case_id": case["id"],
                     "version": secrets.token_hex(16), "draft": draft}
            _save(path, saved)
            return {"version": saved["version"], "draft": draft}

    def save_reply_document(self, case_path, payload, photos):
        """Retain one explicitly supplied original, separately from case evidence."""
        import base64
        import binascii
        import hashlib
        from .returnlight_photos import MAX_PHOTO_BYTES, photo_format, photo_contexts
        upload = set(payload) == {"case_revision", "draft_version", "filename", "data_base64"}
        removal = set(payload) == {"case_revision", "draft_version", "remove"}
        selection = set(payload) == {"case_revision", "draft_version", "recorded_reply_id", "recorded_document_index"}
        received = set(payload) == {"case_revision", "draft_version", "received_document_id", "received_document_sha256"}
        if not (upload or removal or selection or received):
            raise CaseError("Choose one original PDF or image from this case, or remove the saved document.")
        version = payload["draft_version"]
        if version is not None and (type(version) is not str or not _VERSION.fullmatch(version)):
            raise CaseError("Reload the saved work before changing its document.")
        data = None
        if upload:
            filename, encoded = payload["filename"], payload["data_base64"]
            if type(filename) is not str or not filename.strip() or len(filename) > 200:
                raise CaseError("The original document needs a filename of at most 200 characters.")
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not filename.strip() or any(ord(c) < 32 or ord(c) == 127 for c in filename):
                raise CaseError("The original document filename is invalid.")
            if type(encoded) is not str or len(encoded) > 4 * ((MAX_PHOTO_BYTES + 2) // 3):
                raise CaseError("Choose a PDF, PNG or JPEG of at most 8 MiB.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise CaseError("The original document could not be read.") from None
            if photo_format(data) is None:
                raise CaseError("Choose a supported original PDF, PNG or JPEG of at most 8 MiB.")
        elif removal and (type(payload["remove"]) is not str or not _VERSION.fullmatch(payload["remove"])):
            raise CaseError("Choose the saved original document to remove.")
        with _locked(case_path):
            case = _load(case_path)
            if type(payload["case_revision"]) is not int or payload["case_revision"] != len(case["history"]):
                raise CaseChanged("The case changed. Refresh before changing its original document.")
            recorded_reference = None
            if selection:
                reply = next((entry for entry in case.get("merchant_replies", [])
                              if entry["id"] == payload["recorded_reply_id"]), None)
                index = payload["recorded_document_index"]
                documents = reply.get("documents", []) if reply else []
                if type(index) is not int or index < 0 or index >= len(documents) or documents[index]["kind"] != "original":
                    raise CaseError("Choose an original document recorded in this case.")
                recorded_reference = dict(documents[index]["file"])
            elif received:
                recorded_reference = next((dict(photo) for context in photo_contexts(case) if context
                    for photo in context.get("photos", [])
                    if photo["id"] == payload["received_document_id"]
                    and photo["sha256"] == payload["received_document_sha256"]), None)
                if recorded_reference is None:
                    raise CaseError("Choose a document attached to a saved message in this case.")
            if recorded_reference is not None:
                try:
                    data = photos.read(recorded_reference)
                except (OSError, ValueError) as exc:
                    raise CaseError("The selected original is unavailable or changed. Keep the original file again.") from exc
                filename = recorded_reference["name"]
                upload = True
            current = self._read(case_path, case)
            reference = current["draft"].get("reply_document")
            if upload and reference and reference["name"] == filename and reference["sha256"] == hashlib.sha256(data).hexdigest():
                try:
                    photos.read(reference)
                except (OSError, ValueError):
                    pass  # A current-window upload can replace damaged original bytes.
                else:
                    return {**current, "draft": _visible_work(current["draft"], case)}
            if removal and reference is None:
                return {**current, "draft": _visible_work(current["draft"], case)}
            if current["version"] != version:
                raise CaseChanged("Saved work changed in another window. Reopen it before changing the document.")
            if removal and reference["id"] != payload["remove"]:
                raise CaseChanged("The saved document changed. Check the current original before removing it.")
            replacement = (recorded_reference or photos.put(data, filename)) if upload else None
            draft = {**_visible_work(current["draft"], case), "reply_document": replacement}
            self.directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.directory, 0o700)
            saved = {"schema": "returnlight.working-draft.v1", "case_id": case["id"],
                     "version": secrets.token_hex(16), "draft": draft}
            _save(self._path(case_path), saved)
            return {"version": saved["version"], "draft": draft}

    def read_reply_document(self, case_path, document_id, sha256, photos):
        """Load only this case's currently retained original, with exact bytes."""
        with _locked(case_path):
            case = _load(case_path)
            current = self._read(case_path, case)
            reference = current["draft"].get("reply_document")
            if not reference or reference["id"] != document_id or reference["sha256"] != sha256:
                raise CaseChanged("The original document changed or belongs to another case. Preview it again.")
            try:
                data = photos.read(reference)
            except (OSError, ValueError) as exc:
                raise CaseError("The saved original document is unavailable or changed. Choose the original file again.") from exc
            return dict(reference), data

    def mark_reply_for_review(self, case_path, expected_case_revision):
        """Persist an unchecked marker without saving browser words or provider facts."""
        with _locked(case_path):
            case = _load(case_path)
            if type(expected_case_revision) is not int or expected_case_revision != len(case["history"]):
                raise CaseChanged("The case changed. Refresh before checking its saved reply.")
            current = self._read(case_path, case)
            draft = _visible_work(current["draft"], case)
            reply = draft["reply"]
            if not reply or reply.get("review_required") is True:
                return {**current, "draft": draft}
            draft = {**draft, "reply": {**reply, "review_required": True}}
            self.directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.directory, 0o700)
            saved = {"schema": "returnlight.working-draft.v1", "case_id": case["id"],
                     "version": secrets.token_hex(16), "draft": draft}
            _save(self._path(case_path), saved)
            return {"version": saved["version"], "draft": draft}
