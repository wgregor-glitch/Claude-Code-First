"""
Run the 500 test set across 4 model configurations and produce an annotation CSV.

Models:
  gpt4o_mini_hd — openai/gpt-4o-mini  (high detail)
  gpt4o_mini_ld — openai/gpt-4o-mini  (low detail)
  gpt4o_hd      — openai/gpt-4o       (high detail)
  gemma         — baseten/gemma-4-E4B-it (auto detail)

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_500_comparison.py ~/Downloads/500testset.csv
    python3 run_500_comparison.py ~/Downloads/500testset.csv --output annotation.csv

Checkpoints per model are saved as run_500_ckpt_{model_key}.csv.
Re-running resumes from where each model left off.
"""

import base64, csv, os, re, sys, time, urllib.request

try:
    import openai
except ImportError:
    os.system(f"{sys.executable} -m pip install openai -q")
    import openai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_single_image import HEADLINE_PROMPT, LITELLM_PROXY_URL

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}

MODELS = [
    # (key,            model_id,                   detail)
    ("gpt4o_mini_hd", "openai/gpt-4o-mini",       "high"),
    ("gpt4o_mini_ld", "openai/gpt-4o-mini",       "low"),
    ("gpt4o_hd",      "openai/gpt-4o",            "high"),
    ("gemma",         "baseten/gemma-4-E4B-it",   "auto"),
    ("gemini_flash",  "databricks/databricks-gemini-2-5-flash", "auto"),
]

IN_FIELDS = ["", "Alert Created Date", "Image", "Caption", "Document ID",
             "Internal Alert Threshold", "Source Media URL"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def image_content_from_url(url, detail="auto"):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def clean_response(text):
    import re
    # Strip markdown bold/italic, code fences, bullet markers
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'`+', '', text)
    text = re.sub(r'^[-•]\s+', '', text, flags=re.MULTILINE)
    # Strip common preamble lines
    lines = text.strip().splitlines()
    skip = re.compile(r'^(line\s*[12][\s:–—]|here\s+is|output:|result:)', re.I)
    lines = [l.strip() for l in lines if l.strip() and not skip.match(l.strip())]
    return '\n'.join(lines)


def parse_response(text):
    text = clean_response(text or "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    headline, severity = "", ""
    for line in lines:
        if line in VALID_SEVERITIES:
            severity = line
        elif not headline:
            headline = line
    return headline, severity


def extract_vehicle(headline):
    h = (headline or "").strip()
    if not h or h.lower() == "no incident visible":
        return ""
    if re.match(r"road blocked", h, re.I):
        m = re.match(r"road blocked as (.+?) responds", h, re.I)
        return m.group(1).strip().title() if m else ""
    m = re.match(r"(.+?) detected responding to", h, re.I)
    return m.group(1).strip() if m else ""


def extract_incident(headline):
    h = (headline or "").strip().lower()
    if not h or h == "no incident visible":
        return "no_incident_visible"
    if h.startswith("road blocked"):
        return "blocked_road"
    m = re.search(r"detected responding to (.+)$", h, re.I)
    if not m:
        return ""
    inc = m.group(1).strip().lower()
    if "crash" in inc:                         return "crash"
    if "fire" in inc:                          return "fire"
    if "pulled-over" in inc or "pulled over" in inc: return "pulled_over"
    if "construction" in inc:                  return "construction"
    if "crowd" in inc:                         return "crowd"
    if "unknown" in inc:                       return "unknown_incident"
    return inc


def severity_label(code):
    return {"general.alert2.local": "Local Urgent",
            "general.alert3":       "Signal"}.get(code, code)


# ── Per-model runner ──────────────────────────────────────────────────────────

def run_model(rows, model_key, model_id, detail, client):
    checkpoint_csv = f"run_500_ckpt_{model_key}.csv"
    pred_fields = [f"{model_key}_{s}" for s in
                   ["headline", "vehicle", "incident", "severity", "severity_label", "latency_ms", "error"]]

    # Load existing checkpoint
    done = {}
    if os.path.exists(checkpoint_csv):
        with open(checkpoint_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get(f"{model_key}_error", "").strip():
                    done[row[""]] = row
        if done:
            print(f"  Resuming: {len(done)} rows already done")

    todo = [r for r in rows if r[""] not in done]
    print(f"  To process: {len(todo)}")

    ckpt_fields = IN_FIELDS + pred_fields
    ckpt_is_new = not os.path.exists(checkpoint_csv)
    ckpt_f = open(checkpoint_csv, "a", newline="", encoding="utf-8")
    ckpt_w = csv.DictWriter(ckpt_f, fieldnames=ckpt_fields, extrasaction="ignore")
    if ckpt_is_new:
        ckpt_w.writeheader()

    errors = 0
    for idx, row in enumerate(todo, 1):
        row_id = row[""]
        url    = row["Source Media URL"].strip()
        print(f"    [{idx}/{len(todo)}] #{row_id} ...", end=" ", flush=True)

        out = dict(row)
        try:
            img = image_content_from_url(url, detail)
            t0  = time.monotonic()
            resp = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                max_tokens=300,
                timeout=90,
            )
            latency = round((time.monotonic() - t0) * 1000)
            headline, severity = parse_response(resp.choices[0].message.content)
            out[f"{model_key}_headline"]       = headline
            out[f"{model_key}_vehicle"]        = extract_vehicle(headline)
            out[f"{model_key}_incident"]       = extract_incident(headline)
            out[f"{model_key}_severity"]       = severity
            out[f"{model_key}_severity_label"] = severity_label(severity)
            out[f"{model_key}_latency_ms"]     = latency
            out[f"{model_key}_error"]          = ""
            print(f"{headline} | {severity} ({latency}ms)")
        except Exception as e:
            out[f"{model_key}_headline"]       = ""
            out[f"{model_key}_vehicle"]        = ""
            out[f"{model_key}_incident"]       = ""
            out[f"{model_key}_severity"]       = ""
            out[f"{model_key}_severity_label"] = ""
            out[f"{model_key}_latency_ms"]     = ""
            out[f"{model_key}_error"]          = str(e)
            errors += 1
            print(f"ERROR: {e}")

        ckpt_w.writerow(out)
        ckpt_f.flush()

    ckpt_f.close()
    print(f"  Done — {len(todo) - errors} ok, {errors} errors")

    # Reload full checkpoint
    results = {}
    with open(checkpoint_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            results[r[""]] = r
    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV", help="Path to 500testset.csv")
    ap.add_argument("--output", default="run_500_comparison_results.csv")
    ap.add_argument("--models", default="all",
                    help="Comma-separated model keys to run, e.g. gpt4o_mini_hd,gpt4o_hd")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    # Quick proxy check
    print("Checking proxy ...", end=" ", flush=True)
    try:
        r = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=5, timeout=30,
        )
        print(f"ok ({(r.choices[0].message.content or '').strip()!r})")
    except Exception as e:
        sys.exit(f"FAILED: {e}")

    # Load input
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} rows\n")

    # Select models to run
    selected_keys = None if args.models == "all" else set(args.models.split(","))
    models_to_run = [(k, m, d) for k, m, d in MODELS
                     if selected_keys is None or k in selected_keys]

    # Run each model
    all_preds = {}  # row_id → merged prediction dict
    for model_key, model_id, detail in models_to_run:
        print(f"{'='*60}")
        print(f"Model: {model_key}  ({model_id}, detail={detail})")
        print(f"{'='*60}")
        results = run_model(rows, model_key, model_id, detail, client)
        for row_id, r in results.items():
            all_preds.setdefault(row_id, {}).update(r)

    # Build annotation CSV
    model_pred_fields = []
    for k, _, _ in MODELS:
        model_pred_fields += [
            f"{k}_headline",
            f"{k}_vehicle",
            f"{k}_incident",
            f"{k}_severity_label",  # human-readable: Signal / Local Urgent
            f"{k}_severity",        # raw code
            f"{k}_latency_ms",
            f"{k}_error",
        ]

    annotation_fields = [
        "correct_vehicle_count",
        "correct_vehicle_type",
        "correct_vehicle_type_2",
        "correct_vehicle_type_3",
        "correct_incident_type",
        "correct_severity",
        "notes",
    ]

    out_fields = IN_FIELDS + model_pred_fields + annotation_fields

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            rid = row[""]
            merged = dict(row)
            if rid in all_preds:
                merged.update(all_preds[rid])
            # Preserve any existing annotation values from input CSV
            for col in annotation_fields:
                if col not in merged:
                    merged[col] = row.get("Internal Alert Threshold", "") if col == "correct_severity" else ""
            writer.writerow(merged)

    print(f"\n{'='*60}")
    print(f"Annotation CSV → {args.output}")
    print(f"Columns per model: headline | vehicle | incident | severity_label | severity | latency | error")
    print(f"Fill in: correct_vehicle_count | correct_vehicle_type | correct_vehicle_type_2 | correct_vehicle_type_3 | correct_incident_type | correct_severity | notes")


if __name__ == "__main__":
    main()
