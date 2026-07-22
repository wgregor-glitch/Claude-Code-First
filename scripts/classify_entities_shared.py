"""
Entity classifier using Claude Message Batches API.
Classifies CSV rows into official source channels and infers location metadata.

Shareable version — prompts for Anthropic API key at runtime.
"""

import anthropic
import csv
import getpass
import json
import re
import time
import os
from collections import Counter

# --- Config ---
INPUT_CSV = "fr_june_er_gov.csv"
OUTPUT_CSV = "entities_classified.csv"

COL_NAME     = "ENTITY_NAME"
COL_ID       = "ID"
COL_LINK     = "LINK"
COL_VERIFIED = "VERIFIED"

BATCH_SIZE = 10_000
MODEL      = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are an expert media and source classifier. Given an entity name and URL, return a JSON object with the following fields:

channels: Classify the entity into exactly one of these official channel types:
  - news        : Broadcast, print, or major digital source focused on news at a national or international level
  - localnews   : Broadcast or digital source focused on news at the municipal, regional, or state level
  - reporter    : Journalist contributing to a specific major news outlet or major blog
  - blog        : A digital source or the digital piece of a print magazine that breaks news
  - majorblog   : A digital source (or the digital piece of a major print magazine) that breaks news and is a leading source in a particular topic area
  - gov         : Federal, state, or local government organization (office, department, agency) or official person in Government — NOT including emergency services
  - emergency   : Any agency or department whose primary function is emergency response: Police, Sheriff, Fire Department, EMS, 911, OEM, Homeland Security, Coast Guard, National Guard
  - ngo         : Any non-profit citizens group organized at a local, national, or international level
  - university  : Accredited colleges, universities, and their associated accounts
  - unknown     : Cannot be determined from the information provided

IMPORTANT PRIORITY RULES:

news vs. localnews — this is the most important distinction:
- "news" is reserved for a very small set of the most globally recognized news organizations — roughly the top 50 in the world. These are outlets that are household names across multiple continents, with large international audiences and global editorial footprints.
- The ONLY outlets that qualify as "news" are organizations equivalent to: BBC, CNN, Fox News, MSNBC, ABC News (US), NBC News, CBS News, Reuters, Associated Press (AP), AFP, Al Jazeera, Bloomberg, The New York Times, The Washington Post, The Wall Street Journal, The Guardian, The Telegraph, Financial Times, Sky News, CNBC, NPR, NHK, CGTN, TASS, Xinhua, Yonhap, CBC, SBS (Australia), RFI, Politico, ESPN, The Weather Channel, and their direct sub-accounts/verticals (e.g. Reuters UK, BBC Business, Fox News Politics, Bloomberg Markets, NYT World).
- If the outlet is NOT on that level of global recognition — even if it is a well-known national outlet in its own country — classify it as "localnews".
- "localnews" is the DEFAULT. Any outlet that is national-only, regional, city-level, or whose global recognition is uncertain must be "localnews".
- WHEN IN DOUBT, ALWAYS CHOOSE "localnews" OVER "news". It is far better to under-classify than to over-classify.

Official vs. community sources:
- "emergency" is ONLY for verified, official government emergency response agencies. They will almost always include a specific jurisdiction (city, county, state, or country) in their name or URL.
- Community pages, fan accounts, enthusiast groups, or aggregators that cover emergency/police topics but are NOT official agencies must be classified as "blog" instead.
- If the name contains words like "Live", "Action", "News", "Updates", "Community", "Scanner", or looks informal, treat it as "blog" — not "emergency" or "gov".
- If the account name looks unusual, abbreviated in a non-official way, or lacks a clear jurisdiction, lower your confidence accordingly.

Other rules:
- Use "gov" only for non-emergency government entities (e.g. city hall, legislature, tax office, public health department, elected officials).
- Use "reporter" only for individual journalists, not publications.

Verification status:
- "Platform Verified: true" means the account is officially verified by the platform (e.g. Facebook blue check). Use this as a supporting signal for official classifications (gov, emergency, news, localnews).
- "Platform Verified: false" is a weak signal but relevant when classification is ambiguous — an unverified account claiming to be an official agency or major outlet should lower your confidence. Default to "unknown" if the name looks official but the account is unverified and you cannot confirm it from your training knowledge.

Confidence calibration:
- "high": You are certain of the classification based on a well-known name or clear URL.
- "medium": The name and URL suggest a classification but there is some ambiguity.
- "low": You are guessing. The name or URL is vague, unusual, or missing key signals.
- If your confidence is "medium" or "low", set channels to "unknown" instead of guessing.

EXAMPLES — news (major outlets only):
  "BBC News" → news
  "CNN Breaking News" → news
  "Reuters UK" → news (sub-account of Reuters)
  "BBC Business" → news (sub-account of BBC)
  "Fox News Politics" → news (sub-account of Fox News)
  "Bloomberg Markets" → news (sub-account of Bloomberg)
  "NHK News" → news (major national broadcaster, Japan)
  "CBC News" → news (major national broadcaster, Canada)
  "Al Jazeera English" → news (major international outlet)
  "TASS" → news (major national wire service, Russia)
  "Yonhap News Agency" → news (major national wire service, South Korea)
  "Politico" → news (major national political outlet, US)
  "The Weather Channel" → news (major national outlet, US)

EXAMPLES — localnews (everything else):
  "Dallas Morning News" → localnews
  "GMA News" (Philippines) → localnews (national but not globally recognized)
  "ABS-CBN News" (Philippines) → localnews (national but not globally recognized)
  "Punch Newspapers" (Nigeria) → localnews (national but not globally recognized)
  "NewsNation" (US) → localnews (not globally recognized)
  "GB News" (UK) → localnews (not globally recognized)
  "NEWSMAX" (US) → localnews (not globally recognized at top-50 level)
  "NMB News" (Nepal) → localnews
  "SønderborgPortal" (Denmark) → localnews
  "INRAI Noticias" (Colombia) → localnews
  "ArtsakhPress Agency" (Armenia) → localnews
  Any outlet serving a single city, region, or state → localnews
  Any national outlet not on the explicit top-50 list above → localnews

OTHER EXAMPLES:
  "South African Police Service" → emergency (official, jurisdiction in name)
  "NYC Fire Department" → emergency (official, jurisdiction in name)
  "Los Angeles County Sheriff" → emergency (official, jurisdiction in name)
  "CTPOLICELIVE" → blog (community/enthusiast account, no jurisdiction, informal name)
  "Police1" → blog (aggregator/community, not an official agency)
  "Police Action Live" → blog (informal name, not an official agency)
  "City of Chicago" → gov
  "U.S. Senate" → gov
  "CDC" → gov
  "TechCrunch" → majorblog
  "Jane Smith, CNN Reporter" → reporter
  "Red Cross" → ngo
  "Harvard University" → university

external_source_description: A 1-2 sentence factual description of what this entity is.

city: City inferred from the entity name or URL. Empty string if not determinable.
state_province: State or province inferred from the entity name or URL. Empty string if not determinable.
country: Country inferred from the entity name or URL. Empty string if not determinable.

confidence: Your confidence in the channels classification — "high", "medium", or "low". Remember: if confidence is "medium" or "low", set channels to "unknown".

Respond with valid JSON only. No markdown formatting, no code fences, no explanation — just the raw JSON object."""


def get_api_key() -> str:
    """Get API key from environment variable, or prompt the user."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        print("Using ANTHROPIC_API_KEY from environment.")
        return api_key
    print("Anthropic API key not found in environment.")
    api_key = getpass.getpass("Enter your Anthropic API key (input will be hidden): ").strip()
    if not api_key:
        raise ValueError("No API key provided. Exiting.")
    return api_key


def make_user_prompt(name: str, link: str, verified: str) -> str:
    return f"Entity Name: {name}\nURL: {link}\nPlatform Verified: {verified}"


def strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def read_csv(filepath: str) -> list[dict]:
    with open(filepath, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def submit_batch(client: anthropic.Anthropic, rows: list[dict]) -> str:
    requests = []
    for row in rows:
        name     = row.get(COL_NAME,     "").strip()
        link     = row.get(COL_LINK,     "").strip()
        verified = row.get(COL_VERIFIED, "").strip()
        requests.append({
            "custom_id": str(row["_index"]),
            "params": {
                "model": MODEL,
                "max_tokens": 256,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": make_user_prompt(name, link, verified)}],
            },
        })
    batch = client.messages.batches.create(requests=requests)
    print(f"  Submitted batch {batch.id} ({len(requests)} requests)")
    return batch.id


def wait_for_batch(client: anthropic.Anthropic, batch_id: str, poll_interval: int = 30) -> dict:
    print(f"  Waiting for batch {batch_id}...", end="", flush=True)
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            print(" done.")
            break
        print(".", end="", flush=True)
        time.sleep(poll_interval)

    results = {}
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            text = strip_json(result.result.message.content[0].text)
            try:
                parsed = json.loads(text)
                channels = parsed.get("channels", "unknown")
                if isinstance(channels, list):
                    channels = channels[0] if channels else "unknown"
                results[result.custom_id] = {
                    "channels":                    channels,
                    "external_source_description": parsed.get("external_source_description", ""),
                    "city":                        parsed.get("city", ""),
                    "state_province":              parsed.get("state_province", ""),
                    "country":                     parsed.get("country", ""),
                    "confidence":                  parsed.get("confidence", "low"),
                }
            except json.JSONDecodeError:
                results[result.custom_id] = empty_result()
        else:
            results[result.custom_id] = empty_result()
    return results


def empty_result() -> dict:
    return {
        "channels": "unknown",
        "external_source_description": "",
        "city": "",
        "state_province": "",
        "country": "",
        "confidence": "low",
    }


OUTPUT_FIELDS = [
    "link",
    "id",
    "display_name",
    "entity_name",
    "external_source_description",
    "verified",
    "channels",
    "city",
    "state_province",
    "country",
    "confidence",
]


def build_output_row(row: dict, result: dict) -> dict:
    return {
        "link":                        row.get(COL_LINK, ""),
        "id":                          row.get(COL_ID, ""),
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
    api_key = get_api_key()
    client  = anthropic.Anthropic(api_key=api_key)

    print(f"\nReading {INPUT_CSV}...")
    rows = read_csv(INPUT_CSV)
    for i, row in enumerate(rows):
        row["_index"] = i
    print(f"  {len(rows):,} rows loaded")

    all_results: dict[str, dict] = {}
    batch_list = list(chunks(rows, BATCH_SIZE))
    print(f"Submitting {len(batch_list)} batch(es)...")

    batch_ids = []
    for i, batch_rows in enumerate(batch_list):
        print(f"Batch {i + 1}/{len(batch_list)}:")
        batch_ids.append(submit_batch(client, batch_rows))

    for batch_id in batch_ids:
        all_results.update(wait_for_batch(client, batch_id))

    print(f"\nWriting results to {OUTPUT_CSV}...")
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            idx = str(row["_index"])
            result = all_results.get(idx, empty_result())
            writer.writerow(build_output_row(row, result))

    print(f"Done. {len(rows):,} rows written to {OUTPUT_CSV}")

    cats = [all_results[str(r["_index"])]["channels"] for r in rows if str(r["_index"]) in all_results]
    print("\nChannel summary:")
    for cat, count in sorted(Counter(cats).items(), key=lambda x: -x[1]):
        pct = count / len(cats) * 100
        print(f"  {cat:<20} {count:>6,}  ({pct:.1f}%)")


if __name__ == "__main__":
    main()
