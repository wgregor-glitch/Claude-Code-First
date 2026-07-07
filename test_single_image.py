"""
Test a single image URL against the headline prompt.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 test_single_image.py "https://..."
"""

import base64
import os
import sys
import urllib.request

try:
    import openai
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "openai", "-q"])
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
- If NO emergency vehicles are visible → output "No incident visible" (skip the detected responding format)
- "No incident visible" is for emergency vehicles that are driving past, passing through traffic, or stopped at a traffic signal or intersection without emergency-specific scene activity (no stopped civilian vehicle on the verge, no damage, no workers, no crowd). e.g. a police car at a red light among normal traffic; two police cars at a busy intersection with traffic flowing; an ambulance driving in a traffic lane with no scene around it; a single police car passing through an empty intersection.

Before assigning any incident type, ask: is the emergency vehicle stationary at a recognisable emergency scene? If it is moving, passing through traffic, or stopped at a traffic signal or intersection with no emergency-specific activity — output "No incident visible" immediately. Only continue to the incident types below if the vehicle is clearly staged or stopped at a specific location with visible emergency scene activity.

When in doubt between an incident type and "No incident visible", always choose "No incident visible".

INCIDENT_PHRASE rules:
- "crash" — physical crash evidence MUST be visible: vehicle damage, deployed airbags, overturned vehicle, civilian vehicle on the grass/verge having left the roadway, or ambulance(s) with stretchers beside a civilian vehicle. Multiple emergency vehicles stopped together is NOT sufficient on its own — at least one piece of physical damage evidence is required. Do NOT use this for lights alone, or vehicles clustered at an intersection with no visible damage.
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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--model", default="openai/gpt-4o")
    ap.add_argument("--low-detail", action="store_true", help="Use low-detail image (85 tokens vs ~765)")
    args = ap.parse_args()

    url = args.url
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    detail = "low" if args.low_detail else "auto"
    print(f"Fetching image...", end=" ", flush=True)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    img = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}
    print("ok")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)
    print(f"Running {args.model} [{detail} detail]...", end=" ", flush=True)
    resp = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
        max_tokens=80,
        timeout=90,
    )
    print("ok\n")
    print(resp.choices[0].message.content)


if __name__ == "__main__":
    main()
