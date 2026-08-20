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
Both outcomes are common in this camera stream: many images show a real emergency scene, and many show only routine traffic — including routine police presence. There is NO default answer. Decide purely from visible evidence, using the two gates below. "No incident visible" is a correct, expected answer whenever the evidence is not there — it is never a failure. Equally, do not suppress a real scene that passes both gates.

GATE 1: Is at least one emergency vehicle (police, fire truck, ambulance) clearly visible?
- If NO emergency vehicle is visible → output "No incident visible". Parked civilian cars, pedestrians, cyclists, buses, taxis, delivery vans, or heavy traffic alone are NOT an emergency scene. Do not guess an emergency vehicle into existence.

GATE 2: An emergency vehicle being present is NOT enough. Police vehicles routinely drive, park, wait, and patrol without any incident — a police vehicle with nothing happening around it is NOT an incident. To pass this gate there must be corroborating evidence in the image of one of the incident types defined in Step 2 (fire, pulled-over vehicle, blocked road, construction, crowd, crash), OR clear signs of an active emergency response (2 or more emergency vehicles stopped together; a responder on foot in the roadway; traffic stopped or diverting around the scene).

The evidence must be part of the SAME scene as the emergency vehicle. Cones, stopped cars, or lights elsewhere in the frame that are not connected to the emergency vehicle do NOT count.

NOT an incident — output "No incident visible":
✗ Emergency vehicle driving along with the normal flow of traffic
✗ Emergency vehicle stopped at a red light or intersection with normal traffic around it
✗ Emergency vehicle waiting behind another car in a turn lane or queue — that is traffic, not a traffic stop
✗ A single emergency vehicle parked or stopped on its own — roadside, shoulder, or parking area — with traffic flowing normally and no person, civilian vehicle, or object involved
✗ Cones or barrels along a curb, corner, or sidewalk while the road itself is open and traffic flows
✗ Emergency lights visible in the distance while traffic moves normally through the frame

FINAL CHECK: If every vehicle in the image appears to be moving with traffic — nothing stopped in or beside the roadway, no one on foot — output "No incident visible", even if an emergency vehicle is among the moving traffic.

DEFAULT UNDER UNCERTAINTY: If you cannot point to specific evidence of an incident type or an active response, output "No incident visible". An emergency vehicle alone is never enough.

────────────────────────────────
STEP 2 — WHICH INCIDENT TYPE?
────────────────────────────────
Work through these in ORDER. Use the FIRST category that clearly matches.

- "fire" — flames or heavy smoke are visible anywhere in the scene.

- "pulled-over vehicle" — a police vehicle stopped on the hard shoulder or verge, positioned directly behind or beside a stopped civilian vehicle, with no crash damage visible anywhere in the scene. Apply this even when the scene is small or distant in the frame — if there is no debris or damage, this is pulled-over, not crash.
  NOT a police car at an intersection, stopped at traffic lights, or alongside vehicles in a lane of moving traffic.

- "blocked road" — a road or lane is physically closed off by: cones or barriers; OR emergency vehicles parked diagonally or sideways across lanes to block traffic; OR officers standing in the road directing traffic away from a closure. No crash damage visible.
  ⚠ BLOCKED ROAD USES A DIFFERENT FORMAT — do NOT use "detected responding to". Output MUST be:
  Road blocked as [vehicle phrase lowercase] responds to emergency
  e.g. "Road blocked as police vehicle responds to emergency"

- "construction" — an active work zone with clear physical evidence of roadwork. Requires at least ONE of:
  □ Construction or utility machinery present (excavators, pavers, rollers, bucket/cherry picker trucks, aerial platform vehicles, cranes, tree work vehicles)
  □ Utility or highway maintenance vehicles (not police/fire/ambulance) actively working on or beside the road
  □ Explicit road work signage AND visible active digging, resurfacing, or lane modification in progress
  ⚠ Police, fire, or ambulance vehicles alone — even with cones behind them or workers in hi-vis — are NOT construction. If only emergency vehicles are present, use pulled-over, blocked road, or unknown incident instead.
  ⚠ Traffic cones or barriers alone do NOT indicate construction — they also appear at pulled-over and blocked-road scenes.

- "crowd" — a sizeable group of civilians (5 or more people) gathered in or near the roadway as the dominant feature of the scene.
  ⚠ Emergency responders (police officers, firefighters, paramedics) standing near vehicles do NOT count as a crowd.
  ⚠ A few bystanders near an emergency scene is NOT a crowd — use the relevant incident type instead.

- "crash" — only reach this after the above categories do not clearly match. Then look for physical evidence:
  □ A vehicle with visible damage (crushed metal, deployed airbag, shattered glass on the vehicle body)
  □ A vehicle stopped in an abnormal position (sideways across a lane, partially off-road, or facing the wrong direction)
  □ Debris, glass, or vehicle parts scattered on the road surface
  □ Skid marks leading to a stopped vehicle
  If you can clearly see at least ONE of the above → output crash.
  If you cannot → move on to unknown incident.
  ⚠ Tow trucks do NOT confirm a crash — only use crash if a tow truck is present AND physical evidence above is visible.
  ⚠ Traffic slowing or backing up behind police is NOT crash evidence.
  ⚠ Multiple police vehicles do NOT indicate crash — physical evidence is required.

- "unknown incident" — an active scene is present but none of the categories above clearly match. This is the correct answer when the scene is ambiguous — use it freely. It is not a weak answer.
  Use: [VEHICLE_PHRASE] detected responding to unknown incident

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
general.alert2.local is RARE. It applies to a small minority of crash scenes only — nothing else.

Step A — look at the incident phrase you wrote at the end of Line 1:
  Is it exactly "crash"?
    NO  → Line 2 is general.alert3. STOP HERE. Do not look at vehicle count.
          This covers pulled-over vehicle, blocked road, construction, crowd,
          unknown incident, and no incident visible — ALL OF THEM are always
          general.alert3, no matter how many emergency vehicles are visible,
          no matter how many types, no matter how severe the scene looks.
          A blocked road with five police vehicles is still general.alert3.
          An unknown incident with a fire truck and an ambulance is still
          general.alert3. Vehicle count is IRRELEVANT unless Line 1 says crash.
    YES → go to Step B.

Step B (crash only) — recount the total emergency vehicles actually visible
in the image (do not infer this from singular/plural wording in Line 1,
which only distinguishes 1 vs 2+ and is not precise enough here):
    3 or more emergency vehicles visible → general.alert2.local
    2 or fewer emergency vehicles visible → general.alert3

NEVER leave Line 2 blank or omit it. It must be EXACTLY the severity code and nothing else — no period, no explanation, no vehicle count, no extra words.
If you are unsure for any reason, worst case output general.alert3 — but ALWAYS output a severity code.

────────────────────────────────
OUTPUT FORMAT (exactly two lines, no labels, no blank lines):
Police vehicles, Fire truck, and Ambulance detected responding to crash
general.alert2.local

Another example (crash, but only 2 vehicles — not severe under this rule):
Police vehicle and Fire truck detected responding to crash
general.alert3

Another example (NOT crash — always alert3, regardless of vehicle count):
Police vehicle detected responding to pulled-over vehicle
general.alert3

Another example (NOT crash — always alert3, even with multiple vehicle types):
Police vehicles and Fire truck detected responding to unknown incident
general.alert3

Another example (NOT crash — always alert3, even with several vehicles):
Road blocked as police vehicles respond to emergency
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
    ap.add_argument("--max-tokens", type=int, default=80, help="Raise this to see full raw output for verbose/reasoning models")
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
        max_tokens=args.max_tokens,
        timeout=90,
    )
    print("ok\n")
    print(resp.choices[0].message.content)
    print(f"\n[finish_reason={resp.choices[0].finish_reason}]")


if __name__ == "__main__":
    main()
