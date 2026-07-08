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

────────────────────────────────
STEP 1 — IS THERE AN ACTIVE EMERGENCY SCENE?
────────────────────────────────
Scan the image for any of these scene indicators. If ANY ONE is present → proceed immediately to Step 2:

✓ A vehicle stopped on the shoulder, verge, or hard shoulder
✓ A vehicle parked diagonally or perpendicular to the normal direction of traffic (blocking lanes)
✓ People/pedestrians standing on foot near emergency vehicles or on the roadside
✓ An officer or worker standing in a traffic lane
✓ Debris, damage, or displaced objects on the road
✓ 2 or more emergency vehicle types visible together (e.g. police AND fire truck, police AND ambulance)
✓ Emergency vehicles clustered or stopped in an unusual pattern

Output "No incident visible" ONLY when NONE of the above are present AND all visible emergency vehicles appear to be moving normally through traffic or stopped at a red light with completely normal traffic flow around them.
If uncertain, proceed to Step 2. Never default to "No incident visible" under uncertainty.

────────────────────────────────
STEP 2 — WHICH INCIDENT TYPE?
────────────────────────────────
Choose the BEST matching type:

- "crash" — use this whenever ANY of the following are visible: a vehicle stopped in an unusual position (sideways, angled, off-road, or blocking a lane); visible vehicle damage; debris, glass, or vehicle parts on the road; skid marks; multiple vehicles clustered in traffic lanes with emergency response; police AND fire truck together at a scene (typical crash response). If none of these indicators are clearly visible, use "unknown incident" instead.
- "fire" — flames or heavy smoke are visible
- "pulled-over vehicle" — a police vehicle stationary on the hard shoulder or verge, positioned directly behind or beside a stopped civilian vehicle, with no crash damage visible. NOT a police car at an intersection, stopped at traffic lights, or alongside vehicles in a lane of moving traffic.
- "blocked road" — a road or lane is physically closed off by: cones or barriers; OR emergency/law enforcement vehicles parked diagonally or sideways across the road to block it; OR officers standing in the road directing traffic away from a closure. No crash damage visible.
  ⚠ BLOCKED ROAD USES A DIFFERENT FORMAT — do NOT use "detected responding to". Output MUST be:
  Road blocked as [vehicle phrase lowercase] responds to emergency
  e.g. "Road blocked as police vehicle responds to emergency"
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
First apply these overrides — they take priority over vehicle count:
- If Line 1 contains "crash" → always output general.alert2.local

Otherwise count ALL emergency vehicles visible in the image (police cars, fire trucks, ambulances — any type):
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
