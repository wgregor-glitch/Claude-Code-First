"""
Standalone headline + severity generator — no other files needed.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_headlines_standalone.py threshold_text.csv
"""

import argparse
import base64
import csv
import os
import sys
import time
import urllib.request
from datetime import datetime

try:
    import openai
except ImportError:
    print("Installing openai..."); os.system(f"{sys.executable} -m pip install openai -q")
    import openai

LITELLM_PROXY_URL = "https://llm-proxy.ai.use1.test.dmnr.io"

HEADLINE_PROMPT = """\
Look at this traffic camera image and produce exactly two lines of output — nothing else.

Line 1: alert-style headline
Line 2: severity code

────────────────────────────────
LINE 1 — HEADLINE
────────────────────────────────
Follow these steps in order. Stop as soon as a step gives an output.

STEP 1 — Are any emergency vehicles visible?
  No → output exactly: No emergency vehicles visible

STEP 2 — Are the vehicle(s) clearly stopped at or attending an active scene
          (positioned at wreckage, stopped behind a pulled-over car, blocking
          a lane, attending a casualty, fighting a fire)?
  No — vehicle is just driving past, patrolling, or no clear scene activity → output exactly: No incident visible

STEP 3 — Identify the incident type from this list (pick the first that fits):
  - crash        — physical crash evidence: visible vehicle damage, deployed airbags, overturned vehicle, debris on road, or ambulance attending casualties. Lights on alone is NOT a crash.
  - fire         — flames or heavy smoke visible
  - pulled-over vehicle — police stopped behind a civilian vehicle on the shoulder
  - blocked road — a lane physically blocked by cones, barriers, or wreckage → use format: "Road blocked as [vehicle phrase lowercase] responds to emergency"
  - construction — construction machinery operating, workers in hi-vis on road surface, or road work signs with active digging/resurfacing
  - crowd        — visible group of civilians gathered in or near the roadway

  None fit → output exactly: No incident visible

STEP 4 — Build the headline:
  Format: [VEHICLE_PHRASE] detected responding to [INCIDENT]
  Maximum 15 words. No numbers — use singular/plural only.

  VEHICLE_PHRASE:
  - Police vehicle / Police vehicles (2+)
  - Fire truck / Fire trucks (2+)
  - Ambulance / Ambulances (2+)
  - Emergency vehicle / Emergency vehicles (2+) — only when type truly cannot be distinguished
  - Multiple types: list in order Police → Fire truck → Ambulance → Emergency vehicle
    - 2 types: join with "and" — "Police vehicle and Fire truck"
    - 3+ types: Oxford comma — "Police vehicle, Fire truck, and Ambulance"

Do NOT mention location, time of day, weather, road names, or road type.
Do NOT use subjective descriptions (e.g. "major", "serious", "quiet", "busy").
Do NOT include vehicle counts as numbers.

────────────────────────────────
LINE 2 — SEVERITY
────────────────────────────────
Output exactly one of these two codes:

Count ALL emergency vehicles visible in the image (police cars, fire trucks, ambulances — any type).

general.alert2.local  — 3 or more emergency vehicles visible
general.alert3        — 0, 1, or 2 emergency vehicles visible

When in doubt, choose general.alert3.

────────────────────────────────
OUTPUT FORMAT (exactly two lines, no labels, no blank lines):
Police vehicles and Fire truck detected responding to crash
general.alert2.local

Another example:
Police vehicle detected responding to pulled-over vehicle
general.alert3

Another example (no clear incident):
No incident visible
general.alert3"""

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}


def image_content_from_url(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def check_proxy(client, model):
    print(f"Checking proxy ({model}) ...", end=" ", flush=True)
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=5,
            timeout=30,
        )
        print(f"ok ({(r.choices[0].message.content or '').strip()!r})")
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)


def parse_response(text):
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    headline, severity = "", ""
    for line in lines:
        if line in VALID_SEVERITIES:
            severity = line
        elif not headline:
            headline = line
    return headline, severity


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--model", default="openai/gpt-4o")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--output", default=None)
    ap.add_argument("--url-col", type=int, default=0, help="0-based column index for the URL")
    ap.add_argument("--skip-rows", type=int, default=1, help="Header rows to skip (default 1)")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)
    check_proxy(client, args.model)

    records = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for _ in range(args.skip_rows):
            next(reader)
        for i, row in enumerate(reader, start=1):
            if len(row) <= args.url_col:
                continue
            url = row[args.url_col].strip()
            if url.startswith("http"):
                records.append({"row": i, "url": url})
                if args.limit and len(records) >= args.limit:
                    break

    print(f"Images: {len(records)}")
    output_file = args.output or f"headlines_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as outf:
        writer = csv.DictWriter(outf, fieldnames=["row", "url", "headline", "severity", "latency_ms", "error"])
        writer.writeheader()

        for idx, rec in enumerate(records, start=1):
            print(f"  [{idx}/{len(records)}] row {rec['row']} ...", end=" ", flush=True)
            try:
                img = image_content_from_url(rec["url"])
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                    max_tokens=80,
                    timeout=90,
                )
                latency = round((time.monotonic() - t0) * 1000)
                headline, severity = parse_response(resp.choices[0].message.content)

                print(f"{headline} | {severity}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": headline, "severity": severity,
                                 "latency_ms": latency, "error": ""})
            except Exception as exc:
                print(f"ERROR: {exc}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": "", "severity": "", "latency_ms": 0, "error": str(exc)})
            outf.flush()

    print(f"\nDone → {output_file}")


if __name__ == "__main__":
    main()
