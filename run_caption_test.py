"""Test harness for the security intelligence caption prompt.

Runs the system prompt from security_intelligence_prompt.md over a CSV of
(location, text) rows against one or more models on the LiteLLM proxy, then
writes per-model result CSVs, an automated rule-compliance report, and a
cross-model disagreement summary.

Usage:
    export LITELLM_API_KEY=sk-...
    python run_caption_test.py --csv usgeotest.csv --models gpt-5.1,gpt-5-nano
    python run_caption_test.py --list-models          # resolve exact aliases
    python run_caption_test.py --csv usgeotest.csv --limit 25   # smoke test

Stdlib only — no SDK dependencies.
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROXY_URL = os.environ.get("LITELLM_PROXY_URL", "https://llm-proxy-test.dataminr.com").rstrip("/")
PROMPT_FILE = Path(__file__).parent / "security_intelligence_prompt.md"

BANNED_TERMS = [
    "far-left", "far-right", "left-wing", "right-wing", "extremist",
    "radical", "terrorist", "rioters", "counterprotesters",
    "crackdown", "repression", "persecution",
]
MONTH_ABBREVS = r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.? \d"


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    """Extract the '## System Prompt' section from the prompt markdown."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    match = re.search(r"## System Prompt\n(.*?)(?=\n---\n|\n## User Prompt Template)", text, re.S)
    if not match:
        sys.exit(f"Could not find '## System Prompt' section in {PROMPT_FILE}")
    return match.group(1).strip()


def build_user_message(text: str, location: str) -> str:
    return (
        "Write one caption for the event below, following the system rules exactly.\n"
        "Read the Translated text; only read the Original text if no translation is provided.\n\n"
        f"Original text: {text}\n"
        "Translated text: \n"
        f"Event Location: {location}"
    )


# ---------------------------------------------------------------------------
# Proxy client (stdlib)
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict, api_key: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{PROXY_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get(path: str, api_key: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{PROXY_URL}{path}", headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def caption_row(system_prompt: str, text: str, location: str, model: str, api_key: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_message(text, location)},
        ],
    }
    last_err: Exception = RuntimeError("no attempts made")
    for attempt in range(5):
        try:
            resp = _post("/v1/chat/completions", payload, api_key)
            content = resp["choices"][0]["message"]["content"]
            return (content or "").strip().strip('"').strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            # 4xx other than rate limit: retrying identical payload won't help
            if e.code in (400, 401, 403, 404, 422):
                return f"<<ERROR {e.code}: {body}>>"
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
        except Exception as e:  # network hiccups, timeouts, bad JSON
            last_err = e
        time.sleep(2 ** attempt)
    return f"<<ERROR: {last_err}>>"


# ---------------------------------------------------------------------------
# Compliance checks
# ---------------------------------------------------------------------------

def is_us_row(location: str) -> bool:
    loc = location.strip()
    return (
        "USA" in loc
        or "United States" in loc
        or bool(re.search(r",\s*DC\b", loc))
        or bool(re.search(r",\s*[A-Z]{2}\s+\d{5}", loc))
    )


def check_caption(caption: str, location: str) -> list[str]:
    """Return a list of rule-violation labels for one caption."""
    problems: list[str] = []
    us = is_us_row(location)

    if caption.startswith("<<ERROR"):
        return ["api-error"]
    if caption == "NO CAPTION":
        if not us:
            problems.append("no-caption-on-non-us-row")
        return problems

    if len(caption) > 200:
        problems.append("over-200-chars")
    if caption.endswith("."):
        problems.append("trailing-period")
    if "\n" in caption:
        problems.append("multiline")
    if re.search(r"\b([Tt]he|[Aa]n?)\b", caption):
        problems.append("contains-article")
    if re.match(r"\d", caption):
        problems.append("starts-with-numeral")
    if re.search(r"\b\d{1,2}\s?(am|pm|AM|PM)\b", caption):
        problems.append("12-hour-clock")
    if re.search(MONTH_ABBREVS, caption):
        problems.append("abbreviated-month")
    if re.search(r"\b20\d{2}\b", caption):
        problems.append("contains-year")
    for term in BANNED_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", caption, re.I):
            problems.append(f"banned-term:{term}")
    if us and not re.match(r"(Protest|Demonstration)\b", caption):
        problems.append("us-format-violation")
    if not us and not re.search(r", [A-Z][^,]+$", caption):
        problems.append("missing-trailing-location")
    return problems


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Caption prompt test harness")
    ap.add_argument("--csv", help="Input CSV with columns: location, text[, result caption]")
    ap.add_argument("--models", default="gpt-5.1,gpt-5-nano", help="Comma-separated model aliases")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="Only run first N rows (0 = all)")
    ap.add_argument("--out", default="caption_test_results", help="Output directory")
    ap.add_argument("--list-models", action="store_true", help="List proxy model aliases and exit")
    args = ap.parse_args()

    api_key = os.environ.get("LITELLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("Set LITELLM_API_KEY in the environment")

    if args.list_models:
        data = _get("/v1/models", api_key)
        for m in sorted(x["id"] for x in data["data"]):
            print(m)
        return

    if not args.csv:
        sys.exit("--csv is required (or use --list-models)")

    system_prompt = load_system_prompt()
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    results: dict[str, list[str]] = {}
    for model in models:
        print(f"=== {model}: {len(rows)} rows ===", flush=True)
        captions: list[str] = [""] * len(rows)
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(
                    caption_row, system_prompt, r["text"], r["location"], model, api_key
                ): i
                for i, r in enumerate(rows)
            }
            for fut in concurrent.futures.as_completed(futures):
                captions[futures[fut]] = fut.result()
                done += 1
                if done % 50 == 0:
                    print(f"  {done}/{len(rows)}", flush=True)
        results[model] = captions

        safe = model.replace("/", "_")
        with open(outdir / f"captions_{safe}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["location", "text", "result caption"])
            for r, cap in zip(rows, captions):
                w.writerow([r["location"], r["text"], cap])

    # ---- compliance report ----
    report_lines = ["# Caption test report", ""]
    for model, captions in results.items():
        counts: dict[str, int] = {}
        examples: dict[str, str] = {}
        no_caption = sum(1 for c in captions if c == "NO CAPTION")
        errors = sum(1 for c in captions if c.startswith("<<ERROR"))
        for r, cap in zip(rows, captions):
            for p in check_caption(cap, r["location"]):
                counts[p] = counts.get(p, 0) + 1
                examples.setdefault(p, cap[:160])
        us_total = sum(1 for r in rows if is_us_row(r["location"]))
        report_lines += [
            f"## {model}",
            f"- rows: {len(captions)} | US rows: {us_total} | NO CAPTION: {no_caption} | API errors: {errors}",
            "- violations:" if counts else "- violations: none",
        ]
        for p, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            report_lines.append(f"  - {p}: {n}  (e.g. \"{examples[p]}\")")
        report_lines.append("")

    if len(models) == 2:
        a, b = models
        diff_rows = [
            i for i in range(len(rows))
            if (results[a][i] == "NO CAPTION") != (results[b][i] == "NO CAPTION")
        ]
        report_lines += [
            "## Cross-model disagreement",
            f"- rows where exactly one of [{a}, {b}] output NO CAPTION: {len(diff_rows)}",
        ]
        for i in diff_rows[:20]:
            report_lines.append(
                f"  - row {i} ({rows[i]['location'][:50]}): {a}=\"{results[a][i][:80]}\" | {b}=\"{results[b][i][:80]}\""
            )

    report = "\n".join(report_lines)
    (outdir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nOutputs written to {outdir}/")


if __name__ == "__main__":
    main()
