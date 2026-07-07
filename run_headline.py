"""
run_headline.py — generate a headline + severity for each image.

Usage (run locally on VPN):
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_headline.py ~/Downloads/testsheet.csv --limit 20
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).parent))
from image_tester import LITELLM_PROXY_URL, check_proxy_connectivity, image_content_from_url

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

────────────────────────────────
STEP 1 — IS THERE AN ACTIVE EMERGENCY SCENE?
────────────────────────────────
First, decide: is any emergency vehicle clearly stationary at or responding to an active emergency scene?

Output "No incident visible" ONLY if ALL of the following are true:
- No emergency vehicles are visible, OR
- Any visible emergency vehicles are moving through traffic, passing an intersection, or stopped at a traffic signal with no scene around them (no stopped civilian vehicle on the verge, no damage, no workers, no crowd, no debris)

If an emergency vehicle IS stationary at or responding to a scene with any visible activity → proceed to Step 2.
Do NOT output "No incident visible" just because the incident type is unclear — use "unknown incident" instead.

────────────────────────────────
STEP 2 — WHICH INCIDENT TYPE?
────────────────────────────────
Choose the BEST matching type:

- "crash" — a road traffic collision or vehicle accident is visible. Use your judgement: if the scene looks like a crash, use this.
- "fire" — flames or heavy smoke are visible
- "pulled-over vehicle" — a police vehicle stationary on the hard shoulder or verge, positioned directly behind or beside a stopped civilian vehicle, with no crash damage visible. NOT a police car at an intersection, stopped at traffic lights, or alongside vehicles in a lane of moving traffic.
- "blocked road" — a lane is physically blocked by cones or barriers (no crash damage visible). Use format: "Road blocked as [vehicle phrase lowercase] responds to emergency"
- "construction" — any active work zone: construction or utility machinery present (excavators, pavers, rollers, bucket/cherry picker trucks, aerial platform vehicles, cranes, tree work vehicles); workers in hi-vis vests on or beside the road; road work signs with active digging/resurfacing; OR a prominent layout of traffic cones or barriers delineating a work zone with workers or vehicles present
- "crowd" — a visible group of civilians gathered in or near the roadway
- "unknown incident" — emergency vehicles are at a scene with visible activity, but the incident does not clearly match any category above. This is the DEFAULT when a scene is present but the type is uncertain. Use: [VEHICLE_PHRASE] detected responding to unknown incident

Never say "accident" or "collision" — use "crash".
Never output "No incident visible" here — you already confirmed a scene exists in Step 1.

The ONLY valid Line 1 outputs are:
  [VEHICLE_PHRASE] detected responding to crash
  [VEHICLE_PHRASE] detected responding to fire
  [VEHICLE_PHRASE] detected responding to pulled-over vehicle
  [VEHICLE_PHRASE] detected responding to construction
  [VEHICLE_PHRASE] detected responding to crowd
  [VEHICLE_PHRASE] detected responding to unknown incident
  Road blocked as [vehicle phrase lowercase] responds to emergency
  No incident visible

Do NOT mention location, time of day, weather, road names, or road type.
Do NOT use subjective descriptions (e.g. "major", "serious", "quiet", "busy").
Do NOT include vehicle counts as numbers.
Do NOT use "incidents" (plural) — always use "incident" (singular).

────────────────────────────────
LINE 2 — SEVERITY
────────────────────────────────
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

Another example:
Emergency vehicles detected responding to unknown incident
general.alert3

Another example:
Road blocked as police vehicle responds to emergency
general.alert3

Another example (no active scene):
No incident visible
general.alert3"""

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}


def parse_response(text):
    """Split two-line model output into (headline, severity)."""
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    headline = ""
    severity = ""
    for line in lines:
        if line in VALID_SEVERITIES:
            severity = line
        elif not headline:
            headline = line
    return headline, severity


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--model", default="anthropic/claude-sonnet-4-6")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--url-col", type=int, default=0)
    ap.add_argument("--output", default=None)
    ap.add_argument("--proxy-url", default=LITELLM_PROXY_URL)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN")

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)
    check_proxy_connectivity(client, args.model)

    records = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for i, row in enumerate(reader, start=1):
            if len(row) <= args.url_col:
                continue
            url = row[args.url_col].strip()
            if not url or not url.startswith("http"):
                continue
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
                raw = (resp.choices[0].message.content or "").strip()
                headline, severity = parse_response(raw)

                print(f"ok — {headline} | {severity}")
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
