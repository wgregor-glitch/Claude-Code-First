"""
Run the current prompt on a new annotated CSV.

By default runs all 5 models. Use --only to restrict to one.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_newprompt.py newprompt.csv                          # all models
    python3 run_newprompt.py newprompt.csv --only gpt4o_mini_hd    # one model
    python3 run_newprompt.py newprompt.csv --output results.csv --workers 15
    python3 run_newprompt.py newprompt.csv --skip gemma gemini_flash

Checkpoint: run_newprompt_ckpt_<key>.csv per model (resume-safe).
"""

import argparse, base64, csv, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

try:
    import openai
except ImportError:
    os.system(f"{sys.executable} -m pip install openai -q")
    import openai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_single_image import HEADLINE_PROMPT, LITELLM_PROXY_URL

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}

MODELS = [
    # (key,            model_id,                                   detail)
    ("gpt4o_mini_hd", "openai/gpt-4o-mini",                      "high"),
    ("gpt4o_mini_ld", "openai/gpt-4o-mini",                      "low"),
    ("gpt4o_hd",      "openai/gpt-4o",                           "high"),
    ("gemma",         "baseten/gemma-4-E4B-it",                  "auto"),
    ("gemini_flash",  "databricks/databricks-gemini-2-5-flash",  "auto"),
    ("claude_sonnet", "anthropic/claude-sonnet-4-6",             "auto"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def image_content_from_url(url, detail="high"):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def clean_response(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'`+', '', text)
    text = re.sub(r'^[-•]\s+', '', text, flags=re.MULTILINE)
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
    # Discard truncated headlines that end before the incident phrase
    if headline and re.search(r'\bresponding\s*(?:to\s*)?$', headline, re.I):
        headline = ""
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
    if "crash" in inc:                               return "crash"
    if "fire" in inc:                                return "fire"
    if "pulled-over" in inc or "pulled over" in inc: return "pulled_over"
    if "construction" in inc:                        return "construction"
    if "crowd" in inc:                               return "crowd"
    if "unknown" in inc:                             return "unknown_incident"
    return inc


def severity_label(code):
    return {"general.alert2.local": "Local Urgent",
            "general.alert3":       "Signal"}.get(code, code)


# ── Worker ────────────────────────────────────────────────────────────────────

def process_row(row, model_key, model_id, detail, client):
    url = row["Source Media URL"].strip()
    try:
        img = image_content_from_url(url, detail)
        t0  = time.monotonic()
        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
            max_tokens=500,
            timeout=90,
        )
        latency = round((time.monotonic() - t0) * 1000)
        headline, severity = parse_response(resp.choices[0].message.content)
        return {
            f"{model_key}_headline":       headline,
            f"{model_key}_vehicle":        extract_vehicle(headline),
            f"{model_key}_incident":       extract_incident(headline),
            f"{model_key}_severity_label": severity_label(severity),
            f"{model_key}_severity":       severity,
            f"{model_key}_latency_ms":     latency,
            f"{model_key}_error":          "",
        }
    except Exception as e:
        return {
            f"{model_key}_headline":       "",
            f"{model_key}_vehicle":        "",
            f"{model_key}_incident":       "",
            f"{model_key}_severity_label": "",
            f"{model_key}_severity":       "",
            f"{model_key}_latency_ms":     "",
            f"{model_key}_error":          str(e),
        }


# ── Per-model runner ──────────────────────────────────────────────────────────

def run_model(all_rows, fieldnames, model_key, model_id, detail, client, workers):
    ckpt = f"run_newprompt_ckpt_{model_key}.csv"
    pred_fields = [f"{model_key}_{s}" for s in
                   ["headline", "vehicle", "incident", "severity_label", "severity", "latency_ms", "error"]]

    done = {}
    if os.path.exists(ckpt):
        with open(ckpt, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get(f"{model_key}_error", "").strip():
                    done[row["_row_idx"]] = row
        if done:
            print(f"  Checkpoint: {len(done)} rows already done")

    todo = [r for r in all_rows if r["_row_idx"] not in done]
    print(f"  To process: {len(todo)}")

    if not todo:
        return

    ckpt_fields = ["_row_idx"] + list(fieldnames) + [p for p in pred_fields if p not in fieldnames]
    ckpt_is_new = not os.path.exists(ckpt)
    ckpt_lock   = Lock()
    ckpt_f      = open(ckpt, "a", newline="", encoding="utf-8")
    ckpt_w      = csv.DictWriter(ckpt_f, fieldnames=ckpt_fields, extrasaction="ignore")
    if ckpt_is_new:
        ckpt_w.writeheader()

    errors = 0
    complete = 0
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
                if preds.get(f"{model_key}_error"):
                    errors += 1
                ckpt_w.writerow(row_out)
                ckpt_f.flush()
            headline = preds.get(f"{model_key}_headline", "")
            severity = preds.get(f"{model_key}_severity", "")
            latency  = preds.get(f"{model_key}_latency_ms", "")
            err      = preds.get(f"{model_key}_error", "")
            status   = f"ERROR: {err}" if err else f"{headline} | {severity} ({latency}ms)"
            print(f"    [{complete}/{total}] #{row['_row_idx']} {status}")

    ckpt_f.close()
    print(f"  Done — {total - errors}/{total} ok, {errors} errors")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--only",   nargs="+", metavar="KEY",
                    help="Run only these model keys, e.g. --only gpt4o_mini_hd gpt4o_hd")
    ap.add_argument("--skip",   nargs="+", metavar="KEY",
                    help="Skip these model keys, e.g. --skip gemma gemini_flash")
    ap.add_argument("--output",  default=None,
                    help="Output CSV path (default: <input>_results.csv)")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    # Inject stable positional key — handles CSVs where the index column is empty
    for i, row in enumerate(all_rows):
        row["_row_idx"] = str(i)

    print(f"Loaded {len(all_rows)} rows from {args.csv_path}")

    # Select models to run
    models = MODELS
    if args.only:
        models = [m for m in MODELS if m[0] in args.only]
    if args.skip:
        models = [m for m in models if m[0] not in args.skip]

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    for model_key, model_id, detail in models:
        print(f"\n=== {model_key} ({model_id}, {detail}) ===")
        run_model(all_rows, fieldnames, model_key, model_id, detail, client, args.workers)

    # Merge checkpoint results into final output — include EVERY model with an
    # existing checkpoint, not just the ones run this invocation, so partial
    # runs don't overwrite other models' columns in the output CSV
    output = args.output or args.csv_path.replace(".csv", "_results.csv")
    results = {r["_row_idx"]: dict(r) for r in all_rows}
    all_pred_fields = []
    for model_key, _, _ in MODELS:
        ckpt = f"run_newprompt_ckpt_{model_key}.csv"
        pred_fields = [f"{model_key}_{s}" for s in
                       ["headline", "vehicle", "incident", "severity_label", "severity", "latency_ms", "error"]]
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
