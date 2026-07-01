"""Re-score predictions applying user corrections from the FP review CSV."""

import csv
import re
from collections import defaultdict

PREDICTIONS_PATH = "/root/.claude/uploads/4198d3e6-bbc8-5bb5-b7be-6a36a4116264/96b7eeb3-predictions.csv"
TESTSHEET_PATH   = "/root/.claude/uploads/4198d3e6-bbc8-5bb5-b7be-6a36a4116264/2c51133a-testsheet.csv"
CORRECTIONS_PATH = "/root/.claude/uploads/4198d3e6-bbc8-5bb5-b7be-6a36a4116264/cd45af46-Untitled_spreadsheet__false_positives_1.csv"

GT_FIELDS    = ["police_1","police_2plus","fire_truck_1","fire_truck_2plus",
                "ambulance_1","ambulance_2plus","no_emergency_vehicles","hard_to_tell",
                "crash","pulled_over","blocked_road","construction","fire","crowd",
                "other_identifiable","no_incident_visible"]
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
        "crash":        bool(re.search(r'\bcrash\b', h)),
        "pulled_over":  "pulled-over vehicle" in h,
        "blocked_road": is_blocked,
        "construction": "construction" in h,
        "fire_incident":bool(re.search(r'responding to fire\b', h)),
        "crowd":        "crowd" in h,
    }
    return vehicles, incidents


def load_gt(path):
    gt = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
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
        "fire_incident":rec["fire"],
        "crowd":        rec["crowd"],
    }
    return vehicles, incidents


def load_corrections(path):
    """
    Returns dict: row_num -> {prediction_correct, gt_correct, fp_labels}
    prediction_correct=True  → model was right; flip those fp_labels FP→TP
    gt_correct=True          → GT was right;  keep original scoring
    Neither                  → ambiguous; keep as FP (conservative)
    """
    corr = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rn = int(row["row"])
            fp_labels = [x.strip() for x in row.get("fp_labels", "").split(";") if x.strip()]
            corr[rn] = {
                "prediction_correct": row.get("prediction_correct", "").strip().lower() == "yes",
                "gt_correct":         row.get("gt_correct", "").strip().lower() == "yes",
                "fp_labels":          fp_labels,
            }
    return corr


def score():
    gt_all      = load_gt(TESTSHEET_PATH)
    corrections = load_corrections(CORRECTIONS_PATH)

    vcounts = defaultdict(lambda: [0, 0, 0])   # label -> [TP, FP, FN]
    icounts = defaultdict(lambda: [0, 0, 0])

    # Track per-row FP overrides: (row_num, scope, label) that should be TP not FP
    tp_overrides = set()
    # Track per-row FN removals: GT labels that were wrong (GT had it, model didn't, but model was right)
    fn_removals  = set()

    for rn, c in corrections.items():
        if not c["prediction_correct"]:
            continue
        for fp_label in c["fp_labels"]:
            scope, label = fp_label.split(":")
            tp_overrides.add((rn, scope, label))

    # For rows where model was right (prediction_correct), also remove spurious GT FNs:
    # load predictions to know what the model actually predicted for those rows
    pred_by_row = {}
    with open(PREDICTIONS_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("error"):
                pred_by_row[int(row["row"])] = row["headline"]

    for rn, c in corrections.items():
        if not c["prediction_correct"]:
            continue
        if rn not in gt_all or rn not in pred_by_row:
            continue
        gt_v, gt_i = gt_summary(gt_all[rn])
        pred_v, pred_i = parse_headline(pred_by_row[rn])

        # Any GT label the model DID NOT predict (original FN) in a row where
        # the model was right overall → those GT labels were also wrong → FN→TN
        for label, gt_val in gt_v.items():
            if gt_val and not pred_v.get(label):
                fn_removals.add((rn, "vehicle", label))
        for label, gt_val in gt_i.items():
            if gt_val and not pred_i.get(label):
                fn_removals.add((rn, "incident", label))

    # --- Main scoring loop ---
    matched = 0
    with open(PREDICTIONS_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rn = int(row["row"])
            if row.get("error") or rn not in gt_all:
                continue
            matched += 1

            pred_v, pred_i = parse_headline(row["headline"])
            gt_v, gt_i     = gt_summary(gt_all[rn])

            for label in pred_v:
                p, g = pred_v[label], gt_v[label]
                if (rn, "vehicle", label) in tp_overrides:
                    vcounts[label][0] += 1   # forced TP
                elif p and g:
                    vcounts[label][0] += 1
                elif p and not g:
                    vcounts[label][1] += 1   # FP
                elif not p and g:
                    if (rn, "vehicle", label) not in fn_removals:
                        vcounts[label][2] += 1  # FN (skip if GT was wrong)

            for label in pred_i:
                p, g = pred_i[label], gt_i[label]
                if (rn, "incident", label) in tp_overrides:
                    icounts[label][0] += 1
                elif p and g:
                    icounts[label][0] += 1
                elif p and not g:
                    icounts[label][1] += 1
                elif not p and g:
                    if (rn, "incident", label) not in fn_removals:
                        icounts[label][2] += 1

    print(f"\nMatched {matched} rows")
    n_pred_correct = sum(1 for c in corrections.values() if c["prediction_correct"])
    n_gt_correct   = sum(1 for c in corrections.values() if c["gt_correct"])
    n_ambiguous    = sum(1 for c in corrections.values()
                        if not c["prediction_correct"] and not c["gt_correct"])
    print(f"Corrections applied: {n_pred_correct} model-correct (FP→TP), "
          f"{n_gt_correct} GT-correct (kept FP), {n_ambiguous} ambiguous (kept FP)\n")

    def print_table(counts, title):
        print(f"{'─'*60}")
        print(f"  {title}")
        print(f"{'─'*60}")
        print(f"  {'Label':<20} {'P':>6} {'R':>6} {'F1':>6}  TP  FP  FN")
        print(f"{'─'*60}")
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
        print(f"{'─'*60}")
        print(f"  {'MICRO AVG':<20} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f}  {tot_tp:>2}  {tot_fp:>2}  {tot_fn:>2}")
        print()

    print_table(vcounts, "VEHICLES (corrected)")
    print_table(icounts, "INCIDENTS (corrected)")

    # Summary vs original
    v_tp = sum(v[0] for v in vcounts.values())
    v_fp = sum(v[1] for v in vcounts.values())
    v_fn = sum(v[2] for v in vcounts.values())
    i_tp = sum(v[0] for v in icounts.values())
    i_fp = sum(v[1] for v in icounts.values())
    i_fn = sum(v[2] for v in icounts.values())
    all_tp = v_tp + i_tp; all_fp = v_fp + i_fp; all_fn = v_fn + i_fn
    op = all_tp / (all_tp + all_fp) if (all_tp + all_fp) else 0
    or_ = all_tp / (all_tp + all_fn) if (all_tp + all_fn) else 0
    of1 = 2*op*or_ / (op + or_) if (op + or_) else 0
    print(f"  OVERALL (corrected)    P={op:.2f}  R={or_:.2f}  F1={of1:.2f}")
    print(f"  ORIGINAL               P=0.71  R=0.78  F1=0.74")


if __name__ == "__main__":
    score()
