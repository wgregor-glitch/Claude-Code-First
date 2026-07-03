"""ESDc QA CLI.

Commands:
  explain   For each alert in a results CSV, show WHY it matched a given ESDC
            (which keyword / topic / threshold fired), plus any UNVERIFIED leaves.
  show      Pretty-print an ESDC's parsed boolean logic.
  list      List ESDC names in expansion.json (optionally filtered).

Examples:
  python -m esdc_qa.cli explain --esdc "Electronic Warfare" \\
      --expansion esdc_qa/expansion.json \\
      --results "esdc_qa/ESDc_Electronic_Warfare_Example.csv"
  python -m esdc_qa.cli show --esdc "Electronic Warfare" --expansion esdc_qa/expansion.json
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

from .evaluate import evaluate
from .expansion_loader import find_esdc, load_expansion
from .model import pretty
from .relax import generate_variants
from .results_csv import load_results, which_esdcs
from .semantics import load_semantics
from .sql_gen import build_fn_sql, extract_text_terms

HERE = os.path.dirname(__file__)


def _pick_esdc(esdcs, needle):
    hits = find_esdc(esdcs, needle)
    if not hits:
        sys.exit(f"No ESDC matches {needle!r}. Try `list`.")
    if len(hits) > 1:
        exact = [e for e in hits if e.name.lower() == needle.strip().lower()]
        if exact:
            return exact[0]
        names = "\n  ".join(e.name for e in hits[:20])
        sys.exit(f"{needle!r} is ambiguous ({len(hits)} matches):\n  {names}")
    return hits[0]


def cmd_list(args):
    esdcs = load_expansion(args.expansion)
    for name in sorted(esdcs):
        if not args.filter or args.filter.lower() in name.lower():
            print(name)


def cmd_show(args):
    esdcs = load_expansion(args.expansion)
    e = _pick_esdc(esdcs, args.esdc)
    print(f"# {e.name}  (id={e.id}, stemming={e.stemming})")
    if e.logic is None:
        print(e.raw_query)
        return
    print(pretty(e.logic))


def cmd_explain(args):
    esdcs = load_expansion(args.expansion)
    e = _pick_esdc(esdcs, args.esdc)
    if e.logic is None:
        sys.exit(f"ESDC {e.name!r} failed to parse: {e.raw_query[:200]}")
    sem = load_semantics(args.refs or HERE)
    headers, rows = load_results(args.results)

    writer = None
    if args.out:
        fh = open(args.out, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["alert_id", "matched", "fully_verified",
                         "confirmed_leaves", "unverified_leaves", "why"])

    n_match = n_caveat = 0
    for row in rows:
        # only evaluate rows that the export says matched this ESDC
        if args.only_tagged and e.name not in which_esdcs(row):
            continue
        res = evaluate(e.logic, row, sem, headers)
        if res.matched:
            n_match += 1
        if not res.fully_verified:
            n_caveat += 1
        aid = row.get("Alert ID", "?")

        if not args.quiet:
            tag = "✓ verified" if res.fully_verified else "~ has unverified leaves"
            print(f"\n=== Alert {aid}  [{tag}] ===")
            for le in res.confirmed:
                print(f"  ✓ {le.explain()}")
                if le.snippet:
                    print(f"      …{le.snippet}…")
            for le in res.blockers:
                print(f"  ⛔ {le.explain()}")
            for le in res.unknowns:
                print(f"  ? {le.explain()}")

        if writer:
            writer.writerow([
                aid, res.matched, res.fully_verified,
                " | ".join(le.explain() for le in res.confirmed),
                " | ".join(le.explain() for le in res.unknowns),
                res.why(),
            ])

    if writer:
        fh.close()
        print(f"\nWrote {args.out}")
    print(f"\n{n_match} matched / {len(rows)} rows ; {n_caveat} had unverified leaves.")


def cmd_find_fn(args):
    """Find candidate false negatives: rows that match a RELAXED rule but not the original."""
    esdcs = load_expansion(args.expansion)
    e = _pick_esdc(esdcs, args.esdc)
    if e.logic is None:
        sys.exit(f"ESDC {e.name!r} failed to parse.")
    sem = load_semantics(args.refs or HERE)
    headers, rows = load_results(args.corpus)
    variants = generate_variants(e)

    writer = None
    if args.out:
        fh = open(args.out, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["alert_id", "relaxation", "confidence", "description",
                         "evidence", "why_original_missed"])

    candidates = 0
    for row in rows:
        orig = evaluate(e.logic, row, sem, headers)
        if orig.matched:
            continue  # already matches the real rule -> not a false negative
        # find the highest-confidence relaxation that flips it to a match
        best = None
        for v in sorted(variants, key=lambda x: -x.confidence):
            r = evaluate(v.logic, row, sem, headers)
            if r.matched and r.confirmed:
                best = (v, r)
                break
        if not best:
            continue
        candidates += 1
        v, r = best
        aid = row.get("Alert ID", "?")
        evidence = " | ".join(le.explain() for le in r.confirmed[:4])
        missed = " | ".join(le.explain() for le in orig.failed[:4]) or "(all-clause near miss)"
        if not args.quiet:
            print(f"\n=== FN candidate {aid}  [{v.kind}, conf {v.confidence}] ===")
            print(f"  relaxation: {v.description}")
            print(f"  evidence:   {evidence}")
            print(f"  missed:     {missed}")
        if writer:
            writer.writerow([aid, v.kind, v.confidence, v.description, evidence, missed])

    if writer:
        fh.close(); print(f"\nWrote {args.out}")
    print(f"\n{candidates} false-negative candidate(s) from {len(rows)} corpus rows.")


def cmd_fn_sql(args):
    """Generate a Snowflake FN query for an ESDC.

    --full   translate the ENTIRE rule (keyword AND topic AND modelset, minus
             exclusions) -> only true false negatives. Repairs unquoted keywords.
    default  keyword-only scan (broad). Extracts terms from the raw query, so it
             works even on ESDCs that don't parse.
    """
    esdcs = load_expansion(args.expansion)

    # batch mode: generate a full FN query for EVERY ESDC into a directory
    if args.full and args.all:
        import os
        from .ast_to_sql import build_full_fn_sql
        sem = load_semantics(args.refs or HERE)
        outdir = args.outdir or "fn_queries"
        os.makedirs(outdir, exist_ok=True)
        ok = fail = 0
        for name, e in esdcs.items():
            try:
                e.logic = _resolve_logic(e)
                sql = build_full_fn_sql(e, sem, days_back=args.days)
            except Exception as ex:
                fail += 1
                print(f"  SKIP {name}: {ex}")
                continue
            safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip()
            with open(os.path.join(outdir, f"{safe}.sql"), "w", encoding="utf-8") as f:
                f.write(sql)
            ok += 1
        print(f"Wrote {ok} FN queries to {outdir}/  ({fail} failed)")
        return

    e = _pick_esdc(esdcs, args.esdc)
    if args.full:
        from .ast_to_sql import build_full_fn_sql
        try:
            e.logic = _resolve_logic(e)
        except Exception as ex:
            sys.exit(f"Cannot parse {e.name!r} even after repair: {ex}")
        sem = load_semantics(args.refs or HERE)
        sql = build_full_fn_sql(e, sem, days_back=args.days)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(sql)
            print(f"Wrote {args.out}")
        else:
            print(sql)
        return

    terms = extract_text_terms(e.raw_query, only_unquoted_multiword=args.unquoted_only)
    if not terms:
        sys.exit("No text terms found for this ESDC.")
    sql = build_fn_sql(e.name, terms, days_back=args.days)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(sql)
        print(f"Wrote {args.out}  ({len(terms)} phrases)")
    else:
        print(sql)


def _resolve_logic(e):
    """Return parsed logic for an ESDC, repairing malformed queries if needed."""
    if e.logic is not None:
        return e.logic
    from .query_parser import parse_query, repair_query
    return parse_query(repair_query(e.raw_query))


def cmd_audit(args):
    """Audit every ESDC: can we build an accurate FN query, and what are the caveats?"""
    from .ast_to_sql import compile_report
    esdcs = load_expansion(args.expansion)
    sem = load_semantics(args.refs or HERE)
    rows = []
    for name, e in esdcs.items():
        try:
            e.logic = _resolve_logic(e)
        except Exception as ex:
            rows.append({"name": name, "status": "PARSE_FAIL", "unverifiable": "", "trigram": str(ex)[:60]})
            continue
        rep = compile_report(e, sem)
        status = "FULLY_VERIFIABLE"
        if rep["unverifiable"]:
            status = "HAS_UNVERIFIABLE_FIELDS"
        elif rep["trigram"]:
            status = "HAS_TRIGRAM_BUG"
        rows.append({"name": name, "status": status,
                     "unverifiable": " | ".join(rep["unverifiable"]),
                     "trigram": " | ".join(rep["trigram"])})

    from collections import Counter
    tally = Counter(r["status"] for r in rows)
    out = args.out or "esdc_fn_audit.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "status", "unverifiable", "trigram"])
        w.writeheader()
        w.writerows(rows)
    print(f"Audited {len(rows)} ESDCs -> {out}")
    for s, n in tally.most_common():
        print(f"  {s}: {n}")


def cmd_fn_report(args):
    """Annotate FN-scan alerts with why each should have matched + a remedy."""
    import json
    from .fn_report import annotate, output_topic_names
    from .query_parser import parse_query, repair_query

    esdcs = load_expansion(args.expansion)
    e = _pick_esdc(esdcs, args.esdc)
    if e.logic is None:
        e.logic = parse_query(repair_query(e.raw_query))
    sem = load_semantics(args.refs or HERE)
    with open(args.expansion, encoding="utf-8-sig") as f:
        expansion = json.load(f)
    out_topics = output_topic_names(e.name, expansion, sem)

    headers, rows = load_results(args.results)
    cap_col = next((h for h in headers if h.split(".")[-1].strip().lower() == "caption"), "Caption")
    top_col = next((h for h in headers if "topic" in h.lower()), "Internal Topics")
    annotated = annotate(e, rows, sem, out_topics, cap_col, top_col)

    cols = ["alert_id", "source_link", "verdict", "matched_keywords", "output_topic_present",
            "why_should_have_matched", "proposed_remedy", "matched_topics_sample", "caption"]
    out_path = args.out or "fn_report.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for a in annotated:
            w.writerow(a)
    from collections import Counter
    tally = Counter(a["verdict"] for a in annotated)
    print(f"Wrote {out_path}  ({len(annotated)} alerts)")
    print(f"output topic(s): {out_topics}")
    for v, n in tally.most_common():
        print(f"  {v}: {n}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="esdc_qa")
    p.add_argument("--expansion", default=os.path.join(HERE, "expansion.json"))
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list"); pl.add_argument("--filter", default=""); pl.set_defaults(fn=cmd_list)

    ps = sub.add_parser("show"); ps.add_argument("--esdc", required=True); ps.set_defaults(fn=cmd_show)

    pe = sub.add_parser("explain")
    pe.add_argument("--esdc", required=True)
    pe.add_argument("--results", required=True)
    pe.add_argument("--refs", default="", help="dir with the reference CSVs (default: package dir)")
    pe.add_argument("--out", default="", help="write per-row results to this CSV")
    pe.add_argument("--only-tagged", action="store_true",
                    help="only evaluate rows whose 'ESDC Names' includes this ESDC")
    pe.add_argument("--quiet", action="store_true")
    pe.set_defaults(fn=cmd_explain)

    pf = sub.add_parser("find-fn", help="find false-negative candidates in a broad corpus CSV")
    pf.add_argument("--esdc", required=True)
    pf.add_argument("--corpus", required=True, help="CSV of alerts to scan (ideally NOT pre-filtered to this ESDC)")
    pf.add_argument("--refs", default="")
    pf.add_argument("--out", default="")
    pf.add_argument("--quiet", action="store_true")
    pf.set_defaults(fn=cmd_find_fn)

    pq = sub.add_parser("fn-sql", help="generate a Snowflake FN query from an ESDC's keyword terms")
    pq.add_argument("--esdc", default="", help="ESDC name (omit with --all)")
    pq.add_argument("--days", type=int, default=7, help="lookback window in days")
    pq.add_argument("--unquoted-only", action="store_true",
                    help="only emit the unquoted multi-word phrases (the suspected-broken ones)")
    pq.add_argument("--full", action="store_true",
                    help="translate the ENTIRE rule -> true false negatives (repairs malformed queries)")
    pq.add_argument("--all", action="store_true",
                    help="with --full: generate a query for EVERY ESDC into --outdir")
    pq.add_argument("--outdir", default="", help="output dir for --all (default: fn_queries/)")
    pq.add_argument("--refs", default="", help="dir with the reference CSVs (default: package dir)")
    pq.add_argument("--out", default="")
    pq.set_defaults(fn=cmd_fn_sql)

    pr = sub.add_parser("fn-report", help="annotate FN-scan alerts with why-matched + remedy -> CSV")
    pr.add_argument("--esdc", required=True)
    pr.add_argument("--results", required=True, help="CSV from the fn-sql --full scan")
    pr.add_argument("--refs", default="")
    pr.add_argument("--out", default="")
    pr.set_defaults(fn=cmd_fn_report)

    pa = sub.add_parser("audit", help="audit FN-query verifiability across ALL ESDCs")
    pa.add_argument("--refs", default="")
    pa.add_argument("--out", default="")
    pa.set_defaults(fn=cmd_audit)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
