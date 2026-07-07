"""
Gemma headline + severity generator with Baseten wake-up support.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_headlines_gemma.py headlines_test2.csv --url-col 1 --output headlines_gemma.csv
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

try:
    import requests
except ImportError:
    print("Installing requests..."); os.system(f"{sys.executable} -m pip install requests -q")
    import requests

LITELLM_PROXY_URL = "https://llm-proxy.ai.use1.test.dmnr.io"
DEFAULT_MODEL = "baseten/gemma-4-E4B-it"

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
- If NO emergency vehicles are visible → output "No incident visible" (skip the detected responding format)
- "No incident visible" is for emergency vehicles that are driving past, passing through traffic, or stopped at a traffic signal or intersection without emergency-specific scene activity (no stopped civilian vehicle on the verge, no damage, no workers, no crowd). e.g. a police car at a red light among normal traffic; two police cars at a busy intersection with traffic flowing; an ambulance driving in a traffic lane with no scene around it; a single police car passing through an empty intersection.

Before assigning any incident type, ask: is the emergency vehicle stationary at a recognisable emergency scene? If it is moving, passing through traffic, or stopped at a traffic signal or intersection with no emergency-specific activity — output "No incident visible" immediately. Only continue to the incident types below if the vehicle is clearly staged or stopped at a specific location with visible emergency scene activity.

When in doubt between an incident type and "No incident visible", always choose "No incident visible".

INCIDENT_PHRASE rules:
- "crash" — can you see a road traffic collision or vehicle accident? Use your judgement: if the scene looks like a crash, use this.
- "fire" — flames or heavy smoke are visible
- "pulled-over vehicle" — a police vehicle stationary on the hard shoulder or verge, positioned directly behind or beside a stopped civilian vehicle, with no crash damage visible. NOT a police car at an intersection, stopped at traffic lights, or alongside vehicles in a lane of moving traffic.
- "blocked road" — a lane is physically blocked by cones, barriers, or wreckage. Use format: "Road blocked as [vehicle phrase lowercase] responds to emergency"
- "construction" — any active work zone: construction or utility machinery present (excavators, pavers, rollers, bucket/cherry picker trucks, aerial platform vehicles, cranes, tree work vehicles); workers in hi-vis vests on or beside the road; road work signs with active digging/resurfacing; OR a prominent layout of traffic cones or barriers delineating a work zone with workers or vehicles present
- "crowd" — a visible group of civilians gathered in or near the roadway
- "unknown incident" — emergency vehicles are clearly staged or stopped at a scene with visible activity, but the incident type does not fit any category above. Use: [VEHICLE_PHRASE] detected responding to unknown incident
- If none of the above fit AND the vehicle is clearly just driving/patrolling with no scene → output "No incident visible".
- Never say "accident" or "collision" — use "crash"

The ONLY valid Line 1 outputs are:
  [VEHICLE_PHRASE] detected responding to crash
  [VEHICLE_PHRASE] detected responding to fire
  [VEHICLE_PHRASE] detected responding to pulled-over vehicle
  [VEHICLE_PHRASE] detected responding to construction
  [VEHICLE_PHRASE] detected responding to crowd
  [VEHICLE_PHRASE] detected responding to unknown incident
  Road blocked as [vehicle phrase] responds to emergency
  No incident visible

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
general.alert3

Another example (no clear incident):
No incident visible
general.alert3"""

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}


def litellm_wake_and_wait_for_model(
    model_id: str,
    api_key: str = None,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 5,
    base_url: str = None,
) -> bool:
    if base_url is None:
        base_url = os.getenv("LLM_PROXY_BASE_URL", LITELLM_PROXY_URL)
    if api_key is None:
        api_key = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("LITELLM_API_KEY")
    assert model_id.startswith("baseten/"), "Only baseten models can be woken up"

    headers = {"Authorization": f"Bearer {api_key}"}

    response = requests.get(f"{base_url}/{model_id}/state", headers=headers, timeout=10)
    assert response.status_code == 200, f"Unable to get Baseten model state\nCode: {response.status_code} {response.text}"
    status = response.json()["status"]
    if status == "ACTIVE":
        print(f"Model {model_id} is already ACTIVE")
        return True

    print(f"Baseten model {model_id} status is {status}")

    if status not in ["SCALED_TO_ZERO", "WAKING_UP", "DEPLOYING"]:
        print(f"Unhandled Baseten deployment status: {status}")
        return False

    if status == "SCALED_TO_ZERO":
        print(f"...waking up model and waiting up to {timeout_seconds} seconds")
        response = requests.post(f"{base_url}/{model_id}/wake", headers=headers, timeout=10)
        assert response.status_code == 202, f"Unable to wake Baseten model.\nCode: {response.status_code} {response.text}"
    else:
        print(f"Model is waking up, waiting up to {timeout_seconds} seconds")

    elapsed_time = 0
    while elapsed_time < timeout_seconds:
        time.sleep(poll_interval_seconds)
        response = requests.get(f"{base_url}/{model_id}/state", headers=headers)
        deployment_info = response.json()
        print(f"  status: {deployment_info.get('status')}")
        if deployment_info.get("status") == "ACTIVE":
            print(f"...model ready after {elapsed_time}s")
            return True
        elapsed_time += poll_interval_seconds

    print(f"Model didn't wake up after {elapsed_time}s")
    return False


def image_content_from_url(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


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
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--output", default=None)
    ap.add_argument("--url-col", type=int, default=0)
    ap.add_argument("--skip-rows", type=int, default=1)
    ap.add_argument("--no-wake", action="store_true", help="Skip Baseten wake-up check")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    if not args.no_wake and args.model.startswith("baseten/"):
        print(f"Waking up {args.model}...")
        ready = litellm_wake_and_wait_for_model(args.model, api_key=api_key)
        if not ready:
            sys.exit(f"ERROR: model {args.model} did not become ready")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

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
    output_file = args.output or f"headlines_gemma_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

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
