import base64
import threading
import urllib.error
import urllib.request

import pytest

from returnlight_app.returnlight import new_case, record_merchant_reply
from returnlight_app.returnlight_web import LocalCases, LocalReturnlightServer

PDF = b"%PDF-1.4\n1 0 obj <</Type/Catalog>> endobj\n%%EOF\n"


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_unavailable_document_is_local_and_keeps_case(tmp_path, historical, fault):
    key = "a" * 32
    path = tmp_path / (key + ".json")
    case = new_case(path, "RECEIPTCAPTURE01", "30.50", "Please send the original receipt.")
    def no_gateway():
        pytest.fail("A local document read must not contact payment services")
    app = LocalCases(tmp_path, gateway_factory=no_gateway)
    work = app.action(key, "reply-document", {
        "case_revision": len(case["history"]), "draft_version": None,
        "filename": "original.pdf", "data_base64": base64.b64encode(PDF).decode()
    })["working_draft"]
    ref = work["draft"]["reply_document"]
    if historical:
        record_merchant_reply(path, "I sent the original.", len(case["history"]), True,
                              "b" * 32, documents=[{"kind": "original", "file": ref}])
        query = "?reply=" + "b" * 32 + "&document=0"
    else:
        query = "?id=" + ref["id"] + "&sha=" + ref["sha256"]
    before = path.read_bytes()
    if fault == "missing":
        app.photos.path(ref["id"]).unlink()
    else:
        app.photos.path(ref["id"]).write_bytes(PDF.replace(b"Catalog", b"Changed"))
    server = LocalReturnlightServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(origin + "/api/cases/" + key + "/reply-document" + query)
        response = refused.value
        assert response.code == 422
        assert response.headers["Content-Type"].startswith("text/html")
        body = response.read().decode()
        assert "This document is unavailable" in body
        assert 'href="/#case=' + key + '"' in body
        assert "PayPal" not in body and "credentials" not in body
        assert str(tmp_path) not in body and "Traceback" not in body
        assert path.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
