# Returnlight

Read the buyer's words, find the payment, refund exactly what was asked.

Returnlight is a local refund desk for small online sellers. A buyer's message, the original PayPal payment and the refund decision sit on one screen. A local AI model points at the sentence that matters; a person decides the amount and sends it. Software never moves money on its own.

## Try the simulated demo

```sh
python3 -m returnlight_app.returnlight_web --demo --port 5201
```

Open <http://127.0.0.1:5201/>. Needs Python 3.12+ and `pip install pypdf` (or `pip install .`); no PayPal keys and no local model.

1. Click **New case**, then in the yellow badge at the bottom left click **Fill sample message** and **Fill sample capture ID**.
2. Click **Find payment**: the simulated PayPal returns a completed $68.00 payment, the most this case can refund. Click **Use $68.00 as limit**, then **Open case**.
3. Click **Find an AI highlight**: a fixed answer highlights the sentence where the buyer asks for the carafe refund.
4. Click **I’ve checked the request and conditions**, choose **Refund buyer**, enter `40.00`, type the approval phrase shown, and click **Approve amount**.
5. Tick **I checked the available refund amount**, type the `SEND` phrase shown, and click **Send refund**: the case shows "Refund completed" with a simulated PayPal refund reference.

Simulated in this mode: every PayPal call (OAuth token, capture lookup, order lookup, refund, refund lookup) is answered in-process with Sandbox-shaped responses, and the AI step returns a fixed highlight instead of calling a model; a "Simulated demo: no real PayPal call" badge stays on screen. Refunds start from an existing payment, so there is no buyer approval page in this app. The real PayPal Sandbox and Ollama setup is described below. Each demo run uses a fresh scratch case folder (pass `--cases DIR` to keep one).

## Who it is for

Sellers who handle refunds from an inbox: craft shops, small stores, anyone who reads a three-paragraph complaint and then has to choose between refunding everything to be safe and refunding too little.

## How it uses AI

A local language model (served by [Ollama](https://ollama.com) on loopback) reads the buyer's message and highlights the exact passage that states what the buyer wants.

- It writes no summary, picks no category and suggests no amount.
- The highlighted words must appear verbatim in the buyer's message, or the result is rejected.
- A highlight is never an approval. The merchant confirms they have read the request themselves.

Built and tested with `qwen2.5:7b`; choose another installed model with `--model`.

## How it uses PayPal

All payment calls go to the PayPal REST API in **Sandbox**. The endpoint is fixed in code and production hosts are refused.

- **OAuth 2.0 client credentials** (`/v1/oauth2/token`), held by the local server only
- **Payments v2, show captured payment** (`GET /v2/payments/captures/{id}`) to look up the original payment, its amount and status
- **Orders v2, show order** (`GET /v2/checkout/orders/{id}`) to find a capture from an order ID
- **Payments v2, refund captured payment** (`POST /v2/payments/captures/{id}/refund`) for the approved amount, with a stable request ID
- **Payments v2, show refund** (`GET /v2/payments/refunds/{id}`) to reconcile when a response was lost

One capture gets one refund attempt. If PayPal's answer is lost or unclear the case is marked unknown and read back; a refund is never re-sent automatically.

## Run it

Requires Python 3.12 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install .
```

Explore the built-in example (no accounts needed):

```sh
.venv/bin/returnlight
# open http://127.0.0.1:8747
```

Run with a local model and PayPal Sandbox:

```sh
ollama pull qwen2.5:7b

export PAYPAL_CLIENT_ID="your sandbox client id"
export PAYPAL_CLIENT_SECRET="your sandbox client secret"
export RETURNLIGHT_ALLOW_NETWORK=1

.venv/bin/returnlight --model qwen2.5:7b --cases "$HOME/.returnlight/cases"
```

Create Sandbox credentials at <https://developer.paypal.com/dashboard/applications/sandbox>. You need a captured Sandbox payment to refund; make one with any Sandbox checkout and copy its capture ID.

Options: `--port` (default 8747), `--cases` (folder for private case files), `--model`.

## A two-minute walkthrough

1. Choose **New case**, paste the buyer's message and the PayPal capture ID, then **Find payment**. The real amount appears; use it as the refund limit and **Open case**.
2. Choose **Find an AI highlight**. The model marks the sentence that states the request.
3. Read it, then **I've checked the request and conditions**.
4. Open **Refund buyer**, enter the amount and type the approval phrase shown. Nothing is sent yet.
5. Tick the balance check, type the send phrase and **Send refund**. The case shows the refunded amount and PayPal's refund reference.

## Command line

The same state machine is available without the browser:

```sh
python -m returnlight_app.returnlight_cli demo
```

## Tests

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The PDF document tests need the Linux text-extraction path and fail on macOS; everything else runs anywhere.

## Privacy

Cases are plain JSON files on your machine. Credentials stay in the server's environment and never reach the browser. The server binds to loopback only.

## License

MIT. See [LICENSE](LICENSE).

## Light and dark mode

The interface follows the system colour scheme, and the round button in the bottom-right corner switches between light and dark. The second colour sheet is generated from the first with `tools/retheme.py`, so the two always share the same layout.
