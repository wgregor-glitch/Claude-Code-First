"""
score_headlines.py — evaluate headline CSV against GT labels in the testsheet.

Usage:
    python3 score_headlines.py headlines.csv testsheet.csv

Outputs per-label and aggregate precision / recall / F1.
"""

import csv
import re
import sys
from collections import defaultdict

GT_FIELDS = [
    "police_1", "police_2plus", "fire_truck_1", "fire_truck_2plus",
    "ambulance_1", "ambulance_2plus", "no_emergency_vehicles", "hard_to_tell",
    "crash", "pulled_over", "blocked_road", "construction", "fire", "crowd",
    "other_identifiable", "no_incident_visible",
]
GT_COL_START = 3


def parse_headline(headline):
    """Return dicts of claimed vehicle and incident booleans from a headline string."""
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
    """Return dict: csv_index -> gt_dict."""
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
    """Collapse GT into the same vehicle/incident keys we score."""
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


def score(headlines_path, testsheet_path):
    gt_all = load_gt(testsheet_path)

    # Per-label counts: {label: [tp, fp, fn]}
    vcounts = defaultdict(lambda: [0, 0, 0])
    icounts = defaultdict(lambda: [0, 0, 0])

    matched = 0
    with open(headlines_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_num = int(row["row"])
            if row.get("error"):
                continue
            if row_num not in gt_all:
                continue
            matched += 1

            pred_v, pred_i = parse_headline(row["headline"])
            gt_v, gt_i = gt_summary(gt_all[row_num])

            for label in pred_v:
                p, g = pred_v[label], gt_v[label]
                if p and g:
                    vcounts[label][0] += 1   # TP
                elif p and not g:
                    vcounts[label][1] += 1   # FP
                elif not p and g:
                    vcounts[label][2] += 1   # FN

            for label in pred_i:
                p, g = pred_i[label], gt_i[label]
                if p and g:
                    icounts[label][0] += 1
                elif p and not g:
                    icounts[label][1] += 1
                elif not p and g:
                    icounts[label][2] += 1

    print(f"\nMatched {matched} rows\n")

    def print_table(counts, title):
        print(f"{'─'*56}")
        print(f"  {title}")
        print(f"{'─'*56}")
        print(f"  {'Label':<20} {'P':>6} {'R':>6} {'F1':>6}  TP  FP  FN")
        print(f"{'─'*56}")
        tot_tp = tot_fp = tot_fn = 0
        for label, (tp, fp, fn) in sorted(counts.items()):
            tot_tp += tp; tot_fp += fp; tot_fn += fn
            prec = tp / (tp + fp) if (tp + fp) else 0
            rec  = tp / (tp + fn) if (tp + fn) else 0
            f1   = 2*prec*rec / (prec + rec) if (prec + rec) else 0
            print(f"  {label:<20} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f}  {tp:>2}  {fp:>2}  {fn:>2}")
        prec = tot_tp / (tot_tp + tot_fp) if (tot_tp + tot_fp) else 0
        rec  = tot_tp / (tot_tp + tot_fn) if (tot_tp + tot_fn) else 0
        f1   = 2*prec*rec / (prec + rec) if (prec + rec) else 0
        print(f"{'─'*56}")
        print(f"  {'MICRO AVG':<20} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f}  {tot_tp:>2}  {tot_fp:>2}  {tot_fn:>2}")
        print()

    print_table(vcounts, "VEHICLES")
    print_table(icounts, "INCIDENTS")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python3 score_headlines.py headlines.csv testsheet.csv")
    score(sys.argv[1], sys.argv[2])
