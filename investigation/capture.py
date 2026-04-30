"""
Generalized network capture for any tee time booking page.

Usage:
    uv run python investigation/capture.py <slug> <url>

Produces:
    investigation/<slug>_summary.txt          — human-readable summary of API calls
    investigation/<slug>_requests.jsonl       — every interesting request/response
    investigation/<slug>_screenshot.png       — full-page screenshot
    investigation/<slug>_bodies/<n>.json      — body of every JSON response (numbered by capture order)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

OUT_DIR = Path(__file__).parent

INTERESTING_TYPES = {"xhr", "fetch", "document"}
NOISE_HOSTS = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|hotjar|sentry|fullstory|segment|"
    r"facebook\.|fbcdn|cloudflareinsights|bing\.|clarity\.ms|gstatic\.com|"
    r"googleads|optimizely|mixpanel|amplitude|datadog|newrelic)",
    re.IGNORECASE,
)


async def capture(slug: str, url: str) -> None:
    bodies_dir = OUT_DIR / f"{slug}_bodies"
    bodies_dir.mkdir(exist_ok=True)
    captured: list[dict] = []
    body_counter = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        pending: dict[str, dict] = {}

        def on_request(request) -> None:
            if request.resource_type not in INTERESTING_TYPES:
                return
            if NOISE_HOSTS.search(urlparse(request.url).netloc):
                return
            pending[request.url] = {
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            }

        async def on_response(response) -> None:
            nonlocal body_counter
            req = pending.get(response.url)
            if req is None:
                return
            req["status"] = response.status
            req["response_headers"] = dict(response.headers)
            ct = response.headers.get("content-type", "")
            req["content_type"] = ct
            if "json" in ct.lower():
                try:
                    body = await response.json()
                    body_counter += 1
                    body_path = bodies_dir / f"{body_counter:02d}.json"
                    body_path.write_text(json.dumps(body, indent=2))
                    req["body_file"] = body_path.name
                    req["body_preview"] = json.dumps(body)[:600]
                except Exception as e:
                    req["body_error"] = str(e)
            captured.append(req)

        page.on("request", on_request)
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"[{slug}] Loading {url}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
        except Exception as e:
            print(f"[{slug}] navigation: {e}")
        await page.wait_for_timeout(3_000)
        await page.screenshot(path=str(OUT_DIR / f"{slug}_screenshot.png"), full_page=True)
        await browser.close()

    _write_summary(slug, captured)


def _write_summary(slug: str, captured: list[dict]) -> None:
    jsonl = OUT_DIR / f"{slug}_requests.jsonl"
    with jsonl.open("w") as f:
        for c in captured:
            f.write(json.dumps(c) + "\n")

    api_calls = [c for c in captured if "json" in c.get("content_type", "").lower()]
    other = [c for c in captured if c not in api_calls]

    lines: list[str] = []
    lines.append(f"slug: {slug}")
    lines.append(f"total interesting requests: {len(captured)}")
    lines.append(f"  JSON: {len(api_calls)}    other: {len(other)}")
    lines.append("")

    # Group by host so platform pattern is obvious.
    hosts: dict[str, int] = {}
    for c in api_calls:
        h = urlparse(c["url"]).netloc
        hosts[h] = hosts.get(h, 0) + 1
    lines.append("API hosts:")
    for h, n in sorted(hosts.items(), key=lambda x: -x[1]):
        lines.append(f"  {n:3d}  {h}")
    lines.append("")
    lines.append("=" * 80)
    lines.append("JSON CALLS")
    lines.append("=" * 80)
    for c in api_calls:
        lines.append("")
        lines.append(f"{c['method']} {c['url']}")
        lines.append(f"  status: {c.get('status')}  body_file: {c.get('body_file', '-')}")
        if c.get("post_data"):
            lines.append(f"  post_data: {c['post_data'][:300]}")
        auth = c["headers"].get("authorization") or c["headers"].get("Authorization")
        if auth:
            lines.append(f"  authorization: {auth[:80]}...")
        cookie = c["headers"].get("cookie") or c["headers"].get("Cookie")
        if cookie:
            lines.append(f"  cookie: present ({len(cookie)} chars)")
        if c.get("body_preview"):
            lines.append(f"  preview: {c['body_preview'][:300]}")

    lines.append("")
    lines.append("=" * 80)
    lines.append("OTHER")
    lines.append("=" * 80)
    for c in other:
        lines.append(f"{c['method']} {c.get('status', '?'):>3} {c['url']}")

    summary = OUT_DIR / f"{slug}_summary.txt"
    summary.write_text("\n".join(lines))
    print(f"[{slug}] wrote {summary.name}, {jsonl.name}, {len(api_calls)} body files")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: capture.py <slug> <url>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(capture(sys.argv[1], sys.argv[2]))
