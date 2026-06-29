"""
run_annotate.py — run the annotate prompt against GT rows in a Vizzion annotation
sheet and write a combined CSV with gt_* and pred_* columns side by side.

Upload the output file back for instant P/R/F1 analysis without any manual steps.

Usage (run locally on VPN):
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python run_annotate.py Cloudfactory_Vizzion_annotation__Sheet10.csv
    python run_annotate.py sheet.csv --limit 20   # quick test
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).parent))
from image_tester import (
    ANNOTATE_FIELD_MAP,
    LITELLM_PROXY_URL,
    QUESTIONS,
    check_proxy_connectivity,
    image_content_from_url,
)

BOOL_TRUTHY = {"true", "1", "yes", "x"}


def to_bool(v):
    return str(v).strip().lower() in BOOL_TRUTHY


def main():
    ap = argparse.ArgumentParser(
        description="Run annotate prompt on GT rows; output GT+pred combined CSV"
    )
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--model", default="anthropic/claude-sonnet-4-6")
    ap.add_argument("--limit", type=int, default=0, help="0 = all rows")
    ap.add_argument(
        "--url-col",
        type=int,
        default=1,
        help="0-based column index for image URL (default 1 = column B)",
    )
    ap.add_argument("--output", default=None)
    ap.add_argument("--proxy-url", default=LITELLM_PROXY_URL)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN (key from llm-proxy UI)")

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)
    check_proxy_connectivity(client, args.model)

    field_keys = list(ANNOTATE_FIELD_MAP.keys())
    csv_col_names = list(ANNOTATE_FIELD_MAP.values())

    # Read GT rows — only rows that have a non-empty URL
    gt_rows = []
    with open(args.csv_path, newline="", encoding="utf-8") as f:
        next(f)  # skip merged group header row
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        url_col_name = fieldnames[args.url_col]
        for i, row in enumerate(reader, start=1):
            url = (row.get(url_col_name) or "").strip()
            if not url:
                continue
            rec = {"row": i, "url": url}
            for key, col in zip(field_keys, csv_col_names):
                rec[f"gt_{key}"] = to_bool(row.get(col, ""))
            gt_rows.append(rec)
            if args.limit and len(gt_rows) >= args.limit:
                break

    print(f"GT rows with URLs: {len(gt_rows)}")
    if not gt_rows:
        sys.exit("No rows with URLs found — check --url-col")

    out_cols = (
        ["row", "url"]
        + [f"gt_{k}" for k in field_keys]
        + [f"pred_{k}" for k in field_keys]
        + ["reasoning", "latency_ms", "error"]
    )
    output_file = args.output or f"combined_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    q = QUESTIONS["annotate"]

    with open(output_file, "w", newline="", encoding="utf-8") as outf:
        writer = csv.DictWriter(outf, fieldnames=out_cols)
        writer.writeheader()

        for idx, rec in enumerate(gt_rows, start=1):
            print(f"  [{idx}/{len(gt_rows)}] row {rec['row']} ...", end=" ", flush=True)
            out_row = dict(rec)

            try:
                img = image_content_from_url(rec["url"])
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [img, {"type": "text", "text": q["prompt"]}],
                        }
                    ],
                    tools=[q["tool"]],
                    tool_choice={"type": "function", "function": {"name": "classify_image"}},
                    max_tokens=512,
                    timeout=90,
                )
                latency = round((time.monotonic() - t0) * 1000)
                pargs = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
                for k in field_keys:
                    out_row[f"pred_{k}"] = pargs.get(k, False)
                out_row["reasoning"] = pargs.get("reasoning", "")
                out_row["latency_ms"] = latency
                out_row["error"] = ""
                print(f"ok ({latency}ms)")
            except Exception as exc:
                for k in field_keys:
                    out_row[f"pred_{k}"] = False
                out_row["reasoning"] = ""
                out_row["latency_ms"] = 0
                out_row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"ERROR: {exc}")

            writer.writerow(out_row)
            outf.flush()

    print(f"\nDone. Upload {output_file} back for analysis.")


if __name__ == "__main__":
    main()
