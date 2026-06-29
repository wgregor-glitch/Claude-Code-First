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
import re
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
            "Check all incident types visible in this image.\n\n"
            "☑ Crash\n"
            "☑ Pulled over vehicle\n"
            "☑ Blocked road\n"
            "☑ Construction\n"
            "☑ Fire\n"
            "☑ Crowd\n"
            "○ Identifiable incident, but not one of the categories above\n"
            "○ No incident clearly visible from image\n\n"
            "Rules:\n"
            "- You may check any combination of the top six types.\n"
            "- The bottom two are exclusive — selecting either means none of the top six "
            "can also be selected, and these two cannot be selected together."
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
                        "other_unknown":     {"type": "boolean", "description": "An identifiable incident is visible but does not fit any of the six categories above"},
                        "no_incident":       {"type": "boolean", "description": "No incident clearly visible from image — exclusive, cannot combine with any other option"},
                        "reasoning":         {"type": "string",  "description": "Brief explanation of the visible content"},
                    },
                    "required": ["crash", "pulled_over", "blocked_road", "construction", "fire", "crowd", "other_unknown", "no_incident", "reasoning"],
                },
            },
        },
        "fields": ["crash", "pulled_over", "blocked_road", "construction", "fire", "crowd", "other_unknown", "no_incident"],
    },
}

CAPTION_PROMPT = """\
Analyze the traffic camera image and generate exactly one caption.

Task

Determine:

VEHICLE_PHRASE
INCIDENT_PHRASE

Then generate:

[VEHICLE_PHRASE] detected responding to [INCIDENT_PHRASE]
VEHICLE_PHRASE

Identify all visible emergency vehicles.

Possible vehicle types:

Police vehicle
Fire truck
Ambulance
Emergency vehicle (type unclear)

Selection logic:

If multiple emergency vehicle types are visible, use "Emergency vehicles".
If only police vehicles are visible:
1 vehicle → "Police vehicle"
2+ vehicles → "Police vehicles"
If only fire trucks are visible:
1 vehicle → "Fire truck"
2+ vehicles → "Fire trucks"
If only ambulances are visible:
1 vehicle → "Ambulance"
2+ vehicles → "Ambulances"
If emergency vehicles are visible but the type cannot be determined:
1 vehicle → "Emergency vehicle"
2+ vehicles → "Emergency vehicles"

Only assign a specific vehicle type when clearly visible.

If uncertain, use the most generic valid option.

INCIDENT_PHRASE

Identify all visible incident categories.

Possible categories:

Crash
Pulled-over vehicle
Blocked road
Construction
Fire
Other identifiable incident

Selection logic:

1 category → use that category name.
2 categories → join with "and".
3 or more categories → use "incidents".
Other identifiable incident → use "incident".

Examples:

crash
blocked road
crash and fire
incident
incidents

Company Involvement

Only mention a company when both conditions are true:

The company name or logo is clearly visible.
The incident directly involves or impacts a company asset.

Examples of company assets:

Branded vehicle
Store
Restaurant
Gas station
Warehouse
Commercial property

When both conditions are met, replace INCIDENT_PHRASE with a concise company-specific description.

Examples:

overturned Amazon truck
incident outside McDonald's
fire at Shell station
crash involving FedEx truck

Do not mention a company if its logo is visible but unrelated to the incident.

Priority Order

Apply these instructions from highest priority to lowest priority:

Company-specific incident description
Multiple emergency vehicle types → "Emergency vehicles"
Vehicle count singular/plural selection
Incident categorization
Generic fallback values
General Guidance
Base conclusions only on visible evidence.
Do not infer details that are not clearly visible.
When uncertain, select the most generic valid option.
Output Requirements
Return exactly one caption.
Output only the caption.
Do not include explanations, labels, confidence scores, reasoning, or additional text.

Output format:

[VEHICLE_PHRASE] detected responding to [INCIDENT_PHRASE]
Only output NO_EMERGENCY_VEHICLE_VISIBLE when no emergency vehicle is visible in the image."""

BASE_FIELDS = ["row", "image_ref", "original_text", "question", "model", "model_used"]
TAIL_FIELDS = ["reasoning", "latency_ms", "error"]
CAPTION_FIELDS = ["row", "image_ref", "original_text", "question", "model", "model_used", "caption", "latency_ms", "error"]

# Mapping from internal tool field names → exact CSV column names for the annotate question
ANNOTATE_FIELD_MAP: dict[str, str] = {
    "police_1":              "police (1)",
    "police_2plus":          "police vehicles (2 or more)",
    "fire_truck_1":          "fire_truck (1)",
    "fire_truck_2plus":      "fire trucks (2 or more)",
    "ambulance_1":           "ambulance (1)",
    "ambulance_2plus":       "ambulances (2 or more)",
    "no_emergency_vehicles": "no_emergency_vehicles",
    "hard_to_tell":          "hard_to_tell",
    "crash":                 "crash",
    "pulled_over":           "pulled_over",
    "blocked_road":          "blocked_road",
    "construction":          "construction",
    "fire":                  "fire",
    "crowd":                 "crowd",
    "other_identifiable":    "identifiable_incident, not one of the categories above",
    "no_incident_visible":   "no_incident_visible",
}

QUESTIONS["annotate"] = {
    "prompt": (
        "Analyze this traffic camera image and classify both emergency vehicles and incident types.\n\n"
        "PART 1 — Emergency vehicles\n\n"
        "For each type, choose singular OR plural — not both:\n"
        "  ☑ Police vehicle (exactly 1 clearly visible)\n"
        "  ☑ Police vehicles (2 or more clearly visible)\n"
        "  ☑ Fire truck (exactly 1 clearly visible)\n"
        "  ☑ Fire trucks (2 or more clearly visible)\n"
        "  ☑ Ambulance (exactly 1 clearly visible)\n"
        "  ☑ Ambulances (2 or more clearly visible)\n"
        "  ○ No emergency vehicles\n"
        "  ○ Hard to tell\n\n"
        "Counting rules:\n"
        "- Count a vehicle if you can see its body clearly — even if partially cropped at the image edge, "
        "as long as you can identify it as a distinct vehicle.\n"
        "- Do NOT count light reflections, light bars, or glowing patterns as separate vehicles.\n"
        "- Do NOT count the same vehicle twice from different angles.\n"
        "- Only upgrade to plural (2+) if you can see a second vehicle body distinctly — not just inferred "
        "from extra lights or a blurry background shape. If in doubt between 1 and 2, choose singular.\n"
        "- 'No emergency vehicles' and 'Hard to tell' are exclusive — selecting either means no vehicle "
        "boxes can also be selected, and these two cannot be selected together.\n\n"
        "PART 2 — Incident type\n\n"
        "Definitions:\n"
        "- Pulled over vehicle: a civilian vehicle is stationary on the road shoulder or verge with a "
        "police vehicle stopped directly behind or alongside it. Both vehicles must be parked/stopped — "
        "a police car driving past or stationed at an intersection does NOT count as a traffic stop.\n"
        "- Blocked road: a travel lane or the full road is physically obstructed so through-traffic cannot "
        "pass — requires explicit evidence: cones or barriers spanning a lane, crash debris in the road, "
        "or an emergency vehicle stopped across a travel lane. A police car parked on the shoulder with "
        "traffic still flowing past is NOT a blocked road.\n"
        "- Identifiable incident, not one of the above: use this when something is clearly happening "
        "(e.g. a police vehicle with lights on at an intersection, a utility vehicle working, a stalled "
        "car with no police present) but it does not fit crash, pull-over, blocked road, construction, "
        "fire, or crowd.\n"
        "- Pulled over vehicle AND blocked road CAN both be true simultaneously "
        "(e.g. a traffic stop that is also blocking a travel lane).\n\n"
        "  ☑ Crash\n"
        "  ☑ Pulled over vehicle\n"
        "  ☑ Blocked road\n"
        "  ☑ Construction\n"
        "  ☑ Fire\n"
        "  ☑ Crowd\n"
        "  ○ Identifiable incident, but not one of the categories above\n"
        "  ○ No incident clearly visible from image\n\n"
        "Rules:\n"
        "- You may check any combination of the top six incident types.\n"
        "- The bottom two are exclusive — selecting either means none of the top six can also be "
        "selected, and these two cannot be selected together.\n"
        "- Prefer 'Identifiable incident, not one of the categories above' over 'No incident clearly "
        "visible' whenever there is clearly an emergency vehicle present with lights on, even if you "
        "cannot determine the specific incident type."
    ),
    "tool": {
        "type": "function",
        "function": {
            "name": "classify_image",
            "description": "Classify emergency vehicles and incident types visible in the image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "police_1":              {"type": "boolean", "description": "Exactly 1 police vehicle visible"},
                    "police_2plus":          {"type": "boolean", "description": "2 or more police vehicles visible"},
                    "fire_truck_1":          {"type": "boolean", "description": "Exactly 1 fire truck visible"},
                    "fire_truck_2plus":      {"type": "boolean", "description": "2 or more fire trucks visible"},
                    "ambulance_1":           {"type": "boolean", "description": "Exactly 1 ambulance visible"},
                    "ambulance_2plus":       {"type": "boolean", "description": "2 or more ambulances visible"},
                    "no_emergency_vehicles": {"type": "boolean", "description": "No emergency vehicles present — exclusive"},
                    "hard_to_tell":          {"type": "boolean", "description": "Cannot determine — exclusive, last resort"},
                    "crash":                 {"type": "boolean", "description": "A crash or collision is visible"},
                    "pulled_over":           {"type": "boolean", "description": "A pulled-over vehicle is visible"},
                    "blocked_road":          {"type": "boolean", "description": "A travel lane is physically blocked by cones, barriers, crash debris, or a vehicle spanning the lane — NOT merely a police car parked on the shoulder"},
                    "construction":          {"type": "boolean", "description": "Construction activity is visible"},
                    "fire":                  {"type": "boolean", "description": "Fire or smoke is visible"},
                    "crowd":                 {"type": "boolean", "description": "A crowd of people is visible"},
                    "other_identifiable":    {"type": "boolean", "description": "An identifiable incident not in the above categories — exclusive"},
                    "no_incident_visible":   {"type": "boolean", "description": "No incident clearly visible — exclusive"},
                    "reasoning":             {"type": "string",  "description": "Brief explanation of visible content"},
                },
                "required": [
                    "police_1", "police_2plus", "fire_truck_1", "fire_truck_2plus",
                    "ambulance_1", "ambulance_2plus", "no_emergency_vehicles", "hard_to_tell",
                    "crash", "pulled_over", "blocked_road", "construction", "fire", "crowd",
                    "other_identifiable", "no_incident_visible", "reasoning",
                ],
            },
        },
    },
    "fields": list(ANNOTATE_FIELD_MAP.keys()),
    "field_map": ANNOTATE_FIELD_MAP,
}


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


def _parse_result(args: dict[str, Any], row: int, image_ref: str, original_text: str,
                  question: str, model: str, model_used: str, latency_ms: int) -> dict[str, Any]:
    q = QUESTIONS[question]
    field_map: dict[str, str] = q.get("field_map", {})
    result: dict[str, Any] = {
        "row": row, "image_ref": image_ref, "original_text": original_text,
        "question": question, "model": model, "model_used": model_used,
    }
    for f in q["fields"]:
        csv_col = field_map.get(f, f)
        result[csv_col] = args.get(f, False)
    result["reasoning"] = args.get("reasoning", "")
    result["latency_ms"] = latency_ms
    result["error"] = ""
    return result


def _test_image_tool(client: openai.OpenAI, image_content: dict[str, Any], model: str,
                     question: str, row: int, image_ref: str, original_text: str) -> dict[str, Any]:
    q = QUESTIONS[question]
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [image_content, {"type": "text", "text": q["prompt"]}]}],
        tools=[q["tool"]],
        tool_choice={"type": "function", "function": {"name": "classify_image"}},
        max_tokens=512,
        timeout=90,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError(f"Model did not call tool. finish_reason={response.choices[0].finish_reason}")
    args: dict[str, Any] = json.loads(tool_calls[0].function.arguments)
    return _parse_result(args, row, image_ref, original_text, question, model, response.model, latency_ms)


def _test_image_json(client: openai.OpenAI, image_content: dict[str, Any], model: str,
                     question: str, row: int, image_ref: str, original_text: str) -> dict[str, Any]:
    """Fallback for models that don't support forced tool calling — ask for raw JSON instead."""
    q = QUESTIONS[question]
    fields_schema = "\n".join(f'  "{f}": true | false' for f in q["fields"])
    json_prompt = (
        f"{q['prompt']}\n\n"
        f"Respond with ONLY a JSON object — no markdown, no explanation outside the JSON:\n"
        f"{{\n{fields_schema},\n  \"reasoning\": \"brief explanation\"\n}}"
    )
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [image_content, {"type": "text", "text": json_prompt}]}],
        max_tokens=512,
        timeout=90,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)
    content = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise RuntimeError(f"No JSON object found in response: {content[:300]}")
    args: dict[str, Any] = json.loads(match.group())
    return _parse_result(args, row, image_ref, original_text, question, model, response.model, latency_ms)


def test_image(
    client: openai.OpenAI,
    image_content: dict[str, Any],
    model: str,
    question: str,
    row: int,
    image_ref: str,
    original_text: str,
) -> dict[str, Any]:
    if question == "caption":
        return _test_image_caption(client, image_content, model, row, image_ref, original_text)
    try:
        return _test_image_tool(client, image_content, model, question, row, image_ref, original_text)
    except openai.BadRequestError as exc:
        if "tool_choice" in str(exc) or "tool-call-parser" in str(exc):
            return _test_image_json(client, image_content, model, question, row, image_ref, original_text)
        raise


def _test_image_caption(client: openai.OpenAI, image_content: dict[str, Any], model: str,
                        row: int, image_ref: str, original_text: str) -> dict[str, Any]:
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [image_content, {"type": "text", "text": CAPTION_PROMPT}]}],
        max_tokens=128,
        timeout=90,
    )
    latency_ms = round((time.monotonic() - t0) * 1000)
    caption = (response.choices[0].message.content or "").strip()
    return {
        "row": row, "image_ref": image_ref, "original_text": original_text,
        "question": "caption", "model": model, "model_used": response.model,
        "caption": caption, "latency_ms": latency_ms, "error": "",
    }


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
    err = f"{type(exc).__name__}: {exc}"
    if question == "caption":
        return {
            "row": row, "image_ref": image_ref, "original_text": original_text,
            "question": question, "model": model, "model_used": "",
            "caption": "", "latency_ms": 0, "error": err,
        }
    q = QUESTIONS[question]
    field_map: dict[str, str] = q.get("field_map", {})
    result: dict[str, Any] = {
        "row": row, "image_ref": image_ref, "original_text": original_text,
        "question": question, "model": model, "model_used": "",
    }
    for f in q["fields"]:
        result[field_map.get(f, f)] = False
    result["reasoning"] = ""
    result["latency_ms"] = 0
    result["error"] = err
    return result


# ---------------------------------------------------------------------------
# Input sources
# ---------------------------------------------------------------------------


def _col_letter_to_index(col: str):
    """Convert a spreadsheet column letter (A, B, C...) to 0-based index, or None if not a letter."""
    if col.isalpha() and len(col) <= 2:
        idx = 0
        for ch in col.upper():
            idx = idx * 26 + (ord(ch) - ord('A') + 1)
        return idx - 1
    return None


def load_from_csv(csv_path: Path, limit: int, url_column: str = CSV_URL_COLUMN, skip_rows: int = 0) -> list[dict[str, Any]]:
    col_index = _col_letter_to_index(url_column)
    records = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for _ in range(skip_rows):
            next(f)
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if col_index is not None:
            if col_index >= len(fieldnames):
                print(f"ERROR: column letter '{url_column}' (index {col_index}) out of range; CSV has {len(fieldnames)} columns: {fieldnames}", file=sys.stderr)
                sys.exit(1)
            resolved_col = fieldnames[col_index]
        else:
            resolved_col = url_column
        for i, row_dict in enumerate(reader, start=1):
            url = (row_dict.get(resolved_col) or "").strip()
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

    parser.add_argument("--question", choices=["q1", "q2", "caption", "annotate"], default="q1",
                        help="q1=emergency vehicles (default), q2=incident type, caption=generate caption, annotate=full Q1+Q2 boolean grid")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, metavar="MODEL",
                        help="Model IDs to test")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max images to test (default: 10; use 0 for all)")
    parser.add_argument("--output", default=None,
                        help="Output CSV filename (default: results_<question>_<timestamp>.csv)")
    parser.add_argument("--proxy-url", default=LITELLM_PROXY_URL,
                        help=f"LiteLLM proxy base URL (default: {LITELLM_PROXY_URL})")
    parser.add_argument("--url-column", default=CSV_URL_COLUMN,
                        help=f'CSV column containing image URLs (default: "{CSV_URL_COLUMN}")')
    parser.add_argument("--skip-rows", type=int, default=0,
                        help="Rows to skip before the header row (default: 0)")
    args = parser.parse_args()

    output_file = args.output or f"results_{args.question}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    if args.question == "caption":
        csv_fields = CAPTION_FIELDS
    else:
        q = QUESTIONS[args.question]
        field_map = q.get("field_map", {})
        csv_col_names = [field_map.get(f, f) for f in q["fields"]]
        csv_fields = BASE_FIELDS + csv_col_names + TAIL_FIELDS

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
        records = load_from_csv(csv_path, args.limit, args.url_column, args.skip_rows)
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
