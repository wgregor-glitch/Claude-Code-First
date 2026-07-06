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
- If NO emergency vehicles are visible → output "No emergency vehicles visible" (skip the detected responding format)
- If emergency vehicles ARE visible but NOT actively responding to any discernible incident (e.g. parked on roadside, driving past, no flashing lights or response activity evident) → output "No incident visible" (skip the detected responding format)

INCIDENT_PHRASE rules:
Only use the "detected responding to" format when the vehicle(s) are CLEARLY engaged in an active emergency response — lights on, positioned at a scene, or otherwise actively attending an incident. If response activity is not evident, use "No incident visible" instead.
- "crash" — use ONLY when road traffic collision indicators are present in the scene: recently occurring visible vehicle damage, deployed airbags, overturned vehicle, vehicle veered off road onto grass or verge, ambulance(s) stopped near vehicle or stretchers beside a car with ambulance(s) present. A police vehicle with lights on driving through an intersection or patrolling is NOT a crash.
- "fire" — flames or heavy smoke are visible
- "pulled-over vehicle" — police stopped behind a civilian vehicle on the shoulder
- "blocked road" — a lane is physically blocked by cones, barriers, or wreckage. When this applies, use a DIFFERENT format: "Road blocked as [vehicle phrase lowercase] responds to emergency" (e.g. "Road blocked as police vehicle responds to emergency", "Road blocked as fire truck responds to emergency")
- "construction" — any of: construction machinery (excavators, pavers, rollers, road graders) present or operating; workers in hi-vis vests actively working on the road surface; road work signs combined with visible digging, resurfacing, or lane reconfiguration
- "crowd" — a visible group of civilians gathered in or near the roadway (not uniformed emergency responders)
- If none of the above specific types apply → output "No incident visible".
- Never say "accident" or "collision" — use "crash"
- The word "incident" must NEVER appear in Line 1 under any circumstances.

The ONLY valid Line 1 outputs are:
  [VEHICLE_PHRASE] detected responding to crash
  [VEHICLE_PHRASE] detected responding to fire
  [VEHICLE_PHRASE] detected responding to pulled-over vehicle
  [VEHICLE_PHRASE] detected responding to construction
  [VEHICLE_PHRASE] detected responding to crowd
  Road blocked as [vehicle phrase] responds to emergency
  No emergency vehicles visible
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
    if len(sys.argv) < 2:
        sys.exit("Usage: python3 test_single_image.py \"https://...\"")

    url = sys.argv[1]
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    print(f"Fetching image...", end=" ", flush=True)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    img = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    print("ok")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)
    print("Running model...", end=" ", flush=True)
    resp = client.chat.completions.create(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
        max_tokens=80,
        timeout=90,
    )
    print("ok\n")
    print(resp.choices[0].message.content)


if __name__ == "__main__":
    main()
