"""
Image tester for LiteLLM proxy (Dataminr internal network / VPN required).

Tests images against multiple vision models and saves results to CSV.
Default run uses --limit 10 for prompt refinement; set --limit 0 for all rows.

Modes:
  Local files:  --images-dir /path/to/images
  CSV URLs:     --csv /path/to/file.csv  (reads "Source Media URL" column)

Questions:
  --question q1   Emergency vehicle types (default)
  --question q2   Incident type visible in image

Usage (run locally on VPN):
    export ANTHROPIC_AUTH_TOKEN="sk-..."   # key from llm-proxy UI
    python image_tester.py --csv dataminr_alerts.csv --question q1 --limit 10
    python image_tester.py --csv dataminr_alerts.csv --question q2 --limit 10
"""

import argparse
import base64
import csv
import json
import os
import sys
import traceback
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import openai
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_PROXY_URL = "https://llm-proxy.ai.use1.test.dmnr.io"

DEFAULT_MODELS = [
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4-6",
    "gemini/gemini-1.5-pro",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CSV_URL_COLUMN = "Source Media URL"

# ---------------------------------------------------------------------------
# Question definitions
# ---------------------------------------------------------------------------

QUESTIONS: dict[str, dict[str, Any]] = {
    "q1": {
        "prompt": (
            "Check all emergency response vehicle types visible in this image.\n\n"
            "☑ Police\n"
            "☑ Fire Truck\n"
            "☑ Ambulance\n"
            "○ No emergency vehicles\n"
            "○ Hard to tell\n\n"
            "Rules:\n"
            "- You may check any combination of Police, Fire Truck, and Ambulance.\n"
            "- 'No emergency vehicles' and 'Hard to tell' are exclusive — selecting either "
            "means none of the vehicle checkboxes above can also be selected, and these two "
            "cannot be selected together."
        ),
        "tool": {
            "type": "function",
            "function": {
                "name": "classify_image",
                "description": "Report which emergency response vehicle types are visible in the image.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "police":                 {"type": "boolean", "description": "Police vehicle(s) are visible"},
                        "fire_truck":             {"type": "boolean", "description": "Fire truck(s) are visible"},
                        "ambulance":              {"type": "boolean", "description": "Ambulance(s) are visible"},
                        "no_emergency_vehicles":  {"type": "boolean", "description": "Clearly no emergency vehicles present"},
                        "hard_to_tell":           {"type": "boolean", "description": "Hard to tell based on what is visible"},
                        "reasoning":              {"type": "string",  "description": "Brief explanation of the visible content"},
                    },
                    "required": ["police", "fire_truck", "ambulance", "no_emergency_vehicles", "hard_to_tell", "reasoning"],
                },
            },
        },
        "fields": ["police", "fire_truck", "ambulance", "no_emergency_vehicles", "hard_to_tell"],
    },
    "q2": {
        "prompt": (
            "Are any of the following visible in the image? Select all that apply:\n"
            "- Crash\n"
            "- Pulled over vehicle\n"
            "- Blocked road\n"
            "- Construction\n"
            "- Fire\n"
            "- Crowd\n"
            "- Other or unknown type of incident\n"
            "- No incident clearly visible from image"
        ),
        "tool": {
            "type": "function",
            "function": {
                "name": "classify_image",
                "description": "Report which incident types are visible in the image.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "crash":             {"type": "boolean", "description": "A crash or collision is visible"},
                        "pulled_over":       {"type": "boolean", "description": "A pulled-over vehicle is visible"},
                        "blocked_road":      {"type": "boolean", "description": "A blocked or closed road is visible"},
                        "construction":      {"type": "boolean", "description": "Construction activity is visible"},
                        "fire":              {"type": "boolean", "description": "Fire or smoke is visible"},
                        "crowd":             {"type": "boolean", "description": "A crowd of people is visible"},
                        "other_unknown":     {"type": "boolean", "description": "Other or unknown type of incident"},
                        "no_incident":       {"type": "boolean", "description": "No incident clearly visible from image"},
                        "reasoning":         {"type": "string",  "description": "Brief explanation of the visible content"},
                    },
                    "required": ["crash", "pulled_over", "blocked_road", "construction", "fire", "crowd", "other_unknown", "no_incident", "reasoning"],
                },
            },
        },
        "fields": ["crash", "pulled_over", "blocked_road", "construction", "fire", "crowd", "other_unknown", "no_incident"],
    },
}

BASE_FIELDS = ["row", "image_ref", "original_text", "question", "model", "model_used"]
TAIL_FIELDS = ["reasoning", "latency_ms", "error"]


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def image_content_from_file(image_path: Path) -> dict[str, Any]:
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(image_path.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def image_content_from_url(url: str) -> dict[str, Any]:
    # Download locally and base64-encode so the proxy never needs to fetch the URL
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


# ---------------------------------------------------------------------------
# Per-image test
# ---------------------------------------------------------------------------


def test_image(
    client: openai.OpenAI,
    image_content: dict[str, Any],
    model: str,
    question: str,
    row: int,
    image_ref: str,
    original_text: str,
) -> dict[str, Any]:
    q = QUESTIONS[question]
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [image_content, {"type": "text", "text": q["prompt"]}]}],
        tools=[q["tool"]],
        tool_choice={"type": "function", "function": {"name": "classify_image"}},
        max_tokens=512,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)

    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError(f"Model did not call tool. finish_reason={response.choices[0].finish_reason}")

    args: dict[str, Any] = json.loads(tool_calls[0].function.arguments)
    result: dict[str, Any] = {
        "row": row, "image_ref": image_ref, "original_text": original_text,
        "question": question, "model": model, "model_used": response.model,
    }
    for f in q["fields"]:
        result[f] = args.get(f, False)
    result["reasoning"] = args.get("reasoning", "")
    result["latency_ms"] = latency_ms
    result["error"] = ""
    return result


def check_proxy_connectivity(client: openai.OpenAI, model: str) -> None:
    """Send a minimal text-only request to confirm the proxy is reachable before processing images."""
    print(f"Checking proxy connectivity with model {model!r} ...", end=" ", flush=True)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=5,
        )
        reply = (response.choices[0].message.content or "").strip()
        print(f"OK (got: {reply!r})")
    except Exception as exc:
        print(f"FAILED\n  {type(exc).__name__}: {exc}")
        print("\nFull traceback:")
        traceback.print_exc()
        print(
            "\nPossible causes:\n"
            "  1. Not on Dataminr VPN — proxy is internal-only\n"
            "  2. ANTHROPIC_AUTH_TOKEN is wrong or expired\n"
            "  3. Model alias not deployed on this proxy\n"
            "  4. Proxy URL is wrong (check --proxy-url)\n",
            file=sys.stderr,
        )
        sys.exit(1)


def error_row(row: int, image_ref: str, original_text: str, question: str, model: str, exc: Exception) -> dict[str, Any]:
    q = QUESTIONS[question]
    result: dict[str, Any] = {
        "row": row, "image_ref": image_ref, "original_text": original_text,
        "question": question, "model": model, "model_used": "",
    }
    for f in q["fields"]:
        result[f] = False
    result["reasoning"] = ""
    result["latency_ms"] = 0
    result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# Input sources
# ---------------------------------------------------------------------------


def load_from_csv(csv_path: Path, limit: int) -> list[dict[str, Any]]:
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row_dict in enumerate(reader, start=1):
            url = (row_dict.get(CSV_URL_COLUMN) or "").strip()
            if not url:
                continue
            records.append({"row": i, "image_ref": url,
                             "original_text": (row_dict.get("Original Text") or "").strip()})
            if limit and len(records) >= limit:
                break
    return records


def load_from_dir(images_dir: Path, limit: int) -> list[dict[str, Any]]:
    files = sorted(p for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
    if limit:
        files = files[:limit]
    return [{"row": i, "image_ref": str(p), "original_text": ""} for i, p in enumerate(files, start=1)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Test images via LiteLLM proxy")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--images-dir", help="Folder of local image files")
    source.add_argument("--csv", help=f'CSV file with a "{CSV_URL_COLUMN}" column')

    parser.add_argument("--question", choices=["q1", "q2"], default="q1",
                        help="q1=emergency vehicles (default), q2=incident type")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, metavar="MODEL",
                        help="Model IDs to test")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max images to test (default: 10; use 0 for all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV filename (default: results_<question>_<timestamp>.csv)")
    parser.add_argument("--proxy-url", default=LITELLM_PROXY_URL,
                        help=f"LiteLLM proxy base URL (default: {LITELLM_PROXY_URL})")
    args = parser.parse_args()

    q = QUESTIONS[args.question]
    output_file = args.output or f"results_{args.question}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    csv_fields = BASE_FIELDS + q["fields"] + TAIL_FIELDS

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        print("ERROR: Set ANTHROPIC_AUTH_TOKEN (key from https://llm-proxy.ai.use1.test.dmnr.io/ui)",
              file=sys.stderr)
        sys.exit(1)

    client = openai.OpenAI(base_url=args.proxy_url, api_key=api_key)

    # Fail fast: confirm proxy is reachable before downloading images
    check_proxy_connectivity(client, args.models[0])

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_file():
            print(f"ERROR: {csv_path} not found", file=sys.stderr)
            sys.exit(1)
        records = load_from_csv(csv_path, args.limit)
        use_urls = True
    else:
        images_dir = Path(args.images_dir)
        if not images_dir.is_dir():
            print(f"ERROR: {images_dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        records = load_from_dir(images_dir, args.limit)
        use_urls = False

    if not records:
        print("No images found.", file=sys.stderr)
        sys.exit(1)

    print(f"Question: {args.question}")
    print(f"Images:   {len(records)}")
    print(f"Models:   {args.models}")
    print(f"Output:   {output_file}\n")

    total = len(records) * len(args.models)
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=csv_fields)
        writer.writeheader()

        with tqdm(total=total, unit="req") as pbar:
            for rec in records:
                image_content = (
                    image_content_from_url(rec["image_ref"]) if use_urls
                    else image_content_from_file(Path(rec["image_ref"]))
                )
                for model in args.models:
                    pbar.set_postfix(row=rec["row"], model=model.split("/")[-1][:18])
                    try:
                        result_row = test_image(client, image_content, model,
                                                args.question, rec["row"],
                                                rec["image_ref"], rec["original_text"])
                    except Exception as exc:
                        tqdm.write(f"  ERR row={rec['row']} model={model}: {type(exc).__name__}: {exc}")
                        result_row = error_row(rec["row"], rec["image_ref"],
                                               rec["original_text"], args.question, model, exc)
                    writer.writerow(result_row)
                    csvfile.flush()
                    pbar.update(1)

    print(f"\nDone. Results saved to: {output_file}")


if __name__ == "__main__":
    main()
