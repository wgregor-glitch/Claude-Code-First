"""
score_severity.py — grade severity predictions against ground truth.

Usage:
    python3 score_severity.py gt.csv predictions.csv

gt.csv      — test_thresh_1.csv format: col 0 = URL, col 2 = GT severity
predictions.csv — headlines output format: row,url,headline,severity,...
                  OR test_thresh_1 format with col 3 = prediction

Matching is done by URL (query string stripped for robustness).
"""

import argparse
import csv
import sys


def load_gt(path):
    """Returns dict of base_url → (row_num, gt_severity)."""
    gt = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        for i, row in enumerate(reader, start=1):
            if len(row) < 3:
                continue
            url = row[0].strip().split("?")[0]
            sev = row[2].strip()
            if sev in ("general.alert2.local", "general.alert3"):
                gt[url] = (i, sev)
    return gt


def load_preds(path):
    """Returns dict of base_url → severity. Auto-detects format."""
    preds = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        # headlines output has named columns; test CSV has positional
        if "severity" in header:
            sev_col = header.index("severity")
            url_col = header.index("url") if "url" in header else 1
            for row in reader:
                if len(row) <= max(sev_col, url_col):
                    continue
                url = row[url_col].strip().split("?")[0]
                sev = row[sev_col].strip()
                if sev:
                    preds[url] = sev
        else:
            # Assume test CSV format: col 0 = URL, col 3 = prediction
            for row in reader:
                if len(row) < 4:
                    continue
                url = row[0].strip().split("?")[0]
                sev = row[3].strip()
                if sev:
                    preds[url] = sev
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt_csv", metavar="GT_CSV")
    ap.add_argument("pred_csv", metavar="PRED_CSV")
    ap.add_argument("--show-errors", action="store_true",
                    help="Print each incorrect row")
    args = ap.parse_args()

    gt = load_gt(args.gt_csv)
    preds = load_preds(args.pred_csv)

    total = correct = fps = fns = skipped = 0
    fp_rows, fn_rows = [], []

    for url, (row_num, gt_sev) in sorted(gt.items(), key=lambda x: x[1][0]):
        pred_sev = preds.get(url)
        if not pred_sev:
            skipped += 1
            continue
        total += 1
        if pred_sev == gt_sev:
            correct += 1
        elif pred_sev == "general.alert2.local" and gt_sev == "general.alert3":
            fps += 1
            fp_rows.append(row_num)
            if args.show_errors:
                print(f"  FP row {row_num:3d}: predicted alert2.local, GT=alert3")
        elif pred_sev == "general.alert3" and gt_sev == "general.alert2.local":
            fns += 1
            fn_rows.append(row_num)
            if args.show_errors:
                print(f"  FN row {row_num:3d}: predicted alert3,       GT=alert2.local")

    acc = correct / total * 100 if total else 0

    # GT breakdown
    gt2 = sum(1 for _, s in gt.values() if s == "general.alert2.local")
    gt3 = sum(1 for _, s in gt.values() if s == "general.alert3")

    print(f"GT distribution : alert2.local={gt2}  alert3={gt3}  total={gt2+gt3}")
    print(f"Matched rows    : {total}  (skipped/missing: {skipped})")
    print()
    print(f"Correct  : {correct} / {total}  ({acc:.1f}%)")
    print(f"FPs      : {fps}  (predicted alert2.local, GT=alert3)")
    print(f"FNs      : {fns}  (predicted alert3, GT=alert2.local)")
    print()
    if fps + fns:
        precision = correct / (correct + fps) if (correct + fps) else 0
        recall    = correct / (correct + fns) if (correct + fns) else 0
        f1        = 2*precision*recall/(precision+recall) if (precision+recall) else 0
        print(f"Precision: {precision:.3f}  Recall: {recall:.3f}  F1: {f1:.3f}")
    print(f"\nFP rows: {fp_rows}")
    print(f"FN rows: {fn_rows}")


if __name__ == "__main__":
    main()
