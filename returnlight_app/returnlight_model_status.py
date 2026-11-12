"""Bounded local Ollama inventory check; never inference or a case mutation."""
import json
import shlex
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def model_status(model, opener=None):
    result = {"model": model, "state": "unavailable",
              "message": "Ollama is not responding on this computer. Start Ollama, then check again. You can still save a case.",
              "commands": ["ollama serve", "ollama pull " + shlex.quote(model)]}
    try:
        open_local = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()).open
        with open_local("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("Inventory too large")
        payload = json.loads(raw)
        models = payload["models"]
        if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
            raise ValueError("Invalid inventory")
        names = {m.get("name") or m.get("model") for m in models}
        wanted = model if ":" in model else model + ":latest"
        if model in names or wanted in names:
            result.update(state="available", commands=[], message=
                "Configured model is installed. Review still needs to run and may take time; no buyer evidence has been analyzed by this check.")
        else:
            result.update(state="missing", commands=["ollama pull " + shlex.quote(model)],
                message="Ollama is running, but this configured model is not installed. Download it in a terminal, then check again.")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result
