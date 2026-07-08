"""
Run GPT-4o-mini predictions on 500testset.csv and write results back
with added columns: predicted_headline, predicted_severity, latency_ms, error.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_500_testset.py

Checkpoint: saves progress to run_500_checkpoint.csv after every row.
Re-running skips already-completed rows automatically.
"""

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
MODEL             = "openai/gpt-4o-mini"

INPUT_CSV      = None   # set via first positional argument
CHECKPOINT_CSV = "run_500_checkpoint.csv"
OUTPUT_CSV     = "run_500_results.csv"

HEADLINE_PROMPT = """Look at this traffic camera image and produce exactly two lines of output — nothing else.

Line 1: alert-style headline
Line 2: severity code

────────────────────────────────
LINE 1 — HEADLINE
────────────────────────────────
Format:
[VEHICLE_PHRASE] detected responding to [INCIDENT_PHRASE]

INCIDENT_PHRASE must be singular — NEVER write "incidents" (plural).
Maximum 15 words.

────────────────────────────────
VEHICLE IDENTIFICATION
────────────────────────────────

Identify each distinct emergency vehicle type visible.

Police:

* Marked patrol cars, police SUVs, highway patrol.
* Vehicles with Battenburg checker pattern (yellow/blue, yellow/green, or yellow/lime) and "POLICE" markings.
* Vehicles with visible police light bars.
* Exactly 1 → "Police vehicle"
* 2 or more → "Police vehicles"

Fire trucks:

* Fire engines, ladder trucks, heavy rescue vehicles.
* Typically red or lime-green/yellow with emergency markings.
* Exactly 1 → "Fire truck"
* 2 or more → "Fire trucks"

Ambulances:
Any of:

* Box-body EMS/paramedic unit.
* Vehicle with "AMBULANCE" text.
* Red cross or star-of-life markings.
* Yellow-green or white emergency vehicle with EMS/paramedic livery.
* UK emergency vehicle with yellow/green Battenburg checker pattern.
* Stretcher or medical equipment visible.
* Exactly 1 → "Ambulance"
* 2 or more → "Ambulances"

If type cannot be confidently identified:

* Exactly 1 → "Emergency vehicle"
* 2 or more → "Emergency vehicles"

Prefer specific types over "Emergency vehicle" whenever visual evidence supports classification.

If multiple types are present, list in this order:
Police, Fire truck, Ambulance, Emergency vehicle

Formatting:

* 2 types → "Police vehicle and Fire truck"
* 3+ types → "Police vehicle, Fire truck, and Ambulance"

Do not include vehicle counts as numbers.

────────────────────────────────
STEP 1 — IS THERE AN ACTIVE EMERGENCY SCENE?
────────────────────────────────

A scene exists if ANY of these are visible:

✓ Vehicle stopped on shoulder, verge, hard shoulder, or unusual location
✓ Vehicle stopped diagonally, sideways, perpendicular, overturned, or blocking traffic
✓ People standing on foot near emergency vehicles or roadside activity
✓ Officer or worker standing in a traffic lane
✓ Debris, broken vehicle parts, glass, skid marks, flares, or emergency triangles
✓ Emergency vehicles clustered or positioned unusually
✓ Road closure activity, cones, barriers, or traffic control activity
✓ Visible emergency response activity around a vehicle or location

If none of these are present and emergency vehicles appear to be:

* moving normally,
* waiting at a normal traffic control point,
* or simply travelling through traffic,

output:

No incident visible

Do not assume an incident only because an emergency vehicle is visible.

────────────────────────────────
STEP 2 — INCIDENT CLASSIFICATION
────────────────────────────────

Choose the BEST matching type.

CRASH:
Use "crash" ONLY when there is evidence supporting a vehicle collision or crash response.

Strong crash evidence includes:

* Visible vehicle damage or crumple zones.
* Vehicle on its side or roof.
* Vehicle in a clearly abnormal position caused by impact.
* Debris, glass, vehicle parts, skid marks, deployed airbags, flares, or emergency triangles.
* Damaged vehicle being attended by emergency responders.

Supporting crash evidence:

* Ambulance with Police vehicle at an active vehicle scene.
* Ambulance with Fire truck at an active vehicle scene.
* Police, Fire truck, and Ambulance together at an active vehicle scene.

Do NOT classify as crash based only on:

* Police vehicle and Fire truck together.
* Emergency vehicles parked normally.
* A stopped vehicle without visible damage or unusual positioning.
* Traffic congestion near emergency vehicles.
* A police response where the reason is unclear.

When crash evidence is weak but responders are clearly handling an active situation:
use "unknown incident".

FIRE:
Use only when:

* Flames are visible.
* Heavy smoke is visible.

PULLED-OVER VEHICLE:
Use when:

* Police vehicle is stationary on the hard shoulder or verge.
* Police vehicle is directly behind or beside a stopped civilian vehicle.
* No crash damage is visible.

Do NOT use for:

* Police vehicles at intersections.
* Police vehicles stopped in normal traffic lanes.
* Police vehicles travelling with traffic.

BLOCKED ROAD:
Use when:

* Lane or roadway is physically closed by cones, barriers, emergency vehicles positioned to block passage, or officers directing traffic away from closure.
* No clear crash evidence is visible.

Required format:
Road blocked as [vehicle phrase lowercase] responds to emergency

CONSTRUCTION:
Use when:

* Active work vehicles are present.
* Workers in hi-vis are active near roadway.
* Road works signs with active digging/resurfacing are visible.
* A clear work zone with cones/barriers and workers or machinery is present.

CROWD:
Use when:

* A visible group of civilians is gathered in or near the roadway.

UNKNOWN INCIDENT:
Use when:

* Emergency vehicles are present at an active scene.
* Activity is visible.
* The cause cannot be confidently classified.

Format:
[VEHICLE_PHRASE] detected responding to unknown incident

Never use:

* accident
* collision
* incidents (plural)

────────────────────────────────
LINE 2 — SEVERITY
────────────────────────────────

First apply this override:

If Line 1 contains "crash":
output:
general.alert2.local

Otherwise:

Count all visible emergency vehicles classified using the vehicle identification rules.

Output:

general.alert2.local

* 3 or more emergency vehicles visible

general.alert3

* 0, 1, or 2 emergency vehicles visible

When uncertain, choose:
general.alert3

────────────────────────────────
OUTPUT FORMAT
────────────────────────────────

Output exactly two lines.
No labels.
No explanations.
No blank lines.

Valid examples:

Police vehicles and Fire truck detected responding to crash
general.alert2.local

Police vehicle detected responding to pulled-over vehicle
general.alert3

Emergency vehicles detected responding to unknown incident
general.alert3

Road blocked as police vehicle responds to emergency
general.alert3

No incident visible
general.alert3"""

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}

IN_FIELDS  = ["", "Alert Created Date", "Caption", "Document ID", "Source Media URL", "Internal Alert Threshold"]
OUT_FIELDS = IN_FIELDS + ["predicted_headline", "predicted_severity", "latency_ms", "error"]


def image_content_from_url(url, detail="auto"):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def parse_response(text):
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    headline, severity = "", ""
    for line in lines:
        if line in VALID_SEVERITIES:
            severity = line
        elif not headline:
            headline = line
    return headline, severity


def load_checkpoint(checkpoint_csv):
    done = {}
    if os.path.exists(checkpoint_csv):
        with open(checkpoint_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get("error", "").strip():
                    done[row[""]] = row
        print(f"Resuming: {len(done)} rows already done")
    return done


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV", help="Path to 500testset.csv (or similar)")
    ap.add_argument("--output",     default="run_500_results.csv",    help="Output CSV path")
    ap.add_argument("--checkpoint", default="run_500_checkpoint.csv", help="Checkpoint CSV path")
    args = ap.parse_args()

    input_csv      = args.csv_path
    output_csv     = args.output
    checkpoint_csv = args.checkpoint

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    # Quick proxy check
    print(f"Checking proxy ({MODEL}) ...", end=" ", flush=True)
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=5, timeout=30,
        )
        print(f"ok ({(r.choices[0].message.content or '').strip()!r})")
    except Exception as e:
        sys.exit(f"FAILED: {e}")

    # Load input
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} rows from input")

    done = load_checkpoint(checkpoint_csv)

    # Open checkpoint for appending
    ckpt_is_new = not os.path.exists(checkpoint_csv)
    ckpt_f = open(checkpoint_csv, "a", newline="", encoding="utf-8")
    ckpt_w = csv.DictWriter(ckpt_f, fieldnames=OUT_FIELDS)
    if ckpt_is_new:
        ckpt_w.writeheader()

    todo = [r for r in rows if r[""] not in done]
    print(f"To process: {len(todo)}")

    errors = 0
    for idx, row in enumerate(todo, 1):
        row_id = row[""]
        url    = row["Source Media URL"].strip()
        print(f"  [{idx}/{len(todo)}] #{row_id} ...", end=" ", flush=True)

        out = dict(row)
        try:
            img = image_content_from_url(url)
            t0  = time.monotonic()
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                max_tokens=80,
                timeout=90,
            )
            latency = round((time.monotonic() - t0) * 1000)
            headline, severity = parse_response(resp.choices[0].message.content)
            out["predicted_headline"]  = headline
            out["predicted_severity"]  = severity
            out["latency_ms"]          = latency
            out["error"]               = ""
            print(f"{headline} | {severity} ({latency}ms)")
        except Exception as e:
            out["predicted_headline"]  = ""
            out["predicted_severity"]  = ""
            out["latency_ms"]          = ""
            out["error"]               = str(e)
            errors += 1
            print(f"ERROR: {e}")

        ckpt_w.writerow(out)
        ckpt_f.flush()

    ckpt_f.close()

    # Merge checkpoint + original order → final output
    results = {}
    with open(checkpoint_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            results[r[""]] = r

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for row in rows:
            rid = row[""]
            if rid in results:
                writer.writerow(results[rid])
            else:
                out = dict(row)
                out.update({"predicted_headline": "", "predicted_severity": "", "latency_ms": "", "error": "skipped"})
                writer.writerow(out)

    done_count = sum(1 for r in results.values() if not r.get("error","").strip())
    print(f"\nDone. {done_count}/500 successful, {errors} errors.")
    print(f"Output → {output_csv}")


if __name__ == "__main__":
    main()
