import base64
import hashlib
import json
import struct
import zlib
from email.message import EmailMessage

import pytest

from returnlight_app.returnlight import CaseError, add_buyer_followup, case_snapshot
from returnlight_app.returnlight_photos import PhotoStore, photo_format, validate_photo
from returnlight_app.returnlight_record import render_case_record
from returnlight_app.returnlight_web import LocalCases

def png(width=32, height=20):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x34\x67\x89" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

def email(photo, filename="receipt.png"):
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["Subject"] = "Receipt for the replacement"
    message.set_content("Please review this receipt before deciding about the replacement.")
    message.add_attachment(photo, maintype="image", subtype="png", filename=filename)
    return message.as_bytes()

def test_original_photo_survives_intake_case_reload_and_private_export(tmp_path):
    app = LocalCases(tmp_path)
    data = png()
    imported = app.import_email(email(data))
    photo = imported["photos"][0]
    assert imported["attachments_omitted"] == 0
    assert app.photos.read(photo) == data
    assert app.photos.path(photo["id"]).stat().st_mode & 0o777 == 0o600
    context = {k: imported[k] for k in ("sender", "subject", "photos")}
    result = app.create({"capture_id":"PHOTOCASE123", "ceiling":"12.00",
                         "complaint":imported["complaint"], "imported_email":context})
    reopened = LocalCases(tmp_path).detail(result["key"])["case"]
    assert reopened["imported_email"]["photos"] == [photo]
    assert reopened["request_id"] is None and reopened["approved_cents"] == 0
    record = render_case_record(reopened, photo_loader=app.photos.read)
    assert base64.b64encode(data).decode() in record
    assert "Download original photo" in record
    assert "img-src data:" in record
    assert photo["name"] in record

def test_source_photo_stays_with_later_message(tmp_path):
    app = LocalCases(tmp_path)
    result = app.create({"capture_id":"PHOTOCASE123","ceiling":"12.00","complaint":"The original shade arrived damaged. Please help me."})
    imported = app.import_email(email(png(), "replacement.png"))
    context = {k: imported[k] for k in ("sender", "subject", "photos")}
    case = add_buyer_followup(app.path(result["key"]), imported["complaint"],
                              len(result["case"]["history"]), imported_email=context)
    assert "imported_email" not in case
    assert case["buyer_followups"][-1]["imported_email"] == context
    assert case["state"] == "OPEN" and case["request_id"] is None

def test_changed_photo_cannot_be_previewed_saved_or_silently_exported(tmp_path):
    app = LocalCases(tmp_path)
    imported = app.import_email(email(png()))
    photo = imported["photos"][0]
    app.photos.path(photo["id"]).write_bytes(png(33))
    with pytest.raises(ValueError):
        app.photos.preview(photo["id"], photo["sha256"])
    with pytest.raises(CaseError, match="unavailable or changed"):
        app.create({"capture_id":"PHOTOCASE123","ceiling":"12.00","complaint":imported["complaint"],
                    "imported_email":{k: imported[k] for k in ("sender","subject","photos")}})
    assert not list(tmp_path.glob("*.json"))

def test_unsupported_attachment_remains_explicitly_omitted(tmp_path):
    app = LocalCases(tmp_path)
    imported = app.import_email(email(b"not a raster photo"))
    assert imported["attachments_omitted"] == 1
    assert "photos" not in imported
    assert not list((tmp_path/"photos").glob("*.photo"))

def test_photo_reference_validation_and_pixel_bound(tmp_path):
    store = PhotoStore(tmp_path)
    photo = store.put(png(), "receipt.png")
    assert store.preview(photo["id"], photo["sha256"]) == (png(), "image/png")
    with pytest.raises(ValueError):
        validate_photo({**photo,"id":"../other"})
    oversized = bytearray(png())
    oversized[16:24] = struct.pack(">II", 16000,16000)
    assert photo_format(bytes(oversized)) is None
    assert photo_format(b"<svg></svg>") is None

def test_original_names_are_escaped_in_private_record(tmp_path):
    app = LocalCases(tmp_path)
    imported = app.import_email(email(png(), "<receipt>.png"))
    context = {k: imported[k] for k in ("sender","subject","photos")}
    result = app.create({"capture_id":"PHOTOCASE123","ceiling":"12.00","complaint":imported["complaint"],"imported_email":context})
    record = render_case_record(result["case"], photo_loader=app.photos.read)
    assert "&lt;receipt&gt;.png" in record and "<receipt>.png" not in record

@pytest.mark.parametrize("photos", [None, {}, "photo", [], [None]])
def test_invalid_photo_context_is_rejected_before_reading_files(tmp_path, photos):
    app = LocalCases(tmp_path)
    with pytest.raises(CaseError):
        app.create({"capture_id":"PHOTOCASE123", "ceiling":"12.00",
                    "complaint":"Please inspect the photo of my damaged item.",
                    "imported_email":{"sender":"Buyer", "subject":"Photo", "photos":photos}})
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("content_type", ["pdf", "octet-stream"])
def test_pdf_receipt_keeps_exact_bytes_through_saved_message_and_export(tmp_path, content_type):
    app = LocalCases(tmp_path)
    data = b"%PDF-1.7\n% authored bounded storage fixture, not a rendered document\n%%EOF\n"
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["Subject"] = "Purchase receipt for the missing cup"
    message.set_content("The PDF shows two cups and shipping. Please refund only the missing cup.")
    message.add_attachment(data, maintype="application", subtype=content_type, filename="<purchase>.pdf")
    imported = app.import_email(message.as_bytes())
    reference = imported["photos"][0]
    assert imported["attachments_omitted"] == 0
    assert reference["mime"] == "application/pdf"
    assert reference["width"] == reference["height"] == 0
    assert app.photos.read(reference) == data
    assert app.photos.preview(reference["id"], reference["sha256"]) == (data, "application/pdf")
    context = {k:imported[k] for k in ("sender","subject","photos")}
    created = app.create({"capture_id":"PDFRECEIPT123", "ceiling":"12.00", "complaint":imported["complaint"], "imported_email":context})
    loaded = LocalCases(tmp_path).detail(created["key"])["case"]
    exported = render_case_record(loaded, photo_loader=app.photos.read)
    assert "Download original PDF" in exported and "&lt;purchase&gt;.pdf" in exported
    assert base64.b64encode(data).decode() in exported
    assert "frame-src data:" in exported
    with pytest.raises(ValueError):
        validate_photo({**reference, "width":10})
    app.photos.path(reference["id"]).write_bytes(data.replace(b"authored",b"modified"))
    with pytest.raises(ValueError):
        app.photos.read(reference)

def test_non_pdf_and_truncated_documents_are_not_retained(tmp_path):
    store = PhotoStore(tmp_path)
    assert store.put(b"<html>This is not a PDF document.</html>", "receipt.pdf") is None
    assert store.put(b"%PDF-1.7\ntruncated document with no end marker", "receipt.pdf") is None
