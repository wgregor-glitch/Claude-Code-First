"""
run_headline.py — generate a short alert-style headline (≤15 words) for each GT image.

Usage (run locally on VPN):
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_headline.py ~/Downloads/testsheet.csv --limit 20
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).parent))
from image_tester import LITELLM_PROXY_URL, check_proxy_connectivity, image_content_from_url

HEADLINE_PROMPT = """\
Look at this traffic camera image and write a single alert-style headline.

Format: [VEHICLE_PHRASE] detected responding to [INCIDENT_PHRASE]
INCIDENT_PHRASE must be singular — NEVER write "incidents" (plural).
Maximum 15 words. Output ONLY the headline, nothing else.

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

Examples:
  Police vehicle detected responding to incident
  Police vehicles detected responding to incident
  Fire truck detected responding to fire
  Ambulance detected responding to crash
  Police vehicle and Fire truck detected responding to crash
  Police vehicles and Ambulance detected responding to crash
  Fire truck and Ambulance detected responding to crash
  Road blocked as police vehicle responds to incident
  Road blocked as police vehicles respond to incident
  Police vehicle detected responding to pulled-over vehicle
  Police vehicle detected responding to construction
  Police vehicles detected responding to crowd
  No emergency vehicles visible"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--model", default="anthropic/claude-sonnet-4-6")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--url-col", type=int, default=0)
    ap.add_argument("--output", default=None)
    ap.add_argument("--proxy-url", default=LITELLM_PROXY_URL)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN")

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)
    check_proxy_connectivity(client, args.model)

    # Read GT rows (rows with URLs)
    records = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)  # skip merged header
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
        writer = csv.DictWriter(outf, fieldnames=["row", "url", "headline", "latency_ms", "error"])
        writer.writeheader()

        for idx, rec in enumerate(records, start=1):
            print(f"  [{idx}/{len(records)}] row {rec['row']} ...", end=" ", flush=True)
            try:
                img = image_content_from_url(rec["url"])
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                    max_tokens=60,
                    timeout=90,
                )
                latency = round((time.monotonic() - t0) * 1000)
                headline = (resp.choices[0].message.content or "").strip()
                headline = re.sub(r'\bincidents\b', 'incident', headline, flags=re.IGNORECASE)
                # Reformat "X detected responding to blocked road" → "Road blocked as x responds to incident"
                headline = re.sub(
                    r'^(.*?)\s+detected responding to blocked road$',
                    lambda m: f"Road blocked as {m.group(1).lower()} responds to incident",
                    headline, flags=re.IGNORECASE
                )
                # Normalise "A and B and C" → "A, B, and C"
                headline = re.sub(
                    r'((?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?) and (?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?)) and ((?:Police vehicles?|Fire trucks?|Ambulances?|Emergency vehicles?))',
                    r'\1, and \2', headline
                )
                print(f"ok — {headline}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": headline, "latency_ms": latency, "error": ""})
            except Exception as exc:
                print(f"ERROR: {exc}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": "", "latency_ms": 0, "error": str(exc)})
            outf.flush()

    print(f"\nDone. Upload {output_file} back to review.")


if __name__ == "__main__":
    main()
