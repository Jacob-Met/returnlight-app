"""Loopback-only merchant case browser for Returnlight's real local state machine.

This is not Returnlight's in-memory invoice demo. Every action reads/writes the
same private case files and calls the same model/PayPal Sandbox adapters as the
CLI. No external JS, authentication service, or production PayPal endpoint.
"""
from __future__ import annotations

import argparse
import base64
import copy
import threading
import time
from datetime import datetime, timezone
import html
import hashlib
import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .paypal import PayPalError
from .returnlight_model_status import model_status
from .returnlight_remote_review import RemoteReviewer
from .returnlight_intake import import_buyer_email, MAX_EMAIL_BYTES
from .returnlight_record import render_case_record, render_purchase_details, reply_email_choices, render_reply_email, checked_reply_documents, recorded_reply_document
from .returnlight_photos import PhotoStore, photo_contexts, MAX_PHOTO_BYTES
from .returnlight_working_drafts import WorkingDraftStore

from .returnlight import (
    _validate_imported_email,
    CaseChanged, CaseError, ReviewInProgress, review_in_progress, OllamaReviewer, OutcomeUnknown, approve_case, capture_preview, inspect_capture, order_capture_choices, case_snapshot,
    add_buyer_followup, add_post_payment_message, resolve_post_payment_support, record_merchant_reply, update_merchant_followup, merchant_followup_needs_review, merchant_work_sources, buyer_evidence, close_case, reopen_case, revise_approval, execute_case, new_case, real_gateway, refresh_case, review_case,
    _refund_observation_identity, retain_refund_observation, review_support_case, dismiss_support_review, set_case_subject, correct_payment_details, retain_purchase, human_review_case, dismiss_review, discover_case_refunds,
)

_CASE_KEY = re.compile(r"^[a-f0-9]{32}$")
_PHOTO_ROUTE = re.compile(r"^/api/photos/([a-f0-9]{32})$")
_ORDER_CAPTURE_ROUTE = re.compile(r"^/api/orders/([A-Za-z0-9]{8,40})/captures$")
_CAPTURE_ROUTE = re.compile(r"^/api/captures/([A-Za-z0-9]{8,40})$")
_CASE_ROUTE = re.compile(r"^/api/cases/([a-f0-9]{32})(?:/(refund-reply-review|refund-observation|support-review|dismiss-support-review|review|dismiss-review|human-review|approve|execute|refresh|close|revise|followup|postpayment|supporthandled|merchant-reply|merchant-followup|reopen|capture|record|subject|purchase|refunds|payment|working-draft|reply-draft|reply-document|purchase-details|purchase-reply|document-text|document-passage|document-source))?$")

# A complete 15,000-character message plus eight 1,000-character retained
# clauses can occupy 276,000 escaped JSON bytes. Keep the envelope bounded.
_MAX_JSON_BYTES = 384 * 1024
_HTML = Path(__file__).with_name("returnlight_web_ui.html")


class LocalCases:
    """Private case storage. The browser never chooses an on-disk path."""
    def __init__(self, directory: Path, model_name: str = "qwen2.5:3b",
                 reviewer_factory=None, gateway_factory=None, remote_reviewer=None):
        self.directory = Path(directory).expanduser()
        if self.directory.is_symlink():
            raise CaseError("Case directory cannot be a symbolic link.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.directory.is_dir():
            raise CaseError("Case path must be a directory.")
        os.chmod(self.directory, 0o700)
        self.photos = PhotoStore(self.directory)
        self.working_drafts = WorkingDraftStore(self.directory)
        from .returnlight_document_sources import DocumentPassageStore
        self.document_passages = DocumentPassageStore()
        self._refund_observations = {}
        self._refund_observation_lock = threading.Lock()
        self._purchase_observations = {}
        self._purchase_observation_lock = threading.Lock()
        self.model_name = model_name
        self.remote_reviewer = remote_reviewer
        self.reviewer_factory = reviewer_factory or ((lambda: remote_reviewer) if remote_reviewer else (lambda: OllamaReviewer(model=model_name)))
        self.gateway_factory = gateway_factory or real_gateway

    def path(self, key: str) -> Path:
        if type(key) is not str or not _CASE_KEY.fullmatch(key):
            raise CaseError("Invalid case reference.")
        path = self.directory / (key + ".json")
        if path.is_symlink():
            raise CaseError("Symbolic-link cases are not supported.")
        return path

    def summary(self, key: str) -> dict:
        case, work = self.working_drafts.detail(self.path(key))
        draft = work["draft"]
        saved_reply = bool(draft["reply"] and draft["reply"]["capture"] == case["capture_id"] and draft["reply"]["text"].strip())
        saved_decision = bool(draft["decision"] and (draft["decision"]["amount"].strip() or draft["decision"]["reason"].strip()))
        latest = case["buyer_followups"][-1]["text"] if case.get("buyer_followups") else case["complaint"]
        review = case.get("review") or {}
        resolution = (case.get("resolution") or {}).get("reason", "")
        purchase = case.get("purchase_context") or {}
        if purchase.get("capture_id") != case["capture_id"]:
            purchase = {}
        order = purchase.get("order") or {}
        saved_work = []
        for kind, label in (("reply", "Saved unsent reply"), ("decision", "Saved decision"),
                            ("followup", "Saved follow-up"), ("conditions", "Unfinished review")):
            part = draft.get(kind)
            if not part or part.get("capture") != case["capture_id"]:
                continue
            text = ("\n\n".join(filter(None, [part.get("reason"), part.get("amount")])) if kind == "decision"
                    else "\n\n".join(item["quote"] for item in part.get("items", [])) if kind == "conditions"
                    else part.get("text", ""))
            if text.strip():
                saved_work.append({"kind": kind, "label": label, "text": text})
        from .returnlight_document_sources import validated_document_passages
        selected_documents = validated_document_passages(case)
        return {
            "key": key, "id": case["id"], "state": case["state"],
            "saved_reply": saved_reply, "saved_decision": saved_decision,
            "saved_work": saved_work,
            "selected_document_passages": [
                {"id": passage["id"], "file_sha256": passage["file"]["sha256"],
                 "file_name": passage["file"]["name"], "page": passage["page"],
                 "text": passage["text"]} for passage in selected_documents],
            "recorded_documents": [
                {"reply_id": entry["id"], "index": index, "kind": document["kind"],
                 "name": document["file"]["name"] if document["kind"] == "original" else "Saved purchase summary"}
                for entry in case.get("merchant_replies", [])
                for index, document in enumerate(entry.get("documents", []))],
            "saved_conditions": bool(draft.get("conditions")),
            "saved_followup": bool(draft.get("followup") and draft["followup"]["text"].strip()),
            "subject": case.get("subject"),
            "resolution_preview": resolution[:240] + ("…" if len(resolution) > 240 else ""),
            "imported_email": case.get("imported_email"),
            "email_contexts": [entry["imported_email"] for entry in
                               case.get("buyer_followups", []) + case.get("post_payment_messages", [])
                               if entry.get("imported_email")],
            "purchase_order_id": purchase.get("order_id") or order.get("id"),
            "purchase_items": [{"name": item["name"], "sku": item.get("sku")}
                               for item in order.get("items", [])],
            "refund_id": case.get("refund_id"),
            "capture_id": case["capture_id"], "ceiling_cents": case["ceiling_cents"],
            "payment_pending": bool(case.get("payment_pending")),
            "approved_cents": case["approved_cents"], "reopened_at": case.get("reopened_at"),
            "has_refund_attempt": bool(case.get("request_id") or case.get("refund_id")),
            "last_recorded_at": case["history"][-1].get("recorded_at") if case["history"] else None,
            "review_quote": review.get("quote") if len(latest) > 900 and review.get("kind") == "source_selection" else None,
            "complaint_preview": case["complaint"][:125],
            "latest_message_preview": (case["post_payment_messages"][-1]["text"] if case.get("post_payment_messages") else case["buyer_followups"][-1]["text"] if case.get("buyer_followups") else case["complaint"])[:125],
            "complaint": "\n\n".join([buyer_evidence(case)] + [entry["text"] for entry in case.get("post_payment_messages", [])] + [entry["text"] for entry in case.get("merchant_replies", [])] + [entry["reason"] for entry in case.get("support_resolutions", [])] + ([resolution] if resolution else []) + [entry["superseded_resolution"]["reason"] for entry in case.get("buyer_followups", []) if (entry.get("superseded_resolution") or {}).get("reason")]),
            "support_followup_open": bool(case.get("support_followup_open")),
            "merchant_followup_open": bool((case.get("merchant_followup") or {}).get("open")),
            "merchant_followup_needs_review": merchant_followup_needs_review(case),
            "merchant_followup_note": (case.get("merchant_followup") or {}).get("note", ""),
            "merchant_work_history": [entry for entry in merchant_work_sources(case)
                                      if entry["text"] != (case.get("merchant_followup") or {}).get("note")],
            # A failed refund is still merchant work, not a resolved buyer case.
            "queue": ("followup" if case.get("support_followup_open") else
                      "decide" if case["state"] in {"OPEN", "REVIEWED"} and merchant_followup_needs_review(case) else
                      "followup" if (case.get("merchant_followup") or {}).get("open") else
                      "decide" if case["state"] in {"OPEN", "REVIEWED"} else
                      "finished" if case["state"] in {"COMPLETED", "CLOSED"} else "followup"),
        }

    def list_cases(self) -> list[dict]:
        entries = []
        for path in sorted(self.directory.glob("*.json"),
                           key=lambda p: p.stat().st_mtime_ns, reverse=True):
            if not _CASE_KEY.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                entries.append(self.summary(path.stem))
            except (CaseError, OSError, KeyError, TypeError):
                entries.append({"key": path.stem, "state": "UNREADABLE", "queue": "followup",
                                "complaint_preview": "Case cannot be read. Inspect the local file."})
        return entries

    def import_email(self, raw: bytes) -> dict:
        created = []
        def retain(data, filename):
            photo = self.photos.put(data, filename)
            if photo:
                created.append(photo)
            return photo
        try:
            return import_buyer_email(raw, store_photo=retain)
        except Exception:
            for photo in created:
                self.photos.path(photo["id"]).unlink(missing_ok=True)
            raise

    def checked_email(self, context):
        if context is not None:
            context = _validate_imported_email(context)
            for photo in context.get("photos", []):
                try:
                    self.photos.read(photo)
                except (OSError, ValueError) as exc:
                    raise CaseError("An imported attachment is unavailable or changed. Import the original email again.") from exc
        return context

    def create(self, payload: dict) -> dict:
        key = secrets.token_hex(16)
        path = self.path(key)
        observation = None
        token = payload.get("purchase_observation_id")
        if type(token) is str:
            with self._purchase_observation_lock:
                cached = self._purchase_observations.get(token)
                if (cached and time.monotonic() - cached["stored_at"] <= 600
                        and cached["capture"]["capture_id"] == payload.get("capture_id")):
                    observation = {"capture": copy.deepcopy(cached["capture"]),
                                   "observed_at": cached["observed_at"]}
        options = {"purchase_observation": observation} if observation else {}
        if "payment_pending" in payload:
            options["payment_pending"] = payload["payment_pending"]
        if "imported_email" in payload:
            options["imported_email"] = self.checked_email(payload["imported_email"])
        new_case(path, payload.get("capture_id"), payload.get("ceiling"),
                 payload.get("complaint"), **options)
        result = {"key": key, "case": case_snapshot(path)}
        result["reply_email_choices"] = reply_email_choices(result["case"])
        if token is not None and observation is None:
            result["purchase_context_notice"] = (
                "Case opened without saved purchase details. Use Check payment to view current payment details.")
        return result

    def detail(self, key: str) -> dict:
        case, working_draft = self.working_drafts.detail(self.path(key))
        from .returnlight_support_review import current_support_review
        if "support_review" in case:
            case["support_review"] = current_support_review(case)
        from .returnlight_support_review import refund_check_source
        return {"key": key, "case": case, "working_draft": working_draft,
                "review_in_progress": review_in_progress(self.path(key)),
                "refund_check_source": refund_check_source(case),
                "reply_review_observation": self.reply_review_observation(key, case, working_draft),
                "reply_email_choices": reply_email_choices(case)}

    def reply_review_observation(self, key, case, work):
        reply = work.get("draft", {}).get("reply")
        if not reply or not reply.get("review_required") or reply.get("capture") != case["capture_id"]:
            return None
        identity = _refund_observation_identity(case)
        now = time.monotonic()
        with self._refund_observation_lock:
            matching = [cached for cached in self._refund_observations.values()
                        if cached["key"] == key and cached["identity"] == identity
                        and cached.get("reply_review_done") and now - cached["stored_at"] <= 600]
            if not matching:
                return None
            return copy.deepcopy(max(matching, key=lambda cached: cached["stored_at"])["observation"])

    def read_reply_document(self, key, document_id, sha256):
        return self.working_drafts.read_reply_document(self.path(key), document_id, sha256, self.photos)

    def read_document_source(self, key, document_id, sha256):
        from .returnlight_document_sources import document_files
        case, work = self.working_drafts.detail(self.path(key))
        reference = next((item for item in document_files(case, work["draft"])
                          if item["id"] == document_id and item["sha256"] == sha256), None)
        if reference is None:
            raise CaseError("Choose an original PDF kept in this case.")
        return reference, self.photos.read(reference)

    def read_recorded_reply_document(self, key, reply_id, index):
        case = case_snapshot(self.path(key))
        reply = next((entry for entry in case.get("merchant_replies", []) if entry["id"] == reply_id), None)
        if type(index) is not str or not re.fullmatch(r"[01]", index) or reply is None:
            raise CaseError("Choose a document from this recorded reply.")
        documents = reply.get("documents", [])
        if int(index) >= len(documents):
            raise CaseError("That recorded reply has no such document.")
        return recorded_reply_document(documents[int(index)], self.photos.read)

    def order_payments(self, order_id: str) -> dict:
        return order_capture_choices(order_id, self.gateway_factory())

    def inspect_payment(self, capture_id: str) -> dict:
        capture = inspect_capture(capture_id, self.gateway_factory())
        return self._cache_purchase(capture)

    def _cache_purchase(self, capture: dict) -> dict:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._purchase_observation_lock:
            self._purchase_observations = {
                key: value for key, value in self._purchase_observations.items()
                if now - value["stored_at"] <= 600}
            while len(self._purchase_observations) >= 128:
                del self._purchase_observations[next(iter(self._purchase_observations))]
            self._purchase_observations[token] = {
                "capture": copy.deepcopy(capture),
                "observed_at": datetime.now(timezone.utc).isoformat(), "stored_at": now}
        return {"capture": capture, "purchase_observation_id": token}

    def inspect_capture(self, key: str) -> dict:
        # Every inspection uses a fresh Sandbox GET and makes no case-file change.
        if not case_snapshot(self.path(key))["capture_id"]:
            raise CaseError("Link a payment reference before checking payment.")
        return {"key": key, **self._cache_purchase(capture_preview(self.path(key), self.gateway_factory()))}


    def inspect_refunds(self, key: str) -> dict:
        before = case_snapshot(self.path(key))
        found = discover_case_refunds(self.path(key), self.gateway_factory())
        after = case_snapshot(self.path(key))
        if _refund_observation_identity(before) != _refund_observation_identity(after):
            raise CaseChanged("The refund changed during this check. Check the current original refund again.")
        result = {"key": key, **found}
        if found.get("scope") != "recorded_refund":
            return result
        refund = found["refunds"][0]
        if (found["capture_id"] != after["capture_id"]
                or refund["id"] != after["refund_id"]
                or refund["amount_cents"] != after["approved_cents"]):
            raise CaseChanged("The observed refund no longer matches this case. Check it again.")
        observation = {k: after.get(k) for k in ("capture_id", "request_id", "refund_id")}
        observation.update(amount_cents=refund["amount_cents"], status=refund["status"],
                           checked_at=found["checked_at"])
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._refund_observation_lock:
            self._refund_observations = {k: v for k, v in self._refund_observations.items()
                                         if now - v["stored_at"] <= 600}
            while len(self._refund_observations) >= 128:
                del self._refund_observations[next(iter(self._refund_observations))]
            self._refund_observations[token] = {
                "key": key, "identity": _refund_observation_identity(after),
                "observation": observation, "stored_at": now}
        result["refund_observation_id"] = token
        return result

    def action(self, key: str, name: str, payload: dict) -> dict:
        path = self.path(key)
        if name in ("reply-draft", "merchant-reply"):
            current, work = self.working_drafts.detail(path)
            if work.get("error"):
                raise CaseError(work["error"])
            reply = work["draft"].get("reply")
            if (reply and reply["capture"] == current["capture_id"] and reply.get("review_required")
                    and payload.get("reviewed_work_version") != work["version"]):
                raise CaseChanged("A newer refund check requires reviewing this reply. Your words have been kept.")
        if name == "reply-draft":
            case = case_snapshot(path)
            original_document = None
            if payload.get("include_reply_document") is True:
                original_document = self.read_reply_document(key, payload.get("reply_document_id"), payload.get("reply_document_sha256"))
            data = render_reply_email(case, payload, original_document=original_document)
            return {"filename": "returnlight-reply-" + case["id"][:8] + ".eml",
                    "email_base64": base64.b64encode(data).decode("ascii")}
        if name == "reply-document":
            return {"key": key, "working_draft": self.working_drafts.save_reply_document(path, payload, self.photos)}
        if name == "document-source":
            _, reference = self.document_passages.save_source(path, payload, self.photos)
            return {**self.detail(key), "document_source": reference}
        if name == "refund-reply-review":
            if set(payload) != {"refund_observation_id"}:
                raise CaseError("Use the current original-refund check to review saved work.")
            token = payload["refund_observation_id"]
            with self._refund_observation_lock:
                cached = self._refund_observations.get(token) if type(token) is str else None
                if (not cached or cached["key"] != key
                        or time.monotonic() - cached["stored_at"] > 600
                        or cached["identity"] != _refund_observation_identity(case_snapshot(path))):
                    raise CaseError("Check this original refund again before reviewing the saved reply.")
                if cached.get("reply_review_done"):
                    work = self.working_drafts.detail(path)[1]
                else:
                    work = self.working_drafts.require_reply_review(path, copy.deepcopy(cached["identity"]))
                    cached["reply_review_done"] = True
            return {"key": key, "working_draft": work}
        if name == "working-draft":
            return {"key": key, "working_draft": self.working_drafts.save(path, payload)}
        revision = payload.get("case_revision")
        if name in ("approve", "close", "human-review") and (type(revision) is not int or revision <= 0):
            raise CaseError("Reload this page and review the case before saving a decision.")
        if name == "support-review":
            from .returnlight_purchase_sources import purchase_sources
            from .returnlight_support_review import recorded_purchase_sources, support_work_sources, document_passage_sources
            support_case = case_snapshot(path)
            has_purchase = bool(purchase_sources(support_case))
            has_refund_check = bool(support_case.get("refund_observation"))
            has_recorded_purchase = bool(recorded_purchase_sources(support_case))
            has_work = bool(support_work_sources(support_case))
            has_documents = bool(document_passage_sources(support_case))
            required_scope = ("current-support-document-passages-v6" if has_documents else
                              "current-support-work-v5" if has_work else
                              "current-support-recorded-purchase-v4" if has_recorded_purchase else
                              "current-support-refund-check-v3" if has_refund_check else
                              "current-support-purchase-v2" if has_purchase else "current-support-v1")
            if (not self.remote_reviewer or payload.get("remote_confirmed") is not True
                    or payload.get("destination_id") != self.remote_reviewer.destination_id
                    or payload.get("review_scope") != required_scope):
                extra = (" and the dated original refund check" if has_refund_check else "")
                extra += " and saved purchase details" if has_purchase else ""
                extra += " and earlier recorded purchase summaries" if has_recorded_purchase else ""
                extra += " and internal merchant work notes" if has_work else ""
                extra += " and selected document passages" if has_documents else ""
                raise CaseError("Confirm the selected destination and complete support history" + extra + " before sending.")
            case = review_support_case(path, self.reviewer_factory(), revision)
        elif name == "dismiss-support-review":
            case = dismiss_support_review(path, revision)
        elif name == "review":
            if self.remote_reviewer:
                if (case_snapshot(path).get("purchase_context")
                        and payload.get("review_scope") != "conversation-purchase-v2"):
                    raise CaseError("Confirm the saved purchase details and selected destination before sending.")
                if (payload.get("remote_confirmed") is not True
                        or payload.get("destination_id") != self.remote_reviewer.destination_id
                        or type(revision) is not int or revision <= 0):
                    raise CaseError("Confirm the selected destination and buyer conversation before sending it for review.")
                selected_conditions = payload.get("selected_conditions")
                has_work_notes = type(selected_conditions) is list and any(
                    type(item) is dict and "work_seq" in item for item in selected_conditions)
                condition_scope = "selected-source-conditions-v2-work" if has_work_notes else "selected-source-conditions-v1"
                if selected_conditions and payload.get("condition_scope") != condition_scope:
                    raise CaseError("Confirm sending the source conditions you selected for this reading.")
                case = review_case(path, self.reviewer_factory(), expected_case_revision=revision,
                                   selected_conditions=selected_conditions)
            else:
                case = review_case(path, self.reviewer_factory())
        elif name == "dismiss-review":
            case = dismiss_review(path, revision)
        elif name == "human-review":
            case = human_review_case(path, payload.get("quote"), payload.get("confirmed"),
                                     expected_case_revision=revision,
                                     selected_conditions=payload.get("selected_conditions"))
        elif name == "refund-observation":
            token = payload.get("refund_observation_id")
            with self._refund_observation_lock:
                cached = self._refund_observations.get(token) if type(token) is str else None
                if (not cached or cached["key"] != key
                        or time.monotonic() - cached["stored_at"] > 600
                        or cached["identity"] != _refund_observation_identity(case_snapshot(path))):
                    raise CaseError("Check this original refund again before keeping its dated result.")
                observation = copy.deepcopy(cached["observation"])
            case = retain_refund_observation(path, observation, revision)
        elif name == "document-passage":
            case = self.document_passages.save(path, payload, self.photos, self.working_drafts)
        elif name == "purchase":
            token = payload.get("purchase_observation_id")
            with self._purchase_observation_lock:
                cached = self._purchase_observations.get(token) if type(token) is str else None
                if not cached or time.monotonic() - cached["stored_at"] > 600:
                    raise CaseError("Check payment again before keeping purchase details.")
                observation = {"capture": copy.deepcopy(cached["capture"]),
                               "observed_at": cached["observed_at"]}
            case = retain_purchase(path, observation, revision)
        elif name == "subject":
            case = set_case_subject(path, payload.get("subject"), revision)
        elif name == "followup":
            case = add_buyer_followup(path, payload.get("text"), payload.get("case_revision"), imported_email=self.checked_email(payload.get("imported_email")), record_id=payload.get("record_id"))
        elif name == "postpayment":
            case = add_post_payment_message(path, payload.get("text"), revision, imported_email=self.checked_email(payload.get("imported_email")), record_id=payload.get("record_id"))
        elif name == "merchant-reply":
            basis = case_snapshot(path)
            previous = next((entry for entry in basis.get("merchant_replies", []) if entry["id"] == payload.get("record_id")), None)
            if previous:
                documents = previous.get("documents", [])
                purchase = next((item for item in documents if item["kind"] == "purchase_summary"), None)
                original = next((item["file"] for item in documents if item["kind"] == "original"), None)
                if (payload.get("include_purchase_details", False) is not bool(purchase)
                        or payload.get("include_reply_document", False) is not bool(original)
                        or (purchase and payload.get("purchase_details_sha256") != purchase["snapshot"]["sha256"])
                        or (original and (payload.get("reply_document_id") != original["id"]
                                          or payload.get("reply_document_sha256") != original["sha256"]))):
                    raise CaseError("This reply recording identity already contains different documents.")
            else:
                original_document = None
                if payload.get("include_reply_document") is True:
                    original_document = self.read_reply_document(key, payload.get("reply_document_id"), payload.get("reply_document_sha256"))
                documents = [document for document, _ in checked_reply_documents(basis, payload, original_document=original_document)]
            if documents and payload.get("confirmed_documents") is not True:
                raise CaseError("Confirm that these exact checked documents accompanied your reply.")
            case = record_merchant_reply(path, payload.get("text"), revision, payload.get("confirmed_sent"), payload.get("record_id"), documents=documents)
        elif name == "merchant-followup":
            case = update_merchant_followup(path, payload.get("note"), payload.get("completed"), revision)
        elif name == "supporthandled":
            case = resolve_post_payment_support(path, payload.get("reason"), revision)
        elif name == "reopen":
            case = reopen_case(path, payload.get("text"), payload.get("case_revision"), imported_email=self.checked_email(payload.get("imported_email")), record_id=payload.get("record_id"))
        elif name == "approve":
            case = approve_case(path, payload.get("amount"), payload.get("confirmation"),
                                expected_case_revision=revision)
        elif name == "payment":
            case = correct_payment_details(path, payload.get("capture_id"), payload.get("ceiling"),
                                           payload.get("reason"), revision)
        elif name == "revise":
            case = revise_approval(path, payload.get("confirmation"), payload.get("approval_revision"))
        elif name == "close":
            case = close_case(path, payload.get("reason"), payload.get("confirmation"),
                              expected_case_revision=revision, followup_needed=payload.get("followup_needed", False))
        elif name == "execute":
            # Both revision and exact amount are checked under the case lock before provider reads.
            revision = payload.get("approval_revision")
            if type(revision) is not int or revision <= 0 or type(payload.get("confirmation")) is not str:
                raise CaseError("Reload the case and confirm the current approval before sending.")
            case = execute_case(path, None, gateway_factory=self.gateway_factory,
                                expected_approval_revision=revision,
                                send_confirmation=payload["confirmation"])
        elif name == "refresh":
            refund_id = payload.get("refund_id") or None
            confirm = payload.get("confirm_capture_link", False)
            if type(confirm) is not bool:
                raise CaseError("Capture-link confirmation must be a boolean.")
            case = refresh_case(path, self.gateway_factory(), refund_id, confirm)
        else:
            raise CaseError("Unknown case action.")
        try:
            self.working_drafts.retire_completed(path)
        except (CaseError, OSError):
            # The case action succeeded; reads still suppress consumed copies.
            pass
        return self.detail(key)


class LocalReturnlightHandler(BaseHTTPRequestHandler):
    """Exact-host, same-origin, bearer-token-gated local HTTP interface."""
    server_version = "ReturnlightLocal/1"
    sys_version = ""

    def log_message(self, format, *args):
        # Never log private buyer text, case IDs or approval phrases.
        pass

    @property
    def app(self) -> LocalCases:
        return self.server.cases

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") \
                and parsed.port == self.server.server_port and not parsed.username
        except ValueError:
            return False

    def _reply(self, status: int, value, content_type: str = "application/json; charset=utf-8",
               nonce: str | None = None):
        if content_type.startswith("application/json"):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        else:
            data = value if isinstance(value, bytes) else value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if content_type == "application/pdf" else "DENY")
        if nonce:
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'nonce-" + nonce +
                             "'; style-src 'self' 'nonce-" + nonce +
                             "'; connect-src 'self'; img-src 'self'; frame-src 'self'; base-uri 'none'; "
                             "form-action 'self'; frame-ancestors 'none'")
        elif content_type == "application/pdf":
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'self'")
        elif content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")
        else:
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, message: str, code: str = "REFUSED", review_diagnostics=None):
        value = {"ok": False, "error": message, "code": code}
        if review_diagnostics is not None:
            value["review_diagnostics"] = review_diagnostics
        self._reply(status, value)


    def _document_unavailable(self, key):
        target = "/#case=" + key
        page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Document unavailable · Returnlight</title>
<style>body{margin:0;background:#f4f6f0;color:#183b3e;font:17px/1.6 system-ui,sans-serif}
main{max-width:600px;margin:10vh auto;padding:36px;background:white}
.brand{font:700 24px Georgia,serif}h1{font:36px/1.15 Georgia,serif;margin-top:36px}
a{color:#285b48;text-underline-offset:4px}p{margin:20px 0}
@media(max-width:640px){body{background:white}main{margin:0;padding:28px 22px}h1{font-size:32px}}</style>
</head><body><main><div class="brand">Returnlight</div>
<h1>This document is unavailable</h1>
<p>The saved file is missing, changed, or no longer matches this link.</p>
<p><a href="__CASE_LINK__">Return to case</a></p>
</main></body></html>"""
        self._reply(422, page.replace("__CASE_LINK__", html.escape(target, quote=True)), "text/html; charset=utf-8")


    def _guard_host(self) -> bool:
        if not self._host_allowed():
            self._error(421, "Open this app using its loopback URL.")
            return False
        return True

    def do_GET(self):
        if not self._guard_host():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/":
                nonce = secrets.token_urlsafe(18)
                content = _HTML.read_text(encoding="utf-8")
                content = content.replace("__RETURNLIGHT_TOKEN__", html.escape(self.server.token, quote=True))
                content = content.replace("__RETURNLIGHT_NONCE__", nonce)
                self._reply(200, content, "text/html; charset=utf-8", nonce=nonce)
            elif path == "/assets/returnlight.css":
                self._reply(200, _HTML.with_suffix(".css").read_text(encoding="utf-8"), "text/css; charset=utf-8")
            elif path == "/assets/returnlight.dark.css":  # generated dark-mode twin of the sheet above
                self._reply(200, _HTML.with_suffix(".dark.css").read_text(encoding="utf-8"), "text/css; charset=utf-8")
            elif path == "/api/examples":
                example = json.loads(_HTML.with_name("returnlight_example.json").read_text(encoding="utf-8"))
                self._reply(200, {"ok": True, "examples": [{"key": "completed-sandbox-tea-strainer", "title": "Tea strainer · refunded", "example": True, "case": example}]})
            elif path == "/api/examples/completed-sandbox-tea-strainer/record":
                example = json.loads(_HTML.with_name("returnlight_example.json").read_text(encoding="utf-8"))
                self._reply(200, render_case_record(example), "text/html; charset=utf-8")
            elif match := _PHOTO_ROUTE.fullmatch(path):
                try:
                    data, mime = self.app.photos.preview(match.group(1), parse_qs(urlsplit(self.path).query).get("sha", [None])[0])
                    self._reply(200, data, mime)
                except (OSError, ValueError):
                    self._error(404, "Photo unavailable. Keep the original email.", "PHOTO_UNAVAILABLE")
            elif path == "/api/model-status":
                self._reply(200, {"ok": True, **(self.app.remote_reviewer.status() if self.app.remote_reviewer else {"mode": "local", **model_status(self.app.model_name)})})
            elif path == "/api/cases":
                self._reply(200, {"ok": True, "cases": self.app.list_cases()})
            elif match := _ORDER_CAPTURE_ROUTE.fullmatch(path):
                self._reply(200, {"ok": True, **self.app.order_payments(match.group(1))})
            elif match := _CAPTURE_ROUTE.fullmatch(path):
                self._reply(200, {"ok": True, **self.app.inspect_payment(match.group(1))})
            elif match := _CASE_ROUTE.fullmatch(path):
                if match.group(2) == "record":
                    detail = self.app.detail(match.group(1))
                    case = detail["case"]
                    self._reply(200, {"ok": True, "filename": "returnlight-case-" + case["id"][:8] + ".html",
                                      "html": render_case_record(case, photo_loader=self.app.photos.read,
                                                                 working_draft=detail["working_draft"])})
                elif match.group(2) == "reply-document":
                    try:
                        query = parse_qs(urlsplit(self.path).query)
                        if "reply" in query:
                            _, mime, data = self.app.read_recorded_reply_document(match.group(1), query.get("reply", [None])[0], query.get("document", [None])[0])
                            self._reply(200, data, mime)
                        elif query.get("source") == ["document"]:
                            reference, data = self.app.read_document_source(match.group(1), query.get("id", [None])[0], query.get("sha", [None])[0])
                            self._reply(200, data, reference["mime"])
                        else:
                            reference, data = self.app.read_reply_document(match.group(1), query.get("id", [None])[0], query.get("sha", [None])[0])
                            self._reply(200, data, reference["mime"])
                    except (CaseError, OSError, ValueError):
                        self._document_unavailable(match.group(1))
                elif match.group(2) == "document-text":
                    query = parse_qs(urlsplit(self.path).query)
                    page = int(query.get("page", ["1"])[0])
                    value = self.app.document_passages.preview(self.app.path(match.group(1)), query.get("file", [None])[0], page, self.app.photos, self.app.working_drafts)
                    self._reply(200, {"ok": True, **value})
                elif match.group(2) == "purchase-reply":
                    from .returnlight_record import purchase_details_reply, recorded_purchase_details_reply
                    case = case_snapshot(self.app.path(match.group(1)))
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if any(field in query for field in ("reply", "document", "sha")):
                        result = recorded_purchase_details_reply(
                            case, query.get("reply", [None])[0], query.get("document", [None])[0],
                            query.get("sha", [None])[0])
                    else:
                        result = {"text": purchase_details_reply(case),
                                  "purchase_context": case["purchase_context"]}
                    self._reply(200, {"ok": True, **result,
                                      "case_version": [case["capture_id"], case["state"], case["approved_cents"],
                                                       case.get("refund_id"), len(case["history"])]})
                elif match.group(2) == "purchase-details":
                    case = case_snapshot(self.app.path(match.group(1)))
                    document = render_purchase_details(case)
                    self._reply(200, {"ok": True, "filename": "purchase-details.html", "html": document,
                                      "document_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
                                      "case_version": [case["capture_id"], case["state"], case["approved_cents"],
                                                       case.get("refund_id"), len(case["history"])]})
                elif match.group(2) == "refunds":
                    key = match.group(1)
                    self._reply(200, {"ok": True, **self.app.inspect_refunds(key)})
                elif match.group(2) == "capture":
                    self._reply(200, {"ok": True, **self.app.inspect_capture(match.group(1))})
                elif match.group(2):
                    self._error(404, "No GET endpoint for that action.")
                else:
                    self._reply(200, {"ok": True, **self.app.detail(match.group(1))})
            else:
                self._error(404, "Not found.")
        except (CaseError, ValueError, KeyError) as exc:
            self._error(422, str(exc))
        except PayPalError:
            self._error(502, "PayPal Sandbox declined the capture lookup. Check the capture ID and Sandbox account access; no refund was sent.", "SANDBOX_REJECTED")
        except (OSError, RuntimeError):
            self._error(502, "PayPal Sandbox capture lookup unavailable. Check Sandbox credentials and network access; no refund was sent.", "SANDBOX_UNAVAILABLE")

    def do_POST(self):
        if not self._guard_host():
            return
        if not self._origin_allowed():
            self._error(403, "Cross-origin action refused.")
            return
        if not secrets.compare_digest(self.headers.get("X-Returnlight-Token", ""),
                                      self.server.token):
            self._error(403, "Missing local session confirmation.")
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._error(415, "Use JSON for local actions.")
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        path = urlsplit(self.path).path
        # Only email import accepts a larger envelope; case mutations retain their limit.
        limit = 4 * ((MAX_EMAIL_BYTES + 2) // 3) + 128 if path == "/api/intake/email" else _MAX_JSON_BYTES
        if (match := _CASE_ROUTE.fullmatch(path)) and match.group(2) in {"reply-document", "document-source"}:
            limit = 4 * ((MAX_PHOTO_BYTES + 2) // 3) + 2048
        if length < 0 or length > limit:
            self._error(413, "Invalid or excessive request size.")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if type(payload) is not dict:
                raise CaseError("Request must be a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/intake/email":
                self._reply(200, {"ok": True, **self.app.import_email(base64.b64decode(payload.get("email_base64", ""), validate=True))})
            elif path == "/api/cases":
                result = self.app.create(payload)
                self._reply(201, {"ok": True, **result})
            elif match := _CASE_ROUTE.fullmatch(path):
                name = match.group(2)
                if not name or name in ("capture", "refunds"):
                    self._error(405, "Capture inspection is read-only; use GET.")
                else:
                    self._reply(200, {"ok": True, **self.app.action(match.group(1), name, payload)})
            else:
                self._error(404, "Not found.")
        except ReviewInProgress as exc:
            self._error(409, str(exc), "REVIEW_IN_PROGRESS")
        except CaseChanged as exc:
            self._error(409, str(exc), "CASE_CHANGED")
        except OutcomeUnknown as exc:
            # The durable state is already UNKNOWN; no replay, including after page reload.
            self._error(409, str(exc), "OUTCOME_UNKNOWN")
        except (CaseError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(422, str(exc), review_diagnostics=getattr(exc, "review_diagnostics", None))
        except PayPalError:
            self._error(502, "PayPal Sandbox declined the payment lookup. Check the capture or refund ID and Sandbox account access. The saved case is unchanged; do not repeat an uncertain refund.", "SANDBOX_REJECTED")
        except (OSError, RuntimeError) as exc:
            operation = urlsplit(self.path).path.rsplit("/", 1)[-1]
            if operation == "review":
                self._error(502, "Local model review could not finish. Check the local model status, "
                            "then try Review again. This action does not send money.", "MODEL_UNAVAILABLE")
            elif operation in ("execute", "refresh"):
                self._error(502, "PayPal Sandbox could not be reached. Check the saved case state "
                            "before continuing; do not repeat a refund with an uncertain outcome.",
                            "SANDBOX_UNAVAILABLE")
            else:
                self._error(502, "The local case could not be saved or read. Check the private case "
                            "directory and refresh the case before continuing.", "LOCAL_STORAGE_UNAVAILABLE")


class LocalReturnlightServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], cases: LocalCases):
        if address[0] != "127.0.0.1":
            raise CaseError("Merchant UI is strictly loopback-only.")
        super().__init__(address, LocalReturnlightHandler)
        self.cases = cases
        self.token = secrets.token_urlsafe(32)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Returnlight private merchant case browser")
    parser.add_argument("--port", type=int, default=8747)
    parser.add_argument("--cases", type=Path,
                        default=Path.home() / ".local" / "share" / "returnlight" / "returnlight")
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--remote-review-url", help="Explicit HTTPS chat-completions endpoint for buyer text")
    parser.add_argument("--remote-review-model", help="Model at the selected remote endpoint")
    parser.add_argument("--remote-review-timeout", type=float, default=45, help="Whole remote request deadline, at most 120 seconds")
    parser.add_argument("--remote-review-proxy", help="Existing loopback HTTP CONNECT proxy for the selected HTTPS reviewer")
    parser.add_argument("--remote-review-no-auth", action="store_true", help="Explicitly select a reviewer that requires no API key")
    parser.add_argument("--remote-review-thinking", choices=["on", "off"], help="Explicit thinking mode for a reviewer supporting chat_template_kwargs")
    args = parser.parse_args(argv)
    if bool(args.remote_review_url) != bool(args.remote_review_model):
        parser.error("--remote-review-url and --remote-review-model must be selected together")
    if (args.remote_review_proxy or args.remote_review_no_auth or args.remote_review_thinking is not None) and not args.remote_review_url:
        parser.error("Remote proxy/authentication options require the selected remote endpoint")
    remote = RemoteReviewer(args.remote_review_url, args.remote_review_model, timeout=args.remote_review_timeout,
                            proxy_url=args.remote_review_proxy, no_auth=args.remote_review_no_auth,
                            thinking=None if args.remote_review_thinking is None else args.remote_review_thinking == "on") if args.remote_review_url else None
    cases = LocalCases(args.cases, model_name=args.model, remote_reviewer=remote)
    with LocalReturnlightServer(("127.0.0.1", args.port), cases) as server:
        print(f"Returnlight merchant UI: http://127.0.0.1:{server.server_port}/", flush=True)
        print(("Private local case files. Remote review selected: " + remote.destination + "; buyer text requires confirmation.") if remote else "Private local case files. Model via loopback Ollama; payments via PayPal Sandbox only.",
              flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
