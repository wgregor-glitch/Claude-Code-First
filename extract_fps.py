"""Extract false positive rows from predictions vs GT."""

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

GT_FIELDS = [
    "police_1", "police_2plus", "fire_truck_1", "fire_truck_2plus",
    "ambulance_1", "ambulance_2plus", "no_emergency_vehicles", "hard_to_tell",
    "crash", "pulled_over", "blocked_road", "construction", "fire", "crowd",
    "other_identifiable", "no_incident_visible",
]
GT_COL_START = 3


def parse_headline(headline):
    h = (headline or "").strip().lower()
    vehicles = {
        "police":    bool(re.search(r'\bpolice vehicles?\b', h)),
        "fire":      bool(re.search(r'\bfire trucks?\b', h)),
        "ambulance": bool(re.search(r'\bambulances?\b', h)),
        "none":      "no emergency vehicles visible" in h,
    }
    is_blocked = "road blocked" in h or "blocked road" in h
    incidents = {
        "crash":       bool(re.search(r'\bcrash\b', h)),
        "pulled_over": "pulled-over vehicle" in h,
        "blocked_road": is_blocked,
        "construction": "construction" in h,
        "fire_incident": bool(re.search(r'responding to fire\b', h)),
        "crowd":       "crowd" in h,
    }
    return vehicles, incidents


def load_gt(testsheet_path):
    gt = {}
    with open(testsheet_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    for idx, row in enumerate(rows):
        if len(row) <= GT_COL_START + len(GT_FIELDS) - 1:
            continue
        rec = {GT_FIELDS[j]: row[GT_COL_START + j].strip().upper() == "TRUE"
               for j in range(len(GT_FIELDS))}
        gt[idx] = rec
    return gt


def gt_summary(rec):
    vehicles = {
        "police":    rec["police_1"] or rec["police_2plus"],
        "fire":      rec["fire_truck_1"] or rec["fire_truck_2plus"],
        "ambulance": rec["ambulance_1"] or rec["ambulance_2plus"],
        "none":      rec["no_emergency_vehicles"],
    }
    incidents = {
        "crash":        rec["crash"],
        "pulled_over":  rec["pulled_over"],
        "blocked_road": rec["blocked_road"],
        "construction": rec["construction"],
        "fire_incident": rec["fire"],
        "crowd":        rec["crowd"],
    }
    return vehicles, incidents


def main():
    headlines_path = "/root/.claude/uploads/4198d3e6-bbc8-5bb5-b7be-6a36a4116264/96b7eeb3-predictions.csv"
    testsheet_path = "/root/.claude/uploads/4198d3e6-bbc8-5bb5-b7be-6a36a4116264/2c51133a-testsheet.csv"
    out_path = "false_positives.csv"

    gt_all = load_gt(testsheet_path)
    fp_rows = []

    with open(headlines_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_num = int(row["row"])
            if row.get("error"):
                continue
            if row_num not in gt_all:
                continue

            pred_v, pred_i = parse_headline(row["headline"])
            gt_v, gt_i = gt_summary(gt_all[row_num])

            fp_labels = []
            for label in pred_v:
                if pred_v[label] and not gt_v[label]:
                    fp_labels.append(f"vehicle:{label}")
            for label in pred_i:
                if pred_i[label] and not gt_i[label]:
                    fp_labels.append(f"incident:{label}")

            if fp_labels:
                # What GT says is true
                gt_true_v = [k for k, v in gt_v.items() if v]
                gt_true_i = [k for k, v in gt_i.items() if v]
                fp_rows.append({
                    "row": row_num,
                    "headline": row["headline"],
                    "fp_labels": "; ".join(fp_labels),
                    "gt_vehicles": "; ".join(gt_true_v) if gt_true_v else "(none)",
                    "gt_incidents": "; ".join(gt_true_i) if gt_true_i else "(none)",
                    "url": row["url"],
                })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["row", "headline", "fp_labels", "gt_vehicles", "gt_incidents", "url"])
        writer.writeheader()
        writer.writerows(fp_rows)

    print(f"Wrote {len(fp_rows)} FP rows to {out_path}")


if __name__ == "__main__":
    main()
