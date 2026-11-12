"""Private image and PDF evidence retained beside Returnlight case files.

The existing photos reference and store remain compatible with saved image cases.
PDF references have zero raster dimensions and are read only by the native browser.
"""
import hashlib
import os
from pathlib import Path
import re
import secrets
import struct
import tempfile

MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 24_000_000
PHOTO_ID = re.compile(r"^[a-f0-9]{32}$")

def photo_format(data):
    """Identify bounded image or PDF bytes without decoding or executing content."""
    if not isinstance(data, bytes) or not 24 <= len(data) <= MAX_PHOTO_BYTES:
        return None
    if re.match(br"%PDF-(?:1\.[0-7]|2\.0)[\r\n ]", data) and data.rstrip().endswith(b"%%EOF"):
        return "application/pdf", 0, 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        mime = "image/png"
    elif data.startswith(b"\xff\xd8"):
        offset = 2
        found = None
        while offset + 4 <= len(data):
            if data[offset] != 255:
                return None
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):
                break
            if marker in range(0xD0, 0xD8) or marker == 0x01:
                continue
            if offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset:offset+2], "big")
            if length < 2 or offset + length > len(data):
                return None
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if length < 8:
                    return None
                height, width = struct.unpack(">HH", data[offset+3:offset+7])
                found = (width, height)
                break
            offset += length
        if not found:
            return None
        width, height = found
        mime = "image/jpeg"
    else:
        return None
    if not 1 <= width <= 16000 or not 1 <= height <= 16000 or width * height > MAX_PHOTO_PIXELS:
        return None
    return mime, width, height

def validate_photo(value):
    keys = {"id", "name", "mime", "bytes", "sha256", "width", "height"}
    if type(value) is not dict or set(value) != keys:
        raise ValueError("Invalid saved photo reference.")
    if type(value["id"]) is not str or not PHOTO_ID.fullmatch(value["id"]):
        raise ValueError("Invalid saved photo reference.")
    if (type(value["name"]) is not str or not 1 <= len(value["name"]) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in value["name"])):
        raise ValueError("Invalid photo filename.")
    if value["mime"] not in ("image/png", "image/jpeg", "application/pdf"):
        raise ValueError("Only PNG, JPEG and PDF evidence is supported.")
    if type(value["bytes"]) is not int or not 24 <= value["bytes"] <= MAX_PHOTO_BYTES:
        raise ValueError("Invalid photo size.")
    if type(value["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ValueError("Invalid photo digest.")
    if value["mime"] == "application/pdf":
        if any(type(value[k]) is not int or value[k] != 0 for k in ("width", "height")):
            raise ValueError("PDF evidence cannot contain raster dimensions.")
    elif any(type(value[k]) is not int or not 1 <= value[k] <= 16000 for k in ("width", "height")) or value["width"] * value["height"] > MAX_PHOTO_PIXELS:
        raise ValueError("Photo dimensions exceed the supported size.")
    return dict(value)

def photo_contexts(case):
    return [case.get("imported_email")] + [entry.get("imported_email") for entry in case.get("buyer_followups", []) + case.get("post_payment_messages", [])]

class PhotoStore:
    def __init__(self, case_directory):
        self.directory = Path(case_directory) / "photos"
        if self.directory.is_symlink():
            raise ValueError("The photo directory cannot be a symbolic link.")

    def path(self, key):
        if self.directory.is_symlink():
            raise ValueError("The photo directory cannot be a symbolic link.")
        if type(key) is not str or not PHOTO_ID.fullmatch(key):
            raise ValueError("Invalid photo reference.")
        path = self.directory / (key + ".photo")
        if path.is_symlink():
            raise ValueError("Symbolic-link photos are not supported.")
        return path

    def put(self, data, filename):
        format = photo_format(data)
        if format is None:
            return None
        mime, width, height = format
        name = "".join(c for c in (filename or "") if ord(c) >= 32 and ord(c) != 127)[:200].strip()
        name = name or ({"image/png":"Attached photo.png","image/jpeg":"Attached photo.jpg","application/pdf":"Attached document.pdf"}[mime])
        key = secrets.token_hex(16)
        self.path(key)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".photo-", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path(key))
            for directory in (self.directory, self.directory.parent):
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"id":key, "name":name, "mime":mime, "bytes":len(data),
                "sha256":hashlib.sha256(data).hexdigest(), "width":width, "height":height}

    def read(self, reference):
        reference = validate_photo(reference)
        path = self.path(reference["id"])
        if path.stat().st_size != reference["bytes"]:
            raise ValueError("Saved photo size changed. Import the original email again.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("Saved photo changed. Import the original email again.")
        if photo_format(data) != (reference["mime"], reference["width"], reference["height"]):
            raise ValueError("Saved photo format changed.")
        return data

    def preview(self, key, expected_sha):
        if type(expected_sha) is not str or not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            raise ValueError("Invalid photo digest.")
        path = self.path(key)
        if path.stat().st_size > MAX_PHOTO_BYTES:
            raise ValueError("Photo exceeds the supported size.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise ValueError("Saved photo changed. Import the original email again.")
        format = photo_format(data)
        if format is None:
            raise ValueError("Photo is unavailable.")
        return data, format[0]
