"""
Stage-1 alertability agent: binary alertable / not-alertable per image.

Runs a dedicated binary prompt (no headline, no severity) so the model's
only decision is whether an active emergency scene is present.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_alertability.py newdataset.csv                       # all models
    python3 run_alertability.py newdataset.csv --only gpt4o_mini_hd
    python3 run_alertability.py newdataset.csv --workers 3

Checkpoint: run_alert_ckpt_<key>.csv per model (resume-safe).
Output: <input>_alertability.csv with <model>_alertable columns.
"""

import argparse, base64, csv, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

try:
    import openai
except ImportError:
    os.system(f"{sys.executable} -m pip install openai -q")
    import openai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_single_image import LITELLM_PROXY_URL

MODELS = [
    ("gpt4o_mini_hd", "openai/gpt-4o-mini",                      "high"),
    ("gpt4o_hd",      "openai/gpt-4o",                           "high"),
    ("gemini_flash",  "databricks/databricks-gemini-2-5-flash",  "auto"),
]

ALERTABILITY_PROMPT = """\
Look at this traffic camera image. Your ONLY job is to decide one thing:
is there an ACTIVE emergency scene, or is this routine traffic?

Output exactly ONE word:
alertable      — an active emergency scene is visible
not-alertable  — routine traffic, nothing actionable happening

Both answers are common in this camera stream. Decide purely from visible
evidence.

An ACTIVE emergency scene requires BOTH of:
1. At least one emergency vehicle (police, fire truck, ambulance) clearly
   visible. Civilian cars, buses, taxis, pedestrians, or cyclists alone are
   never an emergency scene.
2. Evidence that the emergency vehicle is engaged with something real:
   - stopped directly behind or beside a stopped civilian vehicle
   - stopped with emergency lights flashing — lights on a STOPPED vehicle
     always count, even when the scene is small, distant, or on the far
     side of the road
   - parked diagonally / blocking a lane, with traffic stopped or diverting
   - 2 or more emergency vehicles stopped together at the same spot
   - a responder on foot in the roadway
   - debris, vehicle damage, or a vehicle in an abnormal position
   - flames or smoke
   - a road or lane closed off by cones/barriers WITH an emergency vehicle
     or responders at the closure

Scan the WHOLE frame including the far distance, opposite carriageway,
shoulders, and edges — emergency scenes are often small in traffic camera
images.

ROUTINE (not-alertable) — even when an emergency vehicle is visible:
- emergency vehicle DRIVING along with traffic, or waiting at a light,
  intersection, or in a turn-lane queue behind other cars
- a single emergency vehicle parked on its own with NO flashing lights,
  traffic flowing normally, and no person, civilian vehicle, or object
  involved
- cones or barrels along a curb or sidewalk while the road itself is open
- emergency lights MOVING with the flow of traffic
- every vehicle in the image moving normally with the flow of traffic

DEFAULTS:
- Emergency vehicle clearly STOPPED but you are unsure whether it is
  engaged → alertable. A stopped emergency vehicle deserves review.
- Unsure whether any emergency vehicle is present at all, or it is moving
  with traffic → not-alertable.

Output exactly one word on one line: alertable OR not-alertable
No punctuation, no explanation."""


def image_content_from_url(url, detail="high"):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def parse_verdict(text):
    t = (text or "").strip().lower()
    # first token wins; tolerate stray formatting but nothing fancier
    for line in t.splitlines():
        line = line.strip().strip('*`."\'')
        if line in ("alertable",):
            return "alertable"
        if line in ("not-alertable", "not alertable", "not_alertable"):
            return "not-alertable"
    return ""


def process_row(row, model_key, model_id, detail, client):
    url = row["Source Media URL"].strip()
    try:
        img = image_content_from_url(url, detail)
        t0 = time.monotonic()
        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": [img, {"type": "text", "text": ALERTABILITY_PROMPT}]}],
            max_tokens=200,
            timeout=90,
        )
        latency = round((time.monotonic() - t0) * 1000)
        verdict = parse_verdict(resp.choices[0].message.content)
        return {
            f"{model_key}_alertable":  verdict,
            f"{model_key}_alt_latency_ms": latency,
            f"{model_key}_alt_error":  "" if verdict else f"unparsed: {resp.choices[0].message.content[:80]}",
        }
    except Exception as e:
        return {
            f"{model_key}_alertable":  "",
            f"{model_key}_alt_latency_ms": "",
            f"{model_key}_alt_error":  str(e),
        }


def run_model(all_rows, fieldnames, model_key, model_id, detail, client, workers):
    ckpt = f"run_alert_ckpt_{model_key}.csv"
    pred_fields = [f"{model_key}_alertable", f"{model_key}_alt_latency_ms", f"{model_key}_alt_error"]

    done = {}
    if os.path.exists(ckpt):
        with open(ckpt, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get(f"{model_key}_alt_error", "").strip():
                    done[row["_row_idx"]] = row
        if done:
            print(f"  Checkpoint: {len(done)} rows already done")

    todo = [r for r in all_rows if r["_row_idx"] not in done]
    print(f"  To process: {len(todo)}")
    if not todo:
        return

    ckpt_fields = ["_row_idx"] + list(fieldnames) + [p for p in pred_fields if p not in fieldnames]
    ckpt_is_new = not os.path.exists(ckpt)
    ckpt_lock = Lock()
    ckpt_f = open(ckpt, "a", newline="", encoding="utf-8")
    ckpt_w = csv.DictWriter(ckpt_f, fieldnames=ckpt_fields, extrasaction="ignore")
    if ckpt_is_new:
        ckpt_w.writeheader()

    complete = errors = 0
    total = len(todo)

    def task(row):
        return row, process_row(row, model_key, model_id, detail, client)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, row): row for row in todo}
        for fut in as_completed(futures):
            row, preds = fut.result()
            row_out = dict(row)
            row_out.update(preds)
            with ckpt_lock:
                complete += 1
                if preds.get(f"{model_key}_alt_error"):
                    errors += 1
                ckpt_w.writerow(row_out)
                ckpt_f.flush()
            v = preds.get(f"{model_key}_alertable", "") or f"ERROR: {preds.get(f'{model_key}_alt_error','')}"
            print(f"    [{complete}/{total}] #{row['_row_idx']} {v} ({preds.get(f'{model_key}_alt_latency_ms','')}ms)")

    ckpt_f.close()
    print(f"  Done — {total - errors}/{total} ok, {errors} errors")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--only", nargs="+", metavar="KEY")
    ap.add_argument("--skip", nargs="+", metavar="KEY")
    ap.add_argument("--output", default=None)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    for i, row in enumerate(all_rows):
        row["_row_idx"] = str(i)

    print(f"Loaded {len(all_rows)} rows from {args.csv_path}")

    models = MODELS
    if args.only:
        models = [m for m in MODELS if m[0] in args.only]
    if args.skip:
        models = [m for m in models if m[0] not in args.skip]

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    for model_key, model_id, detail in models:
        print(f"\n=== {model_key} ({model_id}, {detail}) ===")
        run_model(all_rows, fieldnames, model_key, model_id, detail, client, args.workers)

    # Merge every existing checkpoint into the output
    output = args.output or args.csv_path.replace(".csv", "_alertability.csv")
    results = {r["_row_idx"]: dict(r) for r in all_rows}
    all_pred_fields = []
    for model_key, _, _ in MODELS:
        ckpt = f"run_alert_ckpt_{model_key}.csv"
        pred_fields = [f"{model_key}_alertable", f"{model_key}_alt_latency_ms", f"{model_key}_alt_error"]
        all_pred_fields.extend(p for p in pred_fields if p not in all_pred_fields)
        if os.path.exists(ckpt):
            with open(ckpt, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    rid = row["_row_idx"]
                    if rid in results:
                        results[rid].update({k: row[k] for k in pred_fields if k in row})

    out_fields = list(fieldnames) + [p for p in all_pred_fields if p not in fieldnames]
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for row in all_rows:
            w.writerow(results.get(row["_row_idx"], row))

    print(f"\nOutput: {output}")


if __name__ == "__main__":
    main()
