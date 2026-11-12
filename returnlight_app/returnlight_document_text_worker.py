"""Isolated local PDF text extraction. Invoked by returnlight_document_sources."""
import io
import json
import sys

def main():
    # Bound decompression/parser resources before loading the external parser.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    except (ImportError, OSError, ValueError):
        # Platforms without resource limits are not currently supported for extraction.
        return {"error": "Local PDF text extraction is unavailable here. Open the original document."}
    try:
        from pypdf import PdfReader, __version__
        if __version__ != "7.0.1":
            return {"error": "The installed PDF reader version is unsupported. Open the original document."}
    except ImportError:
        return {"error": "The local PDF reader is not installed. Open the original document."}
    try:
        data = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError()
        page = int(sys.argv[1])
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {"error": "This PDF is encrypted. Open the original document."}
        count = len(reader.pages)
        if not 1 <= page <= count <= 10000:
            return {"error": "Choose an existing PDF page; documents over 10000 pages are not supported."}
        text = reader.pages[page - 1].extract_text(extraction_mode="layout").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return {"error": "This page has no selectable text. Open the original; scanned images are not read automatically."}
        if len(text) > 40000:
            return {"error": "This page exceeds the local text limit. Open the original document."}
        text.encode("utf-8")
        if any(ord(char) < 32 and char not in "\n\t" for char in text):
            return {"error": "This page contains unsupported text. Open the original document."}
        return {"text": text, "page_count": count}
    except Exception:
        return {"error": "This PDF page could not be read. Open the original document."}

if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=True))
