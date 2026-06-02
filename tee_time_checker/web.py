"""FastAPI app + Discord bot + watch scheduler — all in one process.

Architecture:

    Discord gateway (websocket)
        ↓  on_message
    TeeTimeBot.on_message()
        ↓  asyncio.to_thread (keeps the event loop free)
    sms.handle_sms(user_key, body, notifier=...)
        ↓
    notifier.notify(user_key, reply)  →  channel.send()  →  Discord

    BackgroundScheduler (APScheduler, daemon thread) tick every N seconds
        ↓
    watcher.process_due(notifier=...)
        ↓
    notifier.notify(user_key, summary)  →  channel.send()

user_key encodes both the author and the channel: "{user_id}:{channel_id}".
DiscordNotifier unpacks it to send the reply to the right place.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

import discord
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Response

from tee_time_checker import sms, state, watcher
from tee_time_checker.watcher import DiscordNotifier

log = logging.getLogger(__name__)

_TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "60"))
# Optional: restrict the bot to one channel. If unset, responds everywhere it's invited.
_DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")


class TeeTimeBot(discord.Client):
    """Discord client that routes inbound messages through the SMS handler."""

    def __init__(self, notifier: DiscordNotifier) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._notifier = notifier

    async def on_ready(self) -> None:
        log.info("Discord bot ready: %s (id=%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user or message.author.bot:
            return

        in_thread = isinstance(message.channel, discord.Thread)

        if _DISCORD_CHANNEL_ID:
            # Accept messages in the configured channel, and in threads spawned from it.
            parent_id = str(message.channel.parent_id) if in_thread else None
            if str(message.channel.id) != _DISCORD_CHANNEL_ID and parent_id != _DISCORD_CHANNEL_ID:
                return

        body = message.content.strip()
        if not body:
            return

        # Messages in a thread continue that thread's conversation.
        # Messages in a regular channel spawn a new thread for the reply.
        if in_thread:
            reply_channel_id = message.channel.id
        else:
            thread = await message.create_thread(
                name=f"Tee time — {message.author.display_name}",
                auto_archive_duration=60,
            )
            reply_channel_id = thread.id

        user_key = f"{message.author.id}:{reply_channel_id}"
        notifier = self._notifier
        await asyncio.to_thread(lambda: sms.handle_sms(user_key, body, notifier=notifier))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the Discord bot and watch scheduler; shut both down on exit."""
    notifier = DiscordNotifier()
    bot = TeeTimeBot(notifier)
    notifier.set_client(bot)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: watcher.process_due(notifier),
        trigger="interval",
        seconds=_TICK_SECONDS,
        next_run_time=datetime.now(tz=ZoneInfo("UTC")),
        id="process_due",
        max_instances=1,
    )
    scheduler.add_job(
        state.purge_expired_pending,
        trigger="interval",
        minutes=15,
        id="purge_expired_pending",
        max_instances=1,
    )
    scheduler.start()
    log.info("scheduler started: tick=%ds", _TICK_SECONDS)

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        bot_task = asyncio.create_task(bot.start(token))
        log.info("Discord bot connecting…")
    else:
        bot_task = None
        log.warning("DISCORD_BOT_TOKEN not set — bot will not connect (scheduler still runs)")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if bot_task is not None:
            await bot.close()
            bot_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
