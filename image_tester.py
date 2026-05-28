"""
Image tester for LiteLLM proxy with Okta OIDC authentication.

Tests images against multiple vision models and saves results to CSV.
Default run uses --limit 10 for prompt refinement; set --limit 0 for all rows.

Modes:
  Local files:  --images-dir /path/to/images
  CSV URLs:     --csv /path/to/file.csv  (reads "Source Media URL" column)

Usage:
    export OKTA_CLIENT_SECRET="<your-secret>"
    python image_tester.py --images-dir /path/to/images
    python image_tester.py --csv dataminr_alerts.csv --limit 10
"""

import argparse
import base64
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import openai
import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_PROXY_URL = "https://llm-proxy-test.dataminr.com/"

OKTA_TOKEN_URL = "https://dmcorp.okta.com/oauth2/v1/token"
OKTA_CLIENT_ID = "0oatzxv6svJy67qZz697"

DEFAULT_MODELS = [
    "gpt-4o",
    "claude-3-5-sonnet-20241022",
    "gemini/gemini-1.5-pro",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Column name in the input CSV that holds image URLs
CSV_URL_COLUMN = "Source Media URL"

IMAGE_PROMPT = (
    "Are any of the following types of emergency response vehicles in the image? "
    "Select all that apply:\n"
    "- Police\n"
    "- Fire Truck\n"
    "- Ambulance\n"
    "- Clearly no emergency vehicles\n"
    "- Hard to tell based on what's visible"
)

VEHICLE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "detect_emergency_vehicles",
        "description": "Report which emergency response vehicle types are visible in the image.",
        "parameters": {
            "type": "object",
            "properties": {
                "police": {
                    "type": "boolean",
                    "description": "Police vehicle(s) are visible",
                },
                "fire_truck": {
                    "type": "boolean",
                    "description": "Fire truck(s) are visible",
                },
                "ambulance": {
                    "type": "boolean",
                    "description": "Ambulance(s) are visible",
                },
                "no_emergency_vehicles": {
                    "type": "boolean",
                    "description": "Clearly no emergency vehicles present",
                },
                "hard_to_tell": {
                    "type": "boolean",
                    "description": "Hard to tell based on what is visible",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation of the visible content",
                },
            },
            "required": [
                "police",
                "fire_truck",
                "ambulance",
                "no_emergency_vehicles",
                "hard_to_tell",
                "reasoning",
            ],
        },
    },
}

CSV_FIELDS = [
    "row",
    "image_ref",
    "original_text",
    "model",
    "model_used",
    "police",
    "fire_truck",
    "ambulance",
    "no_emergency_vehicles",
    "hard_to_tell",
    "reasoning",
    "latency_ms",
    "error",
]


# ---------------------------------------------------------------------------
# Okta authentication
# ---------------------------------------------------------------------------


def get_okta_token(client_secret: str, scope: str = "") -> str:
    payload: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": OKTA_CLIENT_ID,
        "client_secret": client_secret,
    }
    if scope:
        payload["scope"] = scope
    resp = requests.post(
        OKTA_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Image sources
# ---------------------------------------------------------------------------


def image_content_from_file(image_path: Path) -> dict[str, Any]:
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    mime = mime_map.get(image_path.suffix.lower(), "image/jpeg")
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def image_content_from_url(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


# ---------------------------------------------------------------------------
# Per-image test
# ---------------------------------------------------------------------------


def test_image(
    client: openai.OpenAI,
    image_content: dict[str, Any],
    model: str,
    row: int,
    image_ref: str,
    original_text: str,
) -> dict[str, Any]:
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    image_content,
                    {"type": "text", "text": IMAGE_PROMPT},
                ],
            }
        ],
        tools=[VEHICLE_TOOL],
        tool_choice={"type": "function", "function": {"name": "detect_emergency_vehicles"}},
        max_tokens=512,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)

    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError(f"Model did not call tool. finish_reason={response.choices[0].finish_reason}")

    args: dict[str, Any] = json.loads(tool_calls[0].function.arguments)
    return {
        "row": row,
        "image_ref": image_ref,
        "original_text": original_text,
        "model": model,
        "model_used": response.model,
        "police": args.get("police", False),
        "fire_truck": args.get("fire_truck", False),
        "ambulance": args.get("ambulance", False),
        "no_emergency_vehicles": args.get("no_emergency_vehicles", False),
        "hard_to_tell": args.get("hard_to_tell", False),
        "reasoning": args.get("reasoning", ""),
        "latency_ms": latency_ms,
        "error": "",
    }


def error_row(row: int, image_ref: str, original_text: str, model: str, exc: Exception) -> dict[str, Any]:
    return {
        "row": row,
        "image_ref": image_ref,
        "original_text": original_text,
        "model": model,
        "model_used": "",
        "police": False,
        "fire_truck": False,
        "ambulance": False,
        "no_emergency_vehicles": False,
        "hard_to_tell": False,
        "reasoning": "",
        "latency_ms": 0,
        "error": str(exc),
    }


# ---------------------------------------------------------------------------
# Input sources
# ---------------------------------------------------------------------------


def load_from_csv(csv_path: Path, limit: int) -> list[dict[str, str]]:
    """Return list of {row, image_ref, original_text} dicts from CSV file."""
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row_dict in enumerate(reader, start=1):
            url = (row_dict.get(CSV_URL_COLUMN) or "").strip()
            if not url:
                continue
            records.append({
                "row": i,
                "image_ref": url,
                "original_text": (row_dict.get("Original Text") or "").strip(),
            })
            if limit and len(records) >= limit:
                break
    return records


def load_from_dir(images_dir: Path, limit: int) -> list[dict[str, str]]:
    """Return list of {row, image_ref, original_text} dicts from image directory."""
    files = sorted(p for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
    if limit:
        files = files[:limit]
    return [
        {"row": i, "image_ref": str(p), "original_text": ""}
        for i, p in enumerate(files, start=1)
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test images for emergency vehicles via LiteLLM proxy"
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--images-dir", help="Folder of local image files")
    source.add_argument("--csv", help=f'CSV file with a "{CSV_URL_COLUMN}" column')

    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        metavar="MODEL",
        help="Model IDs to test (default: gpt-4o, claude-3-5-sonnet-20241022, gemini/gemini-1.5-pro)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max images to test (default: 10; use 0 for all)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV filename (default: results_<timestamp>.csv)",
    )
    parser.add_argument(
        "--proxy-url",
        default=LITELLM_PROXY_URL,
        help=f"LiteLLM proxy base URL (default: {LITELLM_PROXY_URL})",
    )
    parser.add_argument(
        "--okta-scope",
        default=os.environ.get("OKTA_SCOPE", ""),
        help="Okta scope for client-credentials token (env: OKTA_SCOPE)",
    )
    args = parser.parse_args()

    output_file = args.output or f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # Okta auth
    client_secret = os.environ.get("OKTA_CLIENT_SECRET")
    if not client_secret:
        print("ERROR: OKTA_CLIENT_SECRET environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print("Obtaining Okta access token...")
    try:
        token = get_okta_token(client_secret, scope=args.okta_scope)
    except requests.HTTPError as exc:
        print(f"ERROR: Okta token request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Token obtained.\n")

    client = openai.OpenAI(base_url=args.proxy_url, api_key=token)

    # Load image records
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

    print(f"Images:  {len(records)}")
    print(f"Models:  {args.models}")
    print(f"Output:  {output_file}\n")

    total = len(records) * len(args.models)
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDS)
        writer.writeheader()

        with tqdm(total=total, unit="req") as pbar:
            for rec in records:
                image_content = (
                    image_content_from_url(rec["image_ref"])
                    if use_urls
                    else image_content_from_file(Path(rec["image_ref"]))
                )
                for model in args.models:
                    pbar.set_postfix(row=rec["row"], model=model.split("/")[-1][:18])
                    try:
                        result_row = test_image(
                            client,
                            image_content,
                            model,
                            rec["row"],
                            rec["image_ref"],
                            rec["original_text"],
                        )
                    except Exception as exc:
                        result_row = error_row(
                            rec["row"], rec["image_ref"], rec["original_text"], model, exc
                        )
                    writer.writerow(result_row)
                    csvfile.flush()
                    pbar.update(1)

    print(f"\nDone. Results saved to: {output_file}")


if __name__ == "__main__":
    main()
