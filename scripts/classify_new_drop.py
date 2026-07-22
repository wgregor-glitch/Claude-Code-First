"""
Classify net-new entities from one or more freshly-exported raw CSVs and
merge them into the shared entities_classified.csv master, without
re-classifying entities already present in the master.

Usage:
    python3 classify_new_drop.py <new_csv_1> [new_csv_2 ...]

Each input CSV must have these columns: ENTITY_NAME, ID, LINK, VERIFIED

Requires:
    - Python 3.11+ with the `anthropic` package installed (see requirements.txt)
    - ANTHROPIC_API_KEY in the environment (or it will prompt for one)

Run from the scripts/ directory (or point MASTER at ../data/entities_classified.csv).
"""

import csv
import sys

import classify_entities_shared as shared

MASTER = "../data/entities_classified.csv"

MASTER_FIELDS = shared.OUTPUT_FIELDS


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 classify_new_drop.py <new_csv_1> [new_csv_2 ...]")
        sys.exit(1)

    new_files = sys.argv[1:]

    try:
        master_rows = shared.read_csv(MASTER)
    except FileNotFoundError:
        master_rows = []
    master_by_id = {}
    for row in master_rows:
        master_by_id.setdefault(row["id"], row)

    print(f"Current master: {len(master_by_id):,} unique entities")

    new_entities = {}
    for path in new_files:
        for row in shared.read_csv(path):
            eid = row.get(shared.COL_ID, "").strip()
            if not eid or eid in master_by_id or eid in new_entities:
                continue
            new_entities[eid] = row

    print(f"Net new entities to classify: {len(new_entities):,}")

    if not new_entities:
        print("Nothing new to classify.")
        return

    api_key = shared.get_api_key()
    client = shared.anthropic.Anthropic(api_key=api_key)

    rows = list(new_entities.values())
    for row in rows:
        row["_index"] = row[shared.COL_ID].strip()  # custom_id == entity ID

    batch_list = list(shared.chunks(rows, shared.BATCH_SIZE))
    print(f"Submitting {len(batch_list)} batch(es)...")
    batch_ids = [shared.submit_batch(client, b) for b in batch_list]

    all_results = {}
    for batch_id in batch_ids:
        all_results.update(shared.wait_for_batch(client, batch_id))

    for row in rows:
        eid = row["_index"]
        result = all_results.get(eid, shared.empty_result())
        master_by_id[eid] = shared.build_output_row(row, result)

    with open(MASTER, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_FIELDS)
        writer.writeheader()
        for row in master_by_id.values():
            writer.writerow({k: row.get(k, "") for k in MASTER_FIELDS})

    print(f"Merged {len(new_entities):,} new entities into {MASTER}.")
    print(f"Total unique entities now: {len(master_by_id):,}")


if __name__ == "__main__":
    main()
