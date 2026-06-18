"""
Multi-model image/text tester.

Sends rows from a CSV to one or more models via the LiteLLM proxy and writes
results back as a CSV.

Usage examples
--------------
# Image captioning across two models
python3 image_tester.py \
  --csv ~/Downloads/vizzion-test.csv \
  --url-column "Alert Fields Source Media URL" \
  --question caption \
  --models openai/gpt-4o baseten/gemma-4-12B-it \
  --limit 0

# Text-only headline generation
python3 image_tester.py \
  --csv ~/Downloads/testclaude.csv \
  --question q2 \
  --models openai/gpt-4o anthropic/claude-sonnet-4-6 gemini/gemini-1.5-pro \
  --limit 100
"""

import argparse
import base64
import csv
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import httpx
import openai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LITELLM_PROXY_URL = "https://llm-proxy-test.dataminr.com/"
DEFAULT_TEXT_COLUMN = "TEXT"

# ---------------------------------------------------------------------------
# Predefined question prompts
# ---------------------------------------------------------------------------

_CAPTION_SYSTEM = """You are a digital risk and security analyst."""

_CAPTION_USER_TMPL = """Role:
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

Threat → User makes threat to X group/company, says "<exact phrase>"

Sensitive or identity-related post → User comments on <topic> (no slurs or identity terms)

Output:
Return only the rewritten headline, starting with User.

text: {text}"""

_Q2_SYSTEM = """You are a risk intelligence analyst."""

_Q2_USER_TMPL = """Analyze the following content and identify the primary risk category it belongs to (choose one: Physical Threat, Cyber Threat, Misinformation, Civil Unrest, Financial Risk, Reputational Risk, Unknown). Then provide a one-sentence risk summary.

Format your response exactly as:
Category: <category>
Summary: <one sentence>

Content:
{text}"""

# When a URL column is present, we use a vision prompt for captioning
_CAPTION_IMAGE_SYSTEM = """You are a professional image analyst."""

_CAPTION_IMAGE_USER = """Describe this image in a single AP-style newsroom caption (max 100 characters, max 20 words). Start with the subject. Be factual and neutral. Return only the caption."""


PREDEFINED_QUESTIONS: dict[str, dict] = {
    "caption": {
        "system": _CAPTION_SYSTEM,
        "user_tmpl": _CAPTION_USER_TMPL,  # uses {text}
        "image_system": _CAPTION_IMAGE_SYSTEM,
        "image_user": _CAPTION_IMAGE_USER,
    },
    "q2": {
        "system": _Q2_SYSTEM,
        "user_tmpl": _Q2_USER_TMPL,  # uses {text}
        "image_system": _CAPTION_IMAGE_SYSTEM,
        "image_user": "What is shown in this image? Answer in one sentence.",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    for var in ("ANTHROPIC_AUTH_TOKEN", "LITELLM_API_KEY", "ANTHROPIC_API_KEY"):
        val = os.environ.get(var)
        if val:
            return val
    return "placeholder"


def _fetch_image_b64(url: str, timeout: int = 15) -> tuple[str, str]:
    """Fetch an image URL and return (base64_data, media_type)."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    b64 = base64.standard_b64encode(resp.content).decode()
    return b64, content_type


def _call_model(
    client: openai.OpenAI,
    model: str,
    system: str,
    user_content: list | str,
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=512,
    )
    return (resp.choices[0].message.content or "").strip()


def _build_text_user_content(question_def: dict, text: str) -> str:
    return question_def["user_tmpl"].format(text=text)


def _build_image_user_content(question_def: dict, image_b64: str, media_type: str) -> list:
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
        },
        {"type": "text", "text": question_def["image_user"]},
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-model image/text tester")
    parser.add_argument("--csv", required=True, help="Path to input CSV")
    parser.add_argument(
        "--url-column",
        default=None,
        help="CSV column containing image URLs (optional)",
    )
    parser.add_argument(
        "--text-column",
        default=DEFAULT_TEXT_COLUMN,
        help=f"CSV column containing text content (default: {DEFAULT_TEXT_COLUMN})",
    )
    parser.add_argument(
        "--question",
        required=True,
        help="Predefined question key (caption, q2) or a raw prompt string. "
             "Raw prompts may use {text} as a placeholder.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="One or more model identifiers (e.g. openai/gpt-4o anthropic/claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max rows to process (0 = all)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: <input>_results.csv)",
    )
    parser.add_argument(
        "--proxy-url",
        default=LITELLM_PROXY_URL,
        help=f"LiteLLM proxy base URL (default: {LITELLM_PROXY_URL})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between API calls to avoid rate limits (default: 0.5)",
    )
    args = parser.parse_args()

    # Resolve question definition
    if args.question in PREDEFINED_QUESTIONS:
        question_def = PREDEFINED_QUESTIONS[args.question]
        is_predefined = True
    else:
        # Treat --question value as a raw user prompt (no system prompt)
        question_def = {
            "system": "You are a helpful assistant.",
            "user_tmpl": args.question if "{text}" in args.question else args.question + "\n\n{text}",
            "image_system": "You are a helpful assistant.",
            "image_user": args.question,
        }
        is_predefined = False

    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    output_path = Path(args.output) if args.output else csv_path.with_name(csv_path.stem + "_results.csv")

    api_key = _resolve_api_key()
    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if args.limit > 0:
        rows = rows[: args.limit]

    use_images = args.url_column is not None

    # Validate columns exist
    if use_images and args.url_column not in (fieldnames or []):
        sys.exit(f"URL column '{args.url_column}' not found in CSV. Available: {fieldnames}")
    if not use_images and args.text_column not in (fieldnames or []):
        # Try to auto-detect
        if fieldnames:
            print(f"Warning: text column '{args.text_column}' not found. Available columns: {fieldnames}")
            print(f"Using first column: '{fieldnames[0]}'")
            args.text_column = fieldnames[0]
        else:
            sys.exit(f"Text column '{args.text_column}' not found in CSV.")

    result_columns = [f"result_{m.replace('/', '_')}" for m in args.models]
    out_fieldnames = fieldnames + [c for c in result_columns if c not in fieldnames]

    total = len(rows)
    print(f"Processing {total} rows with {len(args.models)} model(s): {', '.join(args.models)}")
    print(f"Question: {args.question}  |  Mode: {'image+text' if use_images else 'text-only'}")
    print(f"Output: {output_path}")
    print("-" * 70)

    with open(output_path, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=out_fieldnames, extrasaction="ignore")
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            print(f"[{i}/{total}]", end=" ", flush=True)

            for model, col in zip(args.models, result_columns):
                try:
                    if use_images:
                        url = row.get(args.url_column, "").strip()
                        if not url:
                            row[col] = "ERROR: empty URL"
                            continue
                        image_b64, media_type = _fetch_image_b64(url)
                        user_content = _build_image_user_content(question_def, image_b64, media_type)
                        system = question_def["image_system"]
                    else:
                        text = row.get(args.text_column, "").strip()
                        if not text:
                            row[col] = "null"
                            continue
                        user_content = _build_text_user_content(question_def, text)
                        system = question_def["system"]

                    result = _call_model(client, model, system, user_content)
                    row[col] = result
                    print(f"{model}: OK", end="  ", flush=True)

                    if args.delay > 0:
                        time.sleep(args.delay)

                except Exception as exc:
                    row[col] = f"ERROR: {exc}"
                    print(f"{model}: ERROR({exc})", end="  ", flush=True)

            writer.writerow(row)
            out_f.flush()
            print()

    print("-" * 70)
    print(f"Done. Results written to {output_path}")


if __name__ == "__main__":
    main()
