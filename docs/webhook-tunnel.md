# Closing the last gap: letting Razorpay's own servers deliver the webhook

`scripts/s6_verify.py` proves the whole inbound path with real bodies, the real
secret and real HMAC-SHA256 over the exact bytes sent. One hop in it is not real:
the POST comes from the harness rather than from Razorpay. This document closes
that.

## Why localhost cannot be used directly

Razorpay refuses to register a webhook URL unless it is:

* on a **public host** — `127.0.0.1` and `localhost` are rejected outright;
* on **port 80 or 443** only. No custom ports, so `:8123` cannot be registered
  even behind a public DNS name;
* **not** an ngrok-class tunnel. Razorpay blacklists those domains. Their own
  documentation recommends **zrok** for local testing, which is what these steps
  use.

## Steps

### 1. Install and enable zrok

```bash
winget install OpenZiti.zrok
```

Create an account at <https://api.zrok.io> (free), copy the token from the
console, then enable this machine once:

```bash
zrok enable <YOUR_ZROK_TOKEN>
```

### 2. Start the API

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8123
```

### 3. Open a public HTTPS tunnel to it

```bash
zrok share public http://127.0.0.1:8123 --headless
```

zrok prints a URL of the form `https://<random>.share.zrok.io`. That is on port
443 and on a public host, which is what Razorpay requires. Leave this running —
the URL changes every time the share restarts, and Razorpay has to be updated
when it does.

### 4. Register the webhook in the Razorpay dashboard

1. Dashboard → **Account & Settings** → **Webhooks** → **Add New Webhook**.
2. **Webhook URL**: `https://<random>.share.zrok.io/webhooks/razorpay`
3. **Secret**: paste the exact value already in `.env` as
   `RAZORPAY_WEBHOOK_SECRET`. If these two differ, every delivery is answered
   `400 SignatureMismatch` — which is the verifier working correctly, not a bug.
4. **Active Events**: tick exactly these three, and nothing else:
   - `payment_link.paid`
   - `payment_link.expired`
   - `payment_link.cancelled`

   Any other event is acknowledged with `200 processed=false` rather than
   recorded, so subscribing to more just adds noise.
5. Confirm with the test-mode dashboard OTP: **`754081`**.

Make sure the dashboard is in **Test Mode** when doing this. Test-mode and
live-mode webhooks are configured separately and have separate secrets.

### 5. Trigger a real delivery

Read a live payment-link URL off a Stage 5 execution:

```bash
curl -s "http://127.0.0.1:8123/executions?history=true" | grep -o '"razorpay_payment_link_url":"[^"]*"'
```

Open one of those URLs in a browser and pay it with Razorpay's published test
card:

| Field | Value |
| --- | --- |
| Card number | `4111 1111 1111 1111` |
| Expiry | any future date, e.g. `12/30` |
| CVV | any 3 digits, e.g. `123` |
| Name | anything |
| 3-D Secure OTP | `754081` |

Razorpay then delivers `payment_link.paid` to the zrok URL. Watch the uvicorn log
for:

```
app.webhooks.signature: Verified webhook <id> event=payment_link.paid (NNNN bytes)
app.webhooks.store: Recorded recovered for event <event_id> from Razorpay event <id> ...
app.webhooks.store: event '<event_id>' status -> 'recovered'
```

and confirm the record:

```bash
curl -s "http://127.0.0.1:8123/verifications?history=true"
```

### 6. What to expect that looks like a failure but isn't

* **Razorpay's dashboard shows a delivery attempt as failed with 400.** Check the
  secret matches `.env` exactly. Razorpay retries with exponential backoff for 24
  hours and then **disables the webhook**, so fix it and re-enable rather than
  waiting.
* **The same event arrives more than once.** Expected — delivery is at-least-once.
  The second one is answered `200 processed=false detail="duplicate delivery"` and
  writes nothing.
* **`payment_link.expired` arrives after `payment_link.paid`.** Also expected —
  Razorpay does not guarantee ordering. The expiry is recorded as a true statement
  about the link, and the event's status stays `recovered` because that state is
  terminal. The acknowledgement says the transition was refused.
* **A delivery for a link this system never created.** Answered `200
  processed=false`, logged as a warning, deliberately not matched to any event.

## Verifying without the dashboard

`scripts/s6_verify.py` needs no tunnel and no dashboard. It signs with the real
secret and covers 63 assertions including tampering, replay, concurrency,
out-of-order delivery and amount mismatch. The tunnel adds exactly one thing the
script cannot: proof that Razorpay's own servers can reach the endpoint and that
their signature is accepted by this verifier.
