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
import re
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
Format: [VEHICLE_PHRASE] detected responding to [INCIDENT_PHRASE]
INCIDENT_PHRASE must be singular — NEVER write "incidents" (plural).
Maximum 15 words.

VEHICLE_PHRASE rules (no numbers — use singular/plural only):
- Identify each distinct emergency vehicle type visible. For each type:
  - Police: marked patrol cars, police SUVs, highway patrol; vehicles with Battenburg checker pattern (yellow/blue, yellow/green, or yellow/lime) and "POLICE" markings or visible light bars — exactly 1 → "Police vehicle", 2 or more → "Police vehicles"
  - Fire trucks: fire engines, ladder trucks, heavy rescue — typically red or lime-green/yellow with emergency markings — exactly 1 → "Fire truck", 2 or more → "Fire trucks"
  - Ambulances: any of — box-body EMS/paramedic unit; vehicle with "AMBULANCE" text (often mirrored on hood); red cross or star-of-life markings; yellow-green or white emergency vehicle with EMS/paramedic livery; UK vehicles with yellow/green Battenburg checker pattern; vehicle with stretcher or medical equipment visible — exactly 1 → "Ambulance", 2 or more → "Ambulances"
  - Type unclear after applying the above: exactly 1 → "Emergency vehicle", 2 or more → "Emergency vehicles"
- Prefer a specific type over "Emergency vehicle" whenever features are partially visible; only use "Emergency vehicle" when you truly cannot distinguish.
- If multiple types are present, list them in order (Police, Fire truck, Ambulance, Emergency vehicle):
  - 2 types → join with "and": "Police vehicle and Fire truck"
  - 3+ types → use Oxford comma: "Police vehicle, Fire truck, and Ambulance"
- If NO emergency vehicles are visible → output "No emergency vehicles visible" (skip the detected responding format)

INCIDENT_PHRASE rules:
- "crash" — use when crash indicators are present: visible vehicle damage, deployed airbags, ambulance on scene, or multiple emergency vehicle types together
- "fire" — flames or heavy smoke are visible
- "pulled-over vehicle" — police stopped behind a civilian vehicle on the shoulder
- "blocked road" — a lane is physically blocked by cones, barriers, or wreckage. When this applies, use a DIFFERENT format: "Road blocked as [vehicle phrase lowercase] responds to incident" (e.g. "Road blocked as police vehicle responds to incident", "Road blocked as fire truck responds to incident")
- "construction" — any of: construction machinery (excavators, pavers, rollers, road graders) present or operating; workers in hi-vis vests actively working on the road surface; road work signs combined with visible digging, resurfacing, or lane reconfiguration
- "crowd" — a visible group of civilians gathered in or near the roadway (not uniformed emergency responders)
- "incident" — emergency response is clearly active but the specific type is unclear or does not fit the above categories. Always use the singular "incident", never "incidents"
- Never say "accident", "collision", or "incident" when a crash is indicated — use "crash"

Do NOT mention location, time of day, weather, road names, or road type.
Do NOT use subjective descriptions (e.g. "major", "serious", "quiet", "busy").
Do NOT include vehicle counts as numbers.
Do NOT use "incidents" (plural) — always use "incident" (singular).

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
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)
    check_proxy(client, args.model)

    records = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for i, row in enumerate(reader, start=1):
            if not row:
                continue
            url = row[0].strip()
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

                headline = re.sub(r'\bincidents\b', 'incident', headline, flags=re.IGNORECASE)
                headline = re.sub(
                    r'^(.*?)\s+detected responding to blocked road$',
                    lambda m: f"Road blocked as {m.group(1).lower()} responds to incident",
                    headline, flags=re.IGNORECASE
                )
                headline = re.sub(
                    r'((?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?) and (?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?)) and ((?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?))',
                    r'\1, and \2', headline
                )

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
