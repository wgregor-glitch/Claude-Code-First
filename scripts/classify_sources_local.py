#!/usr/bin/env python3
"""
Classify social media entities into official source channel types via the
Dataminr LiteLLM proxy, and merge them into a running entities_classified.csv
master. Self-contained: Python 3.9+ standard library only, nothing to install.

This is the run-it-locally (on the Dataminr network / VPN) counterpart of
scripts/classify_new_drop.py, which uses the Anthropic Batches API instead.

Usage:
    export LLM_PROXY_API_KEY=sk-...        # or it will prompt (hidden input)
    python3 classify_sources_local.py new_export.csv [more_exports.csv ...]

Options:
    --master PATH   running master CSV (default: ./entities_classified.csv)
    --model NAME    model alias on the proxy (default: claude-haiku-4-5)
    --proxy URL     proxy base URL (default: https://llm-proxy-test.dataminr.com)
    --workers N     concurrent requests (default: 8)

Each input CSV must have columns: ENTITY_NAME, ID, LINK, VERIFIED.
Dedupes by ID against the master, so already-classified entities are never
re-sent. Results are appended to the master as they complete, so if the run
is interrupted just re-run the same command — it picks up where it left off.
"""

import argparse
import csv
import getpass
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_PROXY = "https://llm-proxy-test.dataminr.com"
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 256
MAX_RETRIES = 5

COL_NAME     = "ENTITY_NAME"
COL_ID       = "ID"
COL_LINK     = "LINK"
COL_VERIFIED = "VERIFIED"

OUTPUT_FIELDS = ['link', 'id', 'display_name', 'entity_name', 'external_source_description', 'verified', 'channels', 'city', 'state_province', 'country', 'confidence']

SYSTEM_PROMPT = 'You are an expert media and source classifier. Given an entity name and URL, return a JSON object with the following fields:\n\nchannels: Classify the entity into exactly one of these official channel types:\n  - news        : Broadcast, print, or major digital source focused on news at a national or international level\n  - localnews   : Broadcast or digital source focused on news at the municipal, regional, or state level\n  - reporter    : Journalist contributing to a specific major news outlet or major blog\n  - blog        : A digital source or the digital piece of a print magazine that breaks news\n  - majorblog   : A digital source (or the digital piece of a major print magazine) that breaks news and is a leading source in a particular topic area\n  - gov         : Federal, state, or local government organization (office, department, agency) or official person in Government — NOT including emergency services\n  - emergency   : Any agency or department whose primary function is emergency response: Police, Sheriff, Fire Department, EMS, 911, OEM, Homeland Security, Coast Guard, National Guard\n  - ngo         : Any non-profit citizens group organized at a local, national, or international level\n  - university  : Accredited colleges, universities, and their associated accounts\n  - unknown     : Cannot be determined from the information provided\n\nIMPORTANT PRIORITY RULES:\n\nnews vs. localnews — this is the most important distinction:\n- "news" is reserved for a very small set of the most globally recognized news organizations — roughly the top 50 in the world. These are outlets that are household names across multiple continents, with large international audiences and global editorial footprints.\n- The ONLY outlets that qualify as "news" are organizations equivalent to: BBC, CNN, Fox News, MSNBC, ABC News (US), NBC News, CBS News, Reuters, Associated Press (AP), AFP, Al Jazeera, Bloomberg, The New York Times, The Washington Post, The Wall Street Journal, The Guardian, The Telegraph, Financial Times, Sky News, CNBC, NPR, NHK, CGTN, TASS, Xinhua, Yonhap, CBC, SBS (Australia), RFI, Politico, ESPN, The Weather Channel, and their direct sub-accounts/verticals (e.g. Reuters UK, BBC Business, Fox News Politics, Bloomberg Markets, NYT World).\n- If the outlet is NOT on that level of global recognition — even if it is a well-known national outlet in its own country — classify it as "localnews".\n- "localnews" is the DEFAULT. Any outlet that is national-only, regional, city-level, or whose global recognition is uncertain must be "localnews".\n- WHEN IN DOUBT, ALWAYS CHOOSE "localnews" OVER "news". It is far better to under-classify than to over-classify.\n\nOfficial vs. community sources:\n- "emergency" is ONLY for verified, official government emergency response agencies. They will almost always include a specific jurisdiction (city, county, state, or country) in their name or URL.\n- Community pages, fan accounts, enthusiast groups, or aggregators that cover emergency/police topics but are NOT official agencies must be classified as "blog" instead.\n- If the name contains words like "Live", "Action", "News", "Updates", "Community", "Scanner", or looks informal, treat it as "blog" — not "emergency" or "gov".\n- If the account name looks unusual, abbreviated in a non-official way, or lacks a clear jurisdiction, lower your confidence accordingly.\n\nOther rules:\n- Use "gov" only for non-emergency government entities (e.g. city hall, legislature, tax office, public health department, elected officials).\n- Use "reporter" only for individual journalists, not publications.\n\nVerification status:\n- "Platform Verified: true" means the account is officially verified by the platform (e.g. Facebook blue check). Use this as a supporting signal for official classifications (gov, emergency, news, localnews).\n- "Platform Verified: false" is a weak signal but relevant when classification is ambiguous — an unverified account claiming to be an official agency or major outlet should lower your confidence. Default to "unknown" if the name looks official but the account is unverified and you cannot confirm it from your training knowledge.\n\nConfidence calibration:\n- "high": You are certain of the classification based on a well-known name or clear URL.\n- "medium": The name and URL suggest a classification but there is some ambiguity.\n- "low": You are guessing. The name or URL is vague, unusual, or missing key signals.\n- If your confidence is "medium" or "low", set channels to "unknown" instead of guessing.\n\nEXAMPLES — news (major outlets only):\n  "BBC News" → news\n  "CNN Breaking News" → news\n  "Reuters UK" → news (sub-account of Reuters)\n  "BBC Business" → news (sub-account of BBC)\n  "Fox News Politics" → news (sub-account of Fox News)\n  "Bloomberg Markets" → news (sub-account of Bloomberg)\n  "NHK News" → news (major national broadcaster, Japan)\n  "CBC News" → news (major national broadcaster, Canada)\n  "Al Jazeera English" → news (major international outlet)\n  "TASS" → news (major national wire service, Russia)\n  "Yonhap News Agency" → news (major national wire service, South Korea)\n  "Politico" → news (major national political outlet, US)\n  "The Weather Channel" → news (major national outlet, US)\n\nEXAMPLES — localnews (everything else):\n  "Dallas Morning News" → localnews\n  "GMA News" (Philippines) → localnews (national but not globally recognized)\n  "ABS-CBN News" (Philippines) → localnews (national but not globally recognized)\n  "Punch Newspapers" (Nigeria) → localnews (national but not globally recognized)\n  "NewsNation" (US) → localnews (not globally recognized)\n  "GB News" (UK) → localnews (not globally recognized)\n  "NEWSMAX" (US) → localnews (not globally recognized at top-50 level)\n  "NMB News" (Nepal) → localnews\n  "SønderborgPortal" (Denmark) → localnews\n  "INRAI Noticias" (Colombia) → localnews\n  "ArtsakhPress Agency" (Armenia) → localnews\n  Any outlet serving a single city, region, or state → localnews\n  Any national outlet not on the explicit top-50 list above → localnews\n\nOTHER EXAMPLES:\n  "South African Police Service" → emergency (official, jurisdiction in name)\n  "NYC Fire Department" → emergency (official, jurisdiction in name)\n  "Los Angeles County Sheriff" → emergency (official, jurisdiction in name)\n  "CTPOLICELIVE" → blog (community/enthusiast account, no jurisdiction, informal name)\n  "Police1" → blog (aggregator/community, not an official agency)\n  "Police Action Live" → blog (informal name, not an official agency)\n  "City of Chicago" → gov\n  "U.S. Senate" → gov\n  "CDC" → gov\n  "TechCrunch" → majorblog\n  "Jane Smith, CNN Reporter" → reporter\n  "Red Cross" → ngo\n  "Harvard University" → university\n\nexternal_source_description: A 1-2 sentence factual description of what this entity is.\n\ncity: City inferred from the entity name or URL. Empty string if not determinable.\nstate_province: State or province inferred from the entity name or URL. Empty string if not determinable.\ncountry: Country inferred from the entity name or URL. Empty string if not determinable.\n\nconfidence: Your confidence in the channels classification — "high", "medium", or "low". Remember: if confidence is "medium" or "low", set channels to "unknown".\n\nRespond with valid JSON only. No markdown formatting, no code fences, no explanation — just the raw JSON object.'


def get_api_key() -> str:
    for var in ("LLM_PROXY_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY"):
        key = os.environ.get(var, "").strip()
        if key:
            print(f"Using API key from ${var}.")
            return key
    key = getpass.getpass("Enter your LLM proxy API key (input hidden): ").strip()
    if not key:
        sys.exit("No API key provided. Exiting.")
    return key


def strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def empty_result() -> dict:
    return {
        "channels": "unknown",
        "external_source_description": "",
        "city": "",
        "state_province": "",
        "country": "",
        "confidence": "low",
    }


def post_json(url: str, api_key: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def classify_one(chat_url: str, api_key: str, model: str, row: dict) -> dict:
    name     = row.get(COL_NAME,     "").strip()
    link     = row.get(COL_LINK,     "").strip()
    verified = row.get(COL_VERIFIED, "").strip()
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Entity Name: {name}\nURL: {link}\nPlatform Verified: {verified}"},
        ],
    }

    for attempt in range(MAX_RETRIES):
        try:
            data = post_json(chat_url, api_key, payload)
            text = strip_json(data["choices"][0]["message"]["content"])
            parsed = json.loads(text)
            channels = parsed.get("channels", "unknown")
            if isinstance(channels, list):
                channels = channels[0] if channels else "unknown"
            return {
                "channels":                    channels,
                "external_source_description": parsed.get("external_source_description", ""),
                "city":                        parsed.get("city", ""),
                "state_province":              parsed.get("state_province", ""),
                "country":                     parsed.get("country", ""),
                "confidence":                  parsed.get("confidence", "low"),
            }
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return empty_result()  # model replied but not with usable JSON
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
                time.sleep(min(delay, 60))
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} from proxy: {body}") from e
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    return empty_result()


def build_output_row(row: dict, result: dict) -> dict:
    return {
        "link":                        row.get(COL_LINK, ""),
        "id":                          row.get(COL_ID, "").strip(),
        "display_name":                row.get(COL_NAME, ""),
        "entity_name":                 row.get(COL_NAME, ""),
        "external_source_description": result["external_source_description"],
        "verified":                    row.get(COL_VERIFIED, ""),
        "channels":                    result["channels"],
        "city":                        result["city"],
        "state_province":              result["state_province"],
        "country":                     result["country"],
        "confidence":                  result["confidence"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csvs", nargs="+", help="new raw export CSV(s)")
    ap.add_argument("--master", default="entities_classified.csv")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    base = args.proxy.rstrip("/")
    chat_url = base + "/chat/completions"

    # Load master (may not exist yet).
    master_ids = set()
    if os.path.exists(args.master):
        with open(args.master, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                master_ids.add(row.get("id", "").strip())
        master_ids.discard("")
    print(f"Current master: {len(master_ids):,} unique entities")

    # Dedupe new files against master and each other.
    todo = {}
    for path in args.csvs:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = [c for c in (COL_NAME, COL_ID, COL_LINK, COL_VERIFIED)
                       if c not in (reader.fieldnames or [])]
            if missing:
                sys.exit(f"{path}: missing required column(s): {', '.join(missing)}")
            for row in reader:
                eid = (row.get(COL_ID) or "").strip()
                if eid and eid not in master_ids and eid not in todo:
                    todo[eid] = row
    print(f"Net new entities to classify: {len(todo):,}")
    if not todo:
        print("Nothing new to classify.")
        return

    api_key = get_api_key()

    # Preflight: one test request so a bad key / wrong model alias fails fast.
    print(f"Preflight request to {chat_url} (model: {args.model})...", flush=True)
    try:
        post_json(chat_url, api_key, {
            "model": args.model, "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        })
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"Preflight failed: HTTP {e.code}: {body}")
        if e.code == 404 or "model" in body.lower():
            try:
                models = post_json_get(base + "/v1/models", api_key)
                names = sorted(m.get("id", "?") for m in models.get("data", []))
                print("Models available on the proxy:")
                for n in names:
                    print(f"  {n}")
                print("Re-run with --model <one of the above>.")
            except Exception:
                print("Could not list models; check the alias with your proxy admin.")
        sys.exit(1)
    print("Preflight OK.")

    # Open master for append (write header only if new/empty) so completed
    # rows are saved as we go — interrupt + re-run resumes automatically.
    need_header = not os.path.exists(args.master) or os.path.getsize(args.master) == 0
    out = open(args.master, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
    if need_header:
        writer.writeheader()
        out.flush()
    lock = threading.Lock()
    done = 0
    failures = 0
    counts = Counter()
    start = time.time()

    def work(item):
        eid, row = item
        return eid, row, classify_one(chat_url, api_key, args.model, row)

    print(f"Classifying {len(todo):,} entities with {args.workers} workers...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, item) for item in todo.items()]
        for fut in as_completed(futures):
            try:
                eid, row, result = fut.result()
            except Exception as e:
                with lock:
                    failures += 1
                    done += 1
                    print(f"\n  request failed permanently: {e}")
                continue
            with lock:
                writer.writerow(build_output_row(row, result))
                out.flush()
                counts[result["channels"]] += 1
                done += 1
                if done % 100 == 0 or done == len(todo):
                    rate = done / max(time.time() - start, 1e-9)
                    eta = (len(todo) - done) / max(rate, 1e-9)
                    print(f"  {done:,}/{len(todo):,}  ({rate:.1f}/s, ~{eta/60:.0f} min left)", flush=True)

    out.close()

    total = len(master_ids) + done - failures
    print(f"\nMerged {done - failures:,} new entities into {args.master}.")
    if failures:
        print(f"{failures:,} request(s) failed permanently — re-run the same "
              f"command to retry just those entities.")
    print(f"Total unique entities now: {total:,}")
    print("\nChannel breakdown (this run):")
    for cat, count in counts.most_common():
        print(f"  {cat:<20} {count:>6,}  ({count / max(done - failures, 1) * 100:.1f}%)")


def post_json_get(url: str, api_key: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    main()
