"""
Text tester for LiteLLM proxy (Dataminr internal network / VPN required).

Runs a text prompt template against rows in a CSV and saves results.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 text_tester.py --csv posts.csv --text-column TEXT --prompt-file prompt.txt \
        --models openai/gpt-4.1 --limit 0
"""

import argparse
import csv
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import openai
from tqdm import tqdm

LITELLM_PROXY_URL = "https://llm-proxy.ai.use1.test.dmnr.io"

HEADLINE_PROMPT = """\
Role:
You are a digital risk and security analyst.

Goal:
Rewrite each user post into a single-sentence, AP-style newsroom headline suitable for a risk or security briefing.

Core Rules

Start with: User …

One sentence only – concise, factual, complete.

Tone: Neutral, editorial, professional.

Never copy the whole post verbatim.

IMPORTANT: The headline should only be a maximum length of 100 characters , this includes spaces and punctuation as well as letters and numbers. The caption should never exceed the 100 character limit (Or 20 words).

Use quotes only for text that appears exactly in the post — never paraphrase or invent inside quotes.

Never include racial epithets, slurs, or sensitive identity-based language in the caption, even if present in the post. This includes epithets in nouns and proper nouns, for example altering a company or persons name to make it sound derogatory.

If such language appears, omit it or summarize neutrally (e.g., User comments on group relations).

Outside quotes, freely rewrite or summarize.

Return only the headline — no markdown, no JSON, no commentary.

If no valid headline can be produced: output null.

Always include the company, executive or product context in the headline.

Ensure conspiracies and misinformation are labelled in the headline.

Example:

Input text: You think Blackrock and Larry Fink are still gonna be in business when the Rothschilds tax haven (Israel) is made defunct?

Output headline: User makes conspiratorial claim that BlackRock and Larry Fink will fail once the Rothschild tax haven ends, implying Israeli influence

Style Guide

Case: Sentence case (only the first word and proper nouns capitalized).

Profanity: Remove unless essential to meaning or threat; if essential, quote exactly.

Dates: AP format (Jan., Feb., etc.); times in 24-hour clock.

Numbers: Spell out below 10; use numerals for 10 and above.

No Oxford comma.

Focus: Who/what/where/when/impact.

Headline Templates

Opinion → User says "<exact phrase>" about <entity>

Report → User reports "<exact phrase>"

Claim → User claims "<exact phrase>"

Warning → User warns "<exact phrase>"

Observation → User notes "<exact phrase>"

Threat → User makes threat to X group/company, says "<exact phrase>

Sensitive or identity-related post → User comments on <topic> (no slurs or identity terms)

Examples

Post: "They hacked into the hospital systems again, patients couldn't get treatment all night in Chicago."
Headline: User reports "They hacked into the hospital systems again", citing overnight treatment disruptions in Chicago

Post: "The Blackrock CEO is a total fraud, can't believe anyone still trusts him."
Headline: User says "This Blackrock CEO is a total fraud", questioning continued public trust in leadership

Post: "New ransomware group took down a port in Spain yesterday, nothing moving in or out."
Headline: User reports "New ransomware group took down a port in Spain", halting trade operations

Post: "I think phishing is only getting worse for banks, I had an SMS one from Bank of America this morning"
Headline: User warns "phishing is only getting worse for banks", claiming they received a phishing SMS from Bank of America this morning

Post: "Company X stock just keeps dropping every week, this is crazy."
Headline: User notes "Company X stock just keeps dropping every week", expressing concern over market stability

Post: "Those [epithet] people don't belong here, I'm gonna take out a few in CostCo"
Headline: User makes threat to ethnic group, says they will "take out a few in CostCo"

Output:
Return only the rewritten headline, starting with User.

text: {text}"""


def check_proxy_connectivity(client: openai.OpenAI, model: str) -> None:
    print(f"Checking proxy connectivity with model {model!r} ...", end=" ", flush=True)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=50,
            timeout=30,
        )
        reply = (response.choices[0].message.content or "").strip()
        print(f"OK (got: {reply!r})")
    except Exception as exc:
        print(f"FAILED\n  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        print(
            "\nPossible causes:\n"
            "  1. Not on Dataminr VPN\n"
            "  2. ANTHROPIC_AUTH_TOKEN is wrong or expired\n"
            "  3. Model alias not found on proxy\n",
            file=sys.stderr,
        )
        sys.exit(1)


def run_row(client: openai.OpenAI, text: str, model: str, prompt_template: str) -> tuple[str, str]:
    """Returns (headline, model_used)."""
    prompt = prompt_template.replace("{text}", text)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        timeout=60,
    )
    headline = (response.choices[0].message.content or "").strip()
    return headline, response.model


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a text prompt over CSV rows via LiteLLM proxy")
    parser.add_argument("--csv", required=True, help="Input CSV file")
    parser.add_argument("--text-column", default="TEXT", help='Column containing input text (default: "TEXT")')
    parser.add_argument("--models", nargs="+", default=["openai/gpt-4.1"], metavar="MODEL")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max rows to process (default: 10; use 0 for all)")
    parser.add_argument("--output", default=None)
    parser.add_argument("--proxy-url", default=LITELLM_PROXY_URL)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        print("ERROR: Set ANTHROPIC_AUTH_TOKEN", file=sys.stderr)
        sys.exit(1)

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"ERROR: {csv_path} not found", file=sys.stderr)
        sys.exit(1)

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)
    check_proxy_connectivity(client, args.models[0])

    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row_dict in enumerate(reader, start=1):
            text = (row_dict.get(args.text_column) or "").strip()
            if not text:
                continue
            records.append({"row": i, "text": text})
            if args.limit and len(records) >= args.limit:
                break

    if not records:
        print("No rows found.", file=sys.stderr)
        sys.exit(1)

    output_file = args.output or f"results_headline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    print(f"Rows:   {len(records)}")
    print(f"Models: {args.models}")
    print(f"Output: {output_file}\n")

    fields = ["row", "text", "model", "model_used", "headline", "latency_ms", "error"]
    total = len(records) * len(args.models)

    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fields)
        writer.writeheader()

        with tqdm(total=total, unit="req") as pbar:
            for rec in records:
                for model in args.models:
                    pbar.set_postfix(row=rec["row"], model=model.split("/")[-1][:20])
                    t0 = time.monotonic()
                    try:
                        headline, model_used = run_row(client, rec["text"], model, HEADLINE_PROMPT)
                        result = {
                            "row": rec["row"], "text": rec["text"],
                            "model": model, "model_used": model_used,
                            "headline": headline,
                            "latency_ms": round((time.monotonic() - t0) * 1000),
                            "error": "",
                        }
                    except Exception as exc:
                        tqdm.write(f"  ERR row={rec['row']} model={model}: {type(exc).__name__}: {exc}")
                        result = {
                            "row": rec["row"], "text": rec["text"],
                            "model": model, "model_used": "",
                            "headline": "",
                            "latency_ms": 0,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    writer.writerow(result)
                    csvfile.flush()
                    pbar.update(1)

    print(f"\nDone. Results saved to: {output_file}")


if __name__ == "__main__":
    main()
