import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from returnlight_app.returnlight_model_status import model_status
from returnlight_app.returnlight_web import LocalCases


class ModelStatusTests(unittest.TestCase):
    def inventory(self, names):
        def open_local(url, timeout):
            self.assertEqual(url, "http://127.0.0.1:11434/api/tags")
            self.assertEqual(timeout, 3)
            return io.BytesIO(json.dumps({"models": [{"name": n} for n in names]}).encode())
        return open_local

    def test_installed_is_not_claimed_as_inference(self):
        status = model_status("qwen2.5:3b", self.inventory(["qwen2.5:3b"]))
        self.assertEqual(status["state"], "available")
        self.assertIn("Review still needs to run", status["message"])
        self.assertEqual(status["commands"], [])

    def test_other_installed_model_does_not_hide_missing_configured_model(self):
        status = model_status("qwen2.5:3b", self.inventory(["qwen2.5:0.5b"]))
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["commands"], ["ollama pull qwen2.5:3b"])

    def test_unavailable_keeps_case_draft_work_possible(self):
        def offline(*args, **kwargs): raise ConnectionRefusedError()
        status = model_status("qwen2.5:3b", offline)
        self.assertEqual(status["state"], "unavailable")
        self.assertIn("still save a case", status["message"])
        self.assertEqual(status["commands"][0], "ollama serve")

    def test_inventory_check_never_reviews_or_creates_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = LocalCases(Path(tmp))
            with patch("returnlight_app.returnlight_model_status.urllib.request.build_opener") as make:
                make.return_value.open = self.inventory(["qwen2.5:3b"])
                status = model_status(cases.model_name)
            self.assertEqual(status["state"], "available")
            self.assertEqual(cases.list_cases(), [])

    def test_invalid_and_oversized_inventory_cannot_claim_available(self):
        for raw in [b"{}", b'{"models": null}', b"x" * 131073]:
            with self.subTest(raw_size=len(raw)):
                self.assertEqual(model_status("qwen2.5:3b", lambda *a, **kw: io.BytesIO(raw))["state"], "unavailable")

    def test_default_tag_supported(self):
        self.assertEqual(model_status("mistral", self.inventory(["mistral:latest"]))["state"], "available")
