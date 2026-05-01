# Tee Time Checker

SMS-driven tee time availability search across multiple golf booking platforms.

You text the bot what you want — *"tee time tomorrow afternoon for 2 at westminster"* — and it searches every configured course in parallel, replies with what's available, and (if nothing is) keeps watching for 24h and texts you the moment something opens up.

## Architecture

One Python process serves both responsibilities:

```
Twilio  →  POST /sms  →  parse (Claude API)  →  search (5 platforms in parallel)
                                                     ↓
   user's phone  ←  Twilio REST API  ←  SMS summary or clarification

                        meanwhile, in the same process:
        BackgroundScheduler  →  every minute, process due watches
                                     ↓
                          search runs → if hit, fire SMS → mark watch done
```

Configured platforms (`courses.toml`):

| Platform        | Adapter         | Auth required | Courses |
|-----------------|-----------------|---------------|---------|
| Club Prophet    | `cps`           | none          | Westminster, Fossil Trace |
| MemberSports    | `membersports`  | none          | Fox Hollow, Homestead, City Park, Wellshire, Willis Case |
| TeeItUp / Kenna | `teeitup`       | none          | Riverdale Dunes, Riverdale Knolls, CommonGround |
| Quick18         | `quick18`       | none          | Thorncreek |
| Noteefy         | `noteefy`       | none + curl_cffi for Cloudflare TLS | Broadlands |

Add a new course by adding a `[[targets]]` block to `courses.toml`. Add a new platform by writing an `Adapter` implementation; see `tee_time_checker/adapters/`.

---

## Local development

You need:

- Python 3.12+ (we test on 3.12)
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- An Anthropic API key for the natural-language parser

```bash
# Install
git clone https://github.com/brhileman/tee-time-checker.git
cd tee-time-checker
uv sync

# Set your API key
cp .env.example .env
# Edit .env, paste your ANTHROPIC_API_KEY

# Try it
uv run tt search --date tomorrow --players 2 --window afternoon
uv run tt ask "tee time saturday afternoon for 2"
uv run tt chat        # multi-turn REPL
uv run tt sms reply "tee time saturday afternoon for 2"  # simulated inbound SMS
```

### CLI commands

| Command | Purpose |
|---|---|
| `tt search` | Run a search with explicit args |
| `tt parse "<msg>"` | NL parse only — print the extracted criteria |
| `tt ask "<msg>"` | Parse + search, single-shot |
| `tt chat` | Multi-turn REPL — simulates SMS dialog in-memory |
| `tt sms reply "<msg>"` | Simulate one inbound SMS through the real handler (uses SQLite state) |
| `tt watch start "<msg>"` | Start a 24h watch from a natural-language message |
| `tt watch list / cancel / tick / run` | Manage / drive the watch scheduler |
| `tt server` | Run the FastAPI webhook + scheduler (the production entry point) |

### Time windows

The parser maps these to the same buckets as the booking pages use:

- `morning` — open → 10am
- `midday` — 10am → 2pm
- `afternoon` — 2pm → close
- `any` — full day

---

## Deploy to Fly.io

Run the whole stack on a single small Fly machine. ~$3–5/mo.

### Prerequisites

- A Fly.io account (`brew install flyctl && flyctl auth login`)
- A Twilio account with a phone number (US, A2P 10DLC registered for SMS — see below)

### One-time setup

```bash
# From the repo root
flyctl launch --no-deploy --copy-config
# Answer prompts: pick an app name, region (iad recommended), skip Postgres/Redis.
# This will overwrite `app = "tee-time-checker"` in fly.toml with your chosen name.

# Persistent volume for SQLite (state survives deploys)
flyctl volumes create tee_time_data --region iad --size 1

# Set production secrets
flyctl secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  TWILIO_ACCOUNT_SID=ACxxxxxxxx \
  TWILIO_AUTH_TOKEN=xxxxxxxx \
  TWILIO_FROM_NUMBER=+13035551234   # E.164 format
```

### Deploy

```bash
flyctl deploy
```

When the deploy completes, your webhook URL is:

```
https://<your-app-name>.fly.dev/sms
```

Visit `/healthz` in a browser to confirm the server is up:

```
https://<your-app-name>.fly.dev/healthz
```

### Wire up Twilio

In the Twilio Console:

1. Go to **Phone Numbers → Manage → Active Numbers**, click your number.
2. Under **Messaging Configuration**, set:
   - **A message comes in**: `Webhook` → `https://<your-app-name>.fly.dev/sms` → method `HTTP POST`
3. Save.

Send a test SMS to your Twilio number from any phone. Watch live logs:

```bash
flyctl logs
```

You should see the inbound webhook hit, the parser run, and the outbound SMS send.

### A2P 10DLC registration (required before sending real US SMS)

Twilio requires A2P 10DLC registration to send SMS to US numbers. The Sole Proprietor path is right for personal projects:

1. **Twilio Console → Messaging → Regulatory Compliance → A2P 10DLC**
2. Register a Brand (Sole Proprietor — free, takes ~24h)
3. Register a Campaign (~$2/mo). Suggested description:

> *Personal tee time availability alert service for friends and family. Users text natural-language requests (e.g. "tee time tomorrow afternoon for 2") and receive SMS notifications when matching tee times become available at courses they've configured. Opt-in only via direct invitation; STOP/HELP keywords supported.*

Provide:
- Sample message: *"✅ Sat 5/3 afternoon, 2p (18h) — Walnut Creek: 8 slots, 2:50p–5:50p"*
- HELP and STOP keywords are already implemented (see `tee_time_checker/sms.py`)

Daily cap on the Sole Proprietor tier is 1,000 messages — plenty for a friend group.

---

## Config reference

### Environment variables

| Variable | Required for | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Parser, server | Claude API |
| `TWILIO_ACCOUNT_SID` | Production server | Outbound SMS |
| `TWILIO_AUTH_TOKEN` | Production server | Outbound SMS + webhook signature verification |
| `TWILIO_FROM_NUMBER` | Production server | The Twilio number SMS comes from (E.164) |
| `TICK_SECONDS` | Optional (default 60) | How often the watch scheduler wakes up |
| `TEE_TIME_DB_PATH` | Optional (default `./tee_time_checker.db`) | SQLite file location. Set to `/data/...` in Docker. |
| `SKIP_TWILIO_VERIFY` | Local testing only | When `=1`, the `/sms` endpoint accepts unsigned requests (dev only) |
| `LOG_LEVEL` | Optional (default INFO) | Stdlib logging level |

### Adding a course

Open `courses.toml` and add a `[[targets]]` block matching the platform's expected params. Existing entries show the shape for each adapter — the comments in each adapter module document what `params.*` keys are required.

For a new platform entirely, you'll need to write an Adapter:

1. Run `investigation/capture.py <slug> <booking-page-url>` to capture the platform's network calls
2. Identify the search endpoint and required headers
3. Write `tee_time_checker/adapters/<slug>.py` implementing the `Adapter` protocol
4. Register it in `tee_time_checker.search.build_default_registry()`
5. Add `[[targets]]` entries to `courses.toml`

---

## Development workflow

```bash
# Run the scheduler in foreground (with print notifier)
uv run tt watch run

# In another shell — drive it with simulated inbound SMS
uv run tt sms reply --phone +15551234567 "tee time saturday afternoon for 2"

# Or run the full webhook server locally
SKIP_TWILIO_VERIFY=1 uv run tt server
# Then POST form data to http://localhost:8080/sms
```

The investigation scripts live under `investigation/` — they're how each adapter was built (capture network calls, identify API shape, enumerate field meanings). Helpful when adding a new course on an unknown platform.

---

## License

Personal project, no formal license. If you fork it for your own friend group, that's the intent.
