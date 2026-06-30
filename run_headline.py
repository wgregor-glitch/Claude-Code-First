"""
run_headline.py — generate a short alert-style headline (≤15 words) for each GT image.

Usage (run locally on VPN):
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 run_headline.py ~/Downloads/testsheet.csv --limit 20
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).parent))
from image_tester import LITELLM_PROXY_URL, check_proxy_connectivity, image_content_from_url

HEADLINE_PROMPT = """\
Look at this traffic camera image and write a single alert-style headline describing \
what is happening in the scene.

Rules:
- Maximum 15 words
- Describe what is VISIBLE — vehicles present, incident type, activity
- Use plain factual language (not "I see..." or "The image shows...")
- If nothing is happening, say so (e.g. "Normal traffic flow, no incident visible")
- Output ONLY the headline, nothing else

Examples of good headlines:
  Police conducting traffic stop on highway shoulder
  Multi-vehicle crash at intersection, fire and police on scene
  Road construction blocking lane, heavy machinery operating
  Two police vehicles responding to active incident, scene unclear
  Normal traffic, no emergency vehicles or incident visible"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", metavar="CSV")
    ap.add_argument("--model", default="openai/gpt-4o")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--url-col", type=int, default=0)
    ap.add_argument("--output", default=None)
    ap.add_argument("--proxy-url", default=LITELLM_PROXY_URL)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN")

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)
    check_proxy_connectivity(client, args.model)

    # Read GT rows (rows with URLs)
    records = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)  # skip merged header
        for i, row in enumerate(reader, start=1):
            if len(row) <= args.url_col:
                continue
            url = row[args.url_col].strip()
            if not url or not url.startswith("http"):
                continue
            records.append({"row": i, "url": url})
            if args.limit and len(records) >= args.limit:
                break

    print(f"Images: {len(records)}")

    output_file = args.output or f"headlines_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as outf:
        writer = csv.DictWriter(outf, fieldnames=["row", "url", "headline", "latency_ms", "error"])
        writer.writeheader()

        for idx, rec in enumerate(records, start=1):
            print(f"  [{idx}/{len(records)}] row {rec['row']} ...", end=" ", flush=True)
            try:
                img = image_content_from_url(rec["url"])
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                    max_tokens=60,
                    timeout=90,
                )
                latency = round((time.monotonic() - t0) * 1000)
                headline = (resp.choices[0].message.content or "").strip()
                print(f"ok — {headline}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": headline, "latency_ms": latency, "error": ""})
            except Exception as exc:
                print(f"ERROR: {exc}")
                writer.writerow({"row": rec["row"], "url": rec["url"],
                                 "headline": "", "latency_ms": 0, "error": str(exc)})
            outf.flush()

    print(f"\nDone. Upload {output_file} back to review.")


if __name__ == "__main__":
    main()
