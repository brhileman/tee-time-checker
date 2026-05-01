"""FastAPI app — Twilio webhook + lifespan-managed watch scheduler.

One process serves both responsibilities: the HTTP server that handles
inbound SMS webhooks AND the periodic watch poller. They share the
same SQLite file and the same notifier, so a watch firing while a
webhook is in flight goes through the same outbound path.

Inbound flow:

    Twilio POST /sms (form-encoded)
        ↓ signature check
    schedule background task to handle the message
        ↓ (returns empty TwiML to Twilio immediately)
    sms.handle_sms(phone, body, notifier=...)
        ↓
    notifier.notify(phone, reply)  → Twilio REST API → user's phone

Returning fast keeps Twilio's 15-second webhook timeout from biting
when the search or NL parse has tail latency.

Outbound flow (watches):

    BackgroundScheduler tick (every tick_seconds)
        ↓
    watcher.process_due(notifier=...)
        ↓
    notifier.notify(phone, summary or expiry message)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from tee_time_checker import sms, state, watcher

log = logging.getLogger(__name__)

# Tick interval for the watch scheduler. The scheduler reads only watches
# that are *due* (next_check_at <= now), so this just controls how often
# we wake up to look — actual per-watch cadence is the 8-13 min jitter
# stored in next_check_at by the watcher itself.
_TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "60"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the watch poller alongside the HTTP server.

    BackgroundScheduler runs in a daemon thread — async/await on the
    web side stays cooperative; the scheduler ticks happen on its own
    thread. SQLite handles the concurrency (we use WAL mode in state.py).
    """
    notifier = watcher.default_notifier()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: watcher.process_due(notifier),
        trigger="interval",
        seconds=_TICK_SECONDS,
        next_run_time=datetime.now(tz=ZoneInfo("UTC")),  # fire once on boot
        id="process_due",
        max_instances=1,  # don't pile up if a tick runs longer than the interval
    )
    scheduler.add_job(
        state.purge_expired_pending,
        trigger="interval",
        minutes=15,
        id="purge_expired_pending",
        max_instances=1,
    )
    scheduler.start()
    log.info("scheduler started: tick=%ds, notifier=%s", _TICK_SECONDS, type(notifier).__name__)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Simple liveness probe — Fly.io / monitors hit this."""
    return {"status": "ok"}


@app.post("/sms")
async def sms_webhook(request: Request, background: BackgroundTasks) -> Response:
    """Twilio POSTs here when an SMS arrives at our number.

    Validates the request signature, then schedules the actual handling
    on a background task and immediately returns an empty TwiML response.
    The reply goes back to the user via the Twilio REST API (not via
    this response body) — that decouples our work from Twilio's
    15-second webhook timeout.
    """
    form = await request.form()
    _validate_twilio_signature(request, form)

    phone = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()
    if phone and body:
        notifier = watcher.default_notifier()
        background.add_task(_handle_sms_safe, phone, body, notifier)

    # Empty TwiML response — we send the reply async via the REST API.
    return Response(content="<Response/>", media_type="text/xml")


def _handle_sms_safe(phone: str, body: str, notifier) -> None:
    """Wrap the handler so background-task exceptions don't get swallowed silently.

    FastAPI's BackgroundTasks only logs to its own logger by default —
    explicit logging here makes diagnosis easy when something blows up
    server-side and the user just sees no reply.
    """
    try:
        sms.handle_sms(phone, body, notifier=notifier)
    except Exception:
        log.exception("sms.handle_sms failed for %s", phone)


def _validate_twilio_signature(request: Request, form: object) -> None:
    """Reject webhooks that aren't signed by Twilio.

    `TWILIO_AUTH_TOKEN` is the HMAC key. The validator hashes
    `request.url + sorted(form_kv_pairs)` and compares to the
    `X-Twilio-Signature` header. Skipped only when SKIP_TWILIO_VERIFY=1
    is set (used by the simulated-webhook CLI command for local testing).
    """
    if os.environ.get("SKIP_TWILIO_VERIFY") == "1":
        return

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not auth_token:
        # Fail closed — refuse to accept webhooks if we can't verify them.
        raise HTTPException(
            status_code=503,
            detail="TWILIO_AUTH_TOKEN not configured; webhook signature can't be verified",
        )

    from twilio.request_validator import RequestValidator

    validator = RequestValidator(auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    # Twilio signs the public URL; if we're behind a proxy, trust
    # X-Forwarded-* via the trusted-host config. For Fly.io the URL
    # passed through is already canonical.
    url = str(request.url)

    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
