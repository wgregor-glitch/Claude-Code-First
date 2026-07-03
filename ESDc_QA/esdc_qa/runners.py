"""Shared run logic for the UIs (Streamlit app + stdlib server).

Each function returns plain data (columns + rows, or a SQL string) so it can be
rendered by any frontend. No Streamlit/HTTP imports here.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .ast_to_sql import build_full_fn_sql, compile_report
from .evaluate import evaluate, _eval_leaf, TRUE
from .expansion_loader import find_esdc
from .fn_report import annotate, output_topic_names
from .model import BoolNode, ESDC, LeafNode, Node, is_leaf
from .query_parser import parse_query, repair_query
from .results_csv import esdc_names_column, load_results_text, which_esdcs
from .semantics import Semantics, field_spec

FIELD_LABELS = {
    "captionkeyword": "caption keyword", "originaltext": "original-text keyword",
    "translatedtext": "translated-text keyword", "summarykeyword": "summary keyword",
    "topicid": "topic", "pipelinetopicid": "pipeline topic", "modelset": "modelset",
    "alertthreshold": "threshold", "geo": "geo", "source": "source",
    "sourcechannel": "source channel", "hascompanytag": "company tag", "freetext": "free text",
}
_MAXLIST = 25  # how many values to list before "+N more"


def resolve_logic(e: ESDC):
    return e.logic if e.logic is not None else parse_query(repair_query(e.raw_query))


def pick_esdc(esdcs: Dict[str, ESDC], name: str) -> ESDC:
    if name in esdcs:
        return esdcs[name]
    hits = find_esdc(esdcs, name)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No ESDC matches {name!r}")
    raise ValueError(f"{name!r} is ambiguous ({len(hits)} matches)")


def _alert_id(row: dict, headers: List[str]) -> str:
    return (row.get("Alert ID") or row.get("alert_id")
            or next((row[h] for h in headers if h.split(".")[-1].strip().lower() == "alert_id"), "?"))


def run_explain(e: ESDC, sem: Semantics, csv_text: str) -> Tuple[List[str], List[list]]:
    headers, rows = load_results_text(csv_text)
    e.logic = resolve_logic(e)
    cols = ["alert_id", "matched", "fully_verified", "confirmed", "unverified", "why"]
    out = []
    for r in rows:
        res = evaluate(e.logic, r, sem, headers)
        out.append([
            _alert_id(r, headers),
            "yes" if res.matched else "no",
            "yes" if res.fully_verified else "no",
            " | ".join(le.explain() for le in res.confirmed),
            " | ".join(le.explain() for le in res.unknowns),
            res.why(),
        ])
    return cols, out


def esdcs_in_csv(csv_text: str) -> List[str]:
    """Distinct ESDC names that appear in the CSV's ESDC-names column."""
    headers, rows = load_results_text(csv_text)
    col = esdc_names_column(headers)
    seen = set()
    for r in rows:
        seen.update(which_esdcs(r, col))
    return sorted(seen)


# one output column per ESDC input type -> (header, {field keys})
_FIELD_COLS = [
    ("caption_keywords", {"captionkeyword"}),
    ("original_text", {"originaltext", "translatedtext"}),
    ("topics", {"topicid", "pipelinetopicid"}),
    ("modelsets", {"modelset"}),
    ("threshold", {"alertthreshold"}),
    ("source", {"source"}),
    ("channel", {"sourcechannel"}),
    ("geo", {"geo"}),
    ("company_tag", {"hascompanytag"}),
    ("summary", {"summarykeyword"}),
]


def _field_cells(res, row=None, headers=None):
    """Per-field-type breakdown for one (alert, ESDC) evaluation.

    "" = the rule doesn't use this field (not relevant to this ESDC)
    values = what this alert matched on that field (relevant — it contributed)
    "not excluded …" = an exclusion (NOT) clause was checked and the alert passed it
    "—" = the rule uses this field but this alert didn't match it (satisfied another way)
    "EXCLUDED by: …" = an exclusion (NOT) fired on this field
    "not verifiable" = field isn't in the export
    """
    by_field = {}
    for le in res.confirmed + res.blockers + res.failed + res.unknowns:
        by_field.setdefault(le.leaf.field.lower(), []).append(le)
    cells = []
    for name, fields in _FIELD_COLS:
        leaves = [le for f in fields for le in by_field.get(f, [])]
        if not leaves:
            cells.append("")
            continue
        pos = [le.evidence or le.leaf.value for le in leaves if le.state == "TRUE" and not le.blocking]
        block = [le.evidence or le.leaf.value for le in leaves if le.state == "TRUE" and le.blocking]
        if block:
            cells.append("EXCLUDED by: " + ", ".join(dict.fromkeys(block)))
        elif pos:
            cells.append(", ".join(dict.fromkeys(pos)))
        elif all(le.state == "UNKNOWN" for le in leaves):
            cells.append("not verifiable from export")
        elif any(le.blocking for le in leaves):
            # an exclusion clause that did NOT fire -> the alert passed it
            extra = ""
            if name == "threshold" and row is not None and headers is not None:
                tcol = field_spec("alertthreshold").resolve_column(headers)
                actual = (str(row.get(tcol, "")).strip() if tcol else "")
                extra = f" · alert tier: {actual}" if actual else ""
            cells.append("not excluded" + extra)
        else:
            cells.append("—")
    return cells


def run_explain_multi(esdc_list: List[ESDC], sem: Semantics, csv_text: str,
                      only_tagged: bool = True) -> Tuple[List[str], List[list]]:
    """Explain matches for one or more ESDCs at once. One output row per
    (ESDC, alert). With only_tagged, an alert is evaluated against an ESDC only
    if its 'ESDC Names' column includes that ESDC. After the `why` column there
    is one column per ESDC input type listing what matched on that field."""
    headers, rows = load_results_text(csv_text)
    col = esdc_names_column(headers)
    for e in esdc_list:
        e.logic = resolve_logic(e)
    cols = (["esdc", "alert_id", "matched", "fully_verified", "why"]
            + [name for name, _ in _FIELD_COLS])
    out = []
    for r in rows:
        tagged = set(which_esdcs(r, col))
        aid = _alert_id(r, headers)
        for e in esdc_list:
            if only_tagged and tagged and e.name not in tagged:
                continue
            res = evaluate(e.logic, r, sem, headers)
            out.append([
                e.name, aid,
                "yes" if res.matched else "no",
                "yes" if res.fully_verified else "no",
                res.why(),
            ] + _field_cells(res, r, headers))
    return cols, out


def _topic_col(headers):
    return next((h for h in headers if "topic" in h.lower()), "Internal Topics")


def _caption_col(headers):
    return next((h for h in headers if h.split(".")[-1].strip().lower() == "caption"), "Caption")


def _diagnose_clause(clause: Node, row, sem, headers, topics_col):
    """If this clause is NOT satisfied for the row, return (verdict, reason, fix)."""
    res = evaluate(clause, row, sem, headers)
    if res.matched:
        return None
    if not is_leaf(clause) and clause.op == "NOT":
        ev = evaluate(clause.clauses[0], row, sem, headers)
        fired = [le.explain() for le in (ev.confirmed + ev.blockers)][:3]
        return ("EXCLUDED",
                f"excluded by a NOT clause — alert matched: {'; '.join(fired) or 'an excluded condition'}",
                "If this alert should match, narrow or remove that exclusion.")
    fs = {l.field.lower() for l in _iter_leaves(clause)}
    if {"captionkeyword", "originaltext", "translatedtext", "freetext", "summarykeyword"} & fs:
        return ("MISSING_KEYWORD", "none of the rule's keyword terms appear in the alert text",
                "Add a keyword that appears in this alert (≤3 words / trigram limit), or broaden an existing term.")
    if {"topicid", "pipelinetopicid"} & fs:
        have = str(row.get(topics_col, "") or "")[:140] or "(none)"
        return ("MISSING_TOPIC", f"alert isn't in any required topic; its topics: {have}",
                "Add one of the alert's topics to the rule, or check the alert's topic tagging.")
    if "modelset" in fs:
        return ("MISSING_MODELSET", "alert's modelset isn't one the rule requires",
                "Add the alert's modelset to the rule if it should be in scope.")
    if "alertthreshold" in fs:
        return ("THRESHOLD", "alert's threshold tier isn't allowed by the rule",
                "Adjust the threshold condition if this tier should be included.")
    if {"source", "sourcechannel", "geo", "hascompanytag"} & fs:
        return ("METADATA", f"a metadata condition ({', '.join(sorted(fs))}) isn't satisfied",
                "Check or relax that metadata condition.")
    return ("FAILED", f"a clause ({', '.join(sorted(fs))}) isn't satisfied", "Review this clause.")


def run_diagnose(e: ESDC, sem: Semantics, csv_text: str) -> Tuple[List[str], List[list]]:
    """For uploaded alerts, explain why each did NOT match an ESDC + a suggested
    fix consistent with the definition. (Localized counterpart to the FN scan.)"""
    headers, rows = load_results_text(csv_text)
    e.logic = resolve_logic(e)
    root = e.logic
    tcol, ccol = _topic_col(headers), _caption_col(headers)
    cols = ["alert_id", "verdict", "reason", "suggested_fix", "caption"]
    out = []
    for r in rows:
        aid = _alert_id(r, headers)
        cap = str(r.get(ccol, "") or "").replace("\n", " ")[:240]
        res = evaluate(root, r, sem, headers)
        if res.matched:
            out.append([aid, "ALREADY_MATCHES", "this alert DOES satisfy the rule — not a miss", "—", cap])
            continue
        clauses = root.clauses if not is_leaf(root) and root.op in ("AND", "OR") else [root]
        diags = [d for d in (_diagnose_clause(c, r, sem, headers, tcol) for c in clauses) if d]
        if not diags:
            diags = [("FAILED", "rule not satisfied", "Review the rule against this alert.")]
        verdict = diags[0][0] if len(diags) == 1 else "MULTIPLE"
        reason = " ; ".join(d[1] for d in diags)
        fix = " | ".join(dict.fromkeys(d[2] for d in diags))
        out.append([aid, verdict, reason, fix, cap])
    return cols, out


_STOPWORDS = set("""a an the and or but of to in on for with at by from as is are was were be been
being this that these those it its their his her our your my we you they he she them us i not no
will would can could should may might must do does did has have had if then than so such into over
under out up down off about after before new news report reports said says say breaking update via
amid amp http https com www rt""".split())


def _word_present(phrase: str, text: str) -> bool:
    import re
    return re.search(rf'(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])', text, re.IGNORECASE) is not None


def _candidate_ngrams(text: str):
    import re
    toks = re.findall(r"[A-Za-z][A-Za-z0-9'&-]+", text.lower())
    for n in (1, 2, 3):
        for i in range(len(toks) - n + 1):
            gram = toks[i:i + n]
            if n == 1:
                if gram[0] in _STOPWORDS or len(gram[0]) < 4:
                    continue
            else:
                if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                    continue
                if all(t in _STOPWORDS for t in gram):
                    continue
            yield " ".join(gram)


# (display field, existing-key, spec field for column resolution, kind)
_SUGGEST_FIELDS = [
    ("caption keyword", "caption", "captionkeyword", "ngram"),
    ("original text", "original_text", "originaltext", "ngram"),
    ("topic", "topic", "topicid", "list"),
    ("modelset", "modelset", "modelset", "single"),
    ("source", "source", "source", "single"),
    ("channel", "channel", "sourcechannel", "single"),
    ("geo", "geo", "geo", "single"),
]


def _existing_by_field(e: ESDC, sem: Semantics):
    """The values already in the ESDC, grouped by suggestion field key."""
    ex = {key: set() for _, key, _, _ in _SUGGEST_FIELDS}
    for l in _iter_leaves(e.logic):
        f = l.field.lower()
        if f in ("captionkeyword", "freetext", "summarykeyword"):
            ex["caption"].add(l.value.lower())
        elif f in ("originaltext", "translatedtext"):
            ex["original_text"].add(l.value.lower())
        elif f in ("topicid", "pipelinetopicid"):
            ex["topic"].add((sem.topic_name(l.value) or l.value).lower())
        elif f == "modelset":
            ex["modelset"].add(l.value.lower())
        elif f == "source":
            ex["source"].add(l.value.lower())
        elif f == "sourcechannel":
            ex["channel"].add(l.value.lower())
        elif f == "geo":
            ex["geo"].add((sem.geo_name(l.value) or l.value).lower())
    return ex


def run_suggest_terms(e: ESDC, sem: Semantics, csv_text: str,
                      top_n: int = 50, min_alerts: int = 2) -> Tuple[List[str], List[list]]:
    """Coverage-broadening FN across ALL logic options (not just captions).

    From alerts that DON'T currently match the ESDC, for every field type
    (caption keyword, original text, topic, modelset, source, channel, geo) it
    collects the values present in those alerts that aren't yet in the ESDC's
    definition for that field, ranked by how many alerts each would add. Feed it
    alerts that should be in scope (topic-filtered or FN-scan output)."""
    from collections import Counter
    headers, rows = load_results_text(csv_text)
    e.logic = resolve_logic(e)
    existing = _existing_by_field(e, sem)
    ccol = _caption_col(headers)

    cols_for = {key: field_spec(spec).resolve_column(headers)
                for _, key, spec, _ in _SUGGEST_FIELDS}
    freq = {key: Counter() for _, key, _, _ in _SUGGEST_FIELDS}
    examples = {}

    def note(key, value, cap):
        examples.setdefault((key, value.lower()), cap)

    for r in rows:
        if evaluate(e.logic, r, sem, headers).matched:
            continue  # already caught — nothing to add for this alert
        cap = str(r.get(ccol, "") or "").replace("\n", " ")[:160]
        for disp, key, _spec, kind in _SUGGEST_FIELDS:
            col = cols_for[key]
            if not col:
                continue
            cell = str(r.get(col, "") or "")
            if not cell.strip():
                continue
            if kind == "ngram":
                for g in set(_candidate_ngrams(cell)):
                    if g in existing[key] or any(g in t or t in g for t in existing[key]):
                        continue
                    freq[key][g] += 1
                    note(key, g, cap)
            elif kind == "list":
                for v in {x.strip() for x in cell.split(",") if x.strip()}:
                    if v.lower() in existing[key]:
                        continue
                    freq[key][v] += 1
                    note(key, v, cap)
            else:  # single
                v = cell.strip()
                if v.lower() in existing[key]:
                    continue
                freq[key][v] += 1
                note(key, v, cap)

    cands = []
    disp_for = {key: disp for disp, key, _, _ in _SUGGEST_FIELDS}
    for key, counter in freq.items():
        for value, c in counter.items():
            if c >= min_alerts:
                cands.append((disp_for[key], value, c, _input_caveat(disp_for[key], value),
                              examples.get((key, value.lower()), "")))
    cands.sort(key=lambda x: -x[2])
    cols = ["field", "candidate_value", "alerts_it_would_add", "usable_as_input", "example_caption"]
    return cols, [list(x) for x in cands[:top_n]]


def _input_caveat(field, value):
    """Flag topics that are applied BY an ESDc (so they don't exist on the alert
    until after ESDc matching) and therefore can't be used as an ESDc input.
    Heuristic per the content team: a '+'-prefixed topic that isn't Cyber is
    mostly ESDc-applied; all others are generally present before ESDc."""
    if field == "topic":
        v = value.strip()
        if v.startswith("+") and "cyber" not in v.lower():
            return "⚠ likely ESDc-applied child topic — NOT a valid input (only if set before ESDc by model/human; most aren't)"
        return "yes"
    return ""


def run_fn_report(e: ESDC, sem: Semantics, expansion: dict, csv_text: str) -> Tuple[List[str], List[list]]:
    headers, rows = load_results_text(csv_text)
    e.logic = resolve_logic(e)
    out_topics = output_topic_names(e.name, expansion, sem)
    cap = next((h for h in headers if h.split(".")[-1].strip().lower() == "caption"), "Caption")
    top = next((h for h in headers if "topic" in h.lower()), "Internal Topics")
    ann = annotate(e, rows, sem, out_topics, cap, top)
    cols = ["alert_id", "verdict", "matched_keywords", "output_topic_present",
            "why_should_have_matched", "proposed_remedy", "source_link", "caption"]
    return cols, [[a[c] for c in cols] for a in ann]


def run_fn_sql(e: ESDC, sem: Semantics, days: int = 7, limit: int = 1000,
               ai_summary: bool = False) -> str:
    e.logic = resolve_logic(e)
    return build_full_fn_sql(e, sem, days_back=days, limit=limit, ai_summary=ai_summary)


def run_match_sql(e: ESDC, days: int = 7, limit: int = 500, ai_summary: bool = False) -> str:
    """House-style query to PULL the alerts that matched an ESDC (input for
    Alert Match Query) — same columns as the Looker export."""
    from .sql_template import build_match_extraction
    return build_match_extraction(e.name, days=days, limit=limit, ai_summary=ai_summary)


def _leaf_value(leaf: LeafNode, sem: Semantics) -> str:
    f = leaf.field.lower()
    if f in ("topicid", "pipelinetopicid"):
        return sem.topic_name(leaf.value) or f"topic {leaf.value}"
    if f == "alertthreshold":
        return sem.threshold_tier(leaf.value) or leaf.value
    if f == "geo":
        return sem.geo_name(leaf.value) or leaf.value
    return leaf.value


def _describe_node(node: Node, sem: Semantics, depth: int = 0) -> List[str]:
    ind = "    " * depth
    if is_leaf(node):
        lbl = FIELD_LABELS.get(node.field.lower(), node.field)
        return [f'{ind}- {lbl}: "{_leaf_value(node, sem)}"']
    if node.op == "NOT":
        return [f"{ind}- **NOT** — exclude the alert if:"] + _describe_node(node.clauses[0], sem, depth + 1)
    word = "**ALL** of" if node.op == "AND" else "**ANY** of"
    leaves = [c for c in node.clauses if is_leaf(c)]
    # compact case: a flat group of same-field leaves -> one line with the value list
    if len(leaves) == len(node.clauses) and len({l.field.lower() for l in leaves}) == 1:
        lbl = FIELD_LABELS.get(leaves[0].field.lower(), leaves[0].field)
        vals = [_leaf_value(l, sem) for l in leaves]
        shown = ", ".join(f'"{v}"' for v in vals[:_MAXLIST])
        more = f"  _(+{len(vals) - _MAXLIST} more)_" if len(vals) > _MAXLIST else ""
        return [f"{ind}- {word} {len(vals)} {lbl}s: {shown}{more}"]
    lines = [f"{ind}- {word}:"]
    for c in node.clauses:
        lines += _describe_node(c, sem, depth + 1)
    return lines


ATHENA_BASE = "https://apollo-test.dmnr.io/athena/ai-streams/"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace('"', "'")[:46]


def _describe_dot(node: Node, sem: Semantics) -> str:
    """Build a Graphviz DOT diagram of the logic (leaf-groups collapsed)."""
    lines, ctr = [], [0]

    def newid():
        ctr[0] += 1
        return f"n{ctr[0]}"

    def emit(n: Node) -> str:
        nid = newid()
        if is_leaf(n):
            lbl = FIELD_LABELS.get(n.field.lower(), n.field)
            lines.append(f'{nid} [label="{_esc(lbl)}\\n{_esc(_leaf_value(n, sem))}"];')
            return nid
        leaves = [c for c in n.clauses if is_leaf(c)]
        if n.op != "NOT" and leaves and len(leaves) == len(n.clauses) and \
                len({l.field.lower() for l in leaves}) == 1:
            lbl = FIELD_LABELS.get(leaves[0].field.lower(), leaves[0].field)
            word = "ALL of" if n.op == "AND" else "ANY of"
            lines.append(f'{nid} [label="{word} {len(leaves)} {_esc(lbl)}s",'
                         f' color="#57C7BC", fontcolor="#57C7BC"];')
            return nid
        op = {"AND": "AND", "OR": "OR", "NOT": "NOT (exclude)"}[n.op]
        lines.append(f'{nid} [label="{op}", color="#57C7BC", fontcolor="#57C7BC"];')
        for c in n.clauses:
            lines.append(f"{nid} -> {emit(c)};")
        return nid

    emit(node)
    body = "\n  ".join(lines)
    return (
        'digraph {\n'
        '  rankdir=LR; bgcolor="transparent"; pad=0.2;\n'
        '  node [shape=box, style="filled,rounded", fillcolor="#1E2A38", '
        'color="#2a3a4d", fontcolor="#E6EAEF", fontname="Helvetica", fontsize=11];\n'
        '  edge [color="#57C7BC", arrowsize=0.7];\n  '
        + body + "\n}"
    )


_KIND_LABEL = {
    "captionkeyword": "CAPTION KEYWORD", "originaltext": "ORIGINAL-TEXT KEYWORD",
    "translatedtext": "TRANSLATED-TEXT KEYWORD", "summarykeyword": "SUMMARY KEYWORD",
    "topicid": "TOPIC", "pipelinetopicid": "PIPELINE TOPIC",
    "modelset": "MODELSET", "alertthreshold": "THRESHOLD", "source": "SOURCE",
    "sourcechannel": "CHANNEL", "geo": "GEO", "hascompanytag": "COMPANY TAG", "freetext": "FREE TEXT",
}

_HTML_STYLE = """
<style>
.lg-canvas { background:#0f1722; background-image:radial-gradient(#243246 1px, transparent 1px);
             background-size:18px 18px; border:1px solid #2a3a4d; border-radius:10px;
             padding:14px; overflow-x:auto; }
.lg-group { border:1px solid #33465c; border-radius:10px; padding:10px 12px 12px;
            background:rgba(255,255,255,.025); margin:4px; min-width:0; }
.lg-ophdr { text-align:center; text-transform:uppercase; letter-spacing:.18em; font-weight:800;
            font-size:.74rem; padding:3px 0 9px; color:#cfe0ee; }
.lg-ophdr.lg-not { color:#ff9a9a; }
.lg-row { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start; justify-content:center; }
.lg-card { background:#1c2733; border:1px solid #33465c; border-radius:8px; padding:9px 11px;
           min-width:150px; max-width:280px; }
.lg-kind { color:#57C7BC; font-weight:800; letter-spacing:.12em; font-size:.6rem;
           text-transform:uppercase; margin-bottom:5px; }
.lg-count { color:#8aa0b4; font-weight:600; letter-spacing:.04em; }
.lg-text { font-size:.82rem; line-height:1.35; color:#E6EAEF; word-break:break-word; }
.lg-more { color:#8aa0b4; font-size:.76rem; }
.lg-det { display:inline; }
.lg-det > summary { color:#57C7BC; cursor:pointer; font-size:.76rem; display:inline;
                    list-style:none; font-weight:600; }
.lg-det > summary::-webkit-details-marker { display:none; }
.lg-det[open] > summary { color:#8aa0b4; }
</style>
"""


def _describe_html(node: Node, sem: Semantics, full=False) -> str:
    import html as _h

    def card(kind, count_txt, value):
        cnt = f' <span class="lg-count">{count_txt}</span>' if count_txt else ""
        return (f'<div class="lg-card"><div class="lg-kind">{_h.escape(kind)}{cnt}</div>'
                f'<div class="lg-text">{value}</div></div>')

    def value_html(vals, cap=8):
        vals = list(dict.fromkeys(vals))
        esc = [_h.escape(v) for v in vals]
        if full or len(esc) <= cap:
            return ", ".join(esc)
        head, rest = ", ".join(esc[:cap]), ", ".join(esc[cap:])
        return (f'{head} <details class="lg-det"><summary>+{len(esc) - cap} more</summary>'
                f', {rest}</details>')

    def render(n):
        if is_leaf(n):
            kind = _KIND_LABEL.get(n.field.lower(), n.field.upper())
            return card(kind, "", _h.escape(_leaf_value(n, sem)))
        leaves = [c for c in n.clauses if is_leaf(c)]
        if n.op != "NOT" and leaves and len(leaves) == len(n.clauses) and \
                len({l.field.lower() for l in leaves}) == 1:
            kind = _KIND_LABEL.get(leaves[0].field.lower(), leaves[0].field.upper())
            word = "all of" if n.op == "AND" else "any of"
            vals = [_leaf_value(l, sem) for l in leaves]
            return card(kind, f"· {word} {len(set(vals))}", value_html(vals))
        hdr = {"AND": "AND", "OR": "OR", "NOT": "NOT"}[n.op]
        ncls = " lg-not" if n.op == "NOT" else ""
        kids = "".join(render(c) for c in n.clauses)
        return (f'<div class="lg-group"><div class="lg-ophdr{ncls}">{hdr}</div>'
                f'<div class="lg-row">{kids}</div></div>')

    return _HTML_STYLE + '<div class="lg-canvas">' + render(node) + "</div>"


def _vlist(vals, cap=6, full=False):
    vals = list(dict.fromkeys(vals))  # dedupe, preserve order (e.g. threshold tiers)
    if full or len(vals) <= cap:
        return ", ".join(f'“{v}”' for v in vals)
    shown = ", ".join(f'“{v}”' for v in vals[:cap])
    return shown + f" _(+{len(vals) - cap} more)_"


def _leaf_phrase(leaf, sem):
    f, v = leaf.field.lower(), _leaf_value(leaf, sem)
    P = {
        "captionkeyword": f'the caption mentions “{v}”',
        "originaltext": f'the post text contains “{v}”',
        "translatedtext": f'the translated text contains “{v}”',
        "summarykeyword": f'the summary mentions “{v}”',
        "topicid": f'it’s in the topic “{v}”',
        "pipelinetopicid": f'it’s in the topic “{v}”',
        "modelset": f'it was generated by the “{v}” model',
        "alertthreshold": f'its priority is “{v}”',
        "source": f'the source is “{v}”',
        "sourcechannel": f'the channel is “{v}”',
        "geo": f'it’s located in “{v}”',
        "hascompanytag": "it’s tagged to a company",
        "freetext": f'it mentions “{v}”',
    }
    return P.get(f, f'{leaf.field} is “{v}”')


def _group_phrase(op, field, vals, negated=False, full=False):
    conj = "all of" if op == "AND" else "one of"
    lst = _vlist(vals, full=full)
    if negated:
        N = {
            "captionkeyword": f"the caption does NOT mention any of: {lst}",
            "originaltext": f"the post text does NOT contain any of: {lst}",
            "topicid": f"it’s NOT in any of these topics: {lst}",
            "pipelinetopicid": f"it’s NOT in any of these topics: {lst}",
            "modelset": f"it was NOT generated by any of these models: {lst}",
            "alertthreshold": f"its priority is NOT any of: {lst}",
            "source": f"the source is NOT any of: {lst}",
        }
        return N.get(field, f"it is NOT any of these {field}: {lst}")
    G = {
        "captionkeyword": f"the caption mentions {conj}: {lst}",
        "originaltext": f"the post text contains {conj}: {lst}",
        "translatedtext": f"the translated text contains {conj}: {lst}",
        "summarykeyword": f"the summary mentions {conj}: {lst}",
        "topicid": f"it’s in {conj} these topics: {lst}",
        "pipelinetopicid": f"it’s in {conj} these topics: {lst}",
        "modelset": f"it was generated by {conj} these models: {lst}",
        "alertthreshold": f"its priority is {conj}: {lst}",
        "source": f"the source is {conj}: {lst}",
        "sourcechannel": f"the channel is {conj}: {lst}",
        "geo": f"it’s located in {conj}: {lst}",
    }
    return G.get(field, f"{field} is {conj}: {lst}")


def _same_field_group(n):
    leaves = [c for c in n.clauses if is_leaf(c)]
    if n.op != "NOT" and leaves and len(leaves) == len(n.clauses) and \
            len({l.field.lower() for l in leaves}) == 1:
        return leaves
    return None


def _vals_of(grp, sem):
    return [_leaf_value(l, sem) for l in grp]


def _clause_sentence(n, sem, full=False):
    """One readable sentence for a clause, with inline AND/OR."""
    if is_leaf(n):
        return _leaf_phrase(n, sem)
    grp = _same_field_group(n)
    if grp:
        return _group_phrase(n.op, grp[0].field.lower(), _vals_of(grp, sem), full=full)
    if n.op == "NOT":
        child = n.clauses[0]
        cgrp = _same_field_group(child) if not is_leaf(child) else None
        if cgrp:
            return _group_phrase(child.op, cgrp[0].field.lower(), _vals_of(cgrp, sem), negated=True, full=full)
        if is_leaf(child):
            return f"it is NOT true that {_leaf_phrase(child, sem)}"
        return "it does NOT match: " + _clause_sentence(child, sem, full)
    joiner = " **AND** " if n.op == "AND" else " **OR** "
    return joiner.join(_clause_sentence(c, sem, full) for c in n.clauses)


def _category(node):
    if not is_leaf(node) and node.op == "NOT":
        return "Exclusion"
    fs = {l.field.lower() for l in _iter_leaves(node)}
    if fs <= {"topicid", "pipelinetopicid"}:
        return "Topic"
    if fs == {"captionkeyword"}:
        return "Caption keywords"
    if fs == {"originaltext"}:
        return "Original-text keywords"
    if fs <= {"captionkeyword", "originaltext", "translatedtext", "summarykeyword", "freetext"}:
        return "Keywords"
    if fs == {"modelset"}:
        return "AI model"
    if fs == {"alertthreshold"}:
        return "Priority"
    if fs <= {"source", "sourcechannel"}:
        return "Source"
    if fs == {"geo"}:
        return "Location"
    if fs & {"topicid"} and fs & {"captionkeyword", "originaltext"}:
        return "Topic or keywords"
    return "Condition"


def _is_simple(n):
    """A clause that reads as one sentence (no nested AND/OR to break out)."""
    if is_leaf(n) or _same_field_group(n):
        return True
    if n.op == "NOT":
        child = n.clauses[0]
        return is_leaf(child) or bool(_same_field_group(child))
    return False


def _node_lines(n, sem, depth, full=False):
    """Render a clause as indented markdown bullets, AND/OR groups broken out."""
    ind = "    " * depth
    if _is_simple(n):
        return [f"{ind}- {_clause_sentence(n, sem, full)}"]
    if n.op == "NOT":
        lines = [f"{ind}- it does **NOT** match any of:"]
        for c in n.clauses[0].clauses:
            lines += _node_lines(c, sem, depth + 1, full)
        return lines
    word = "ALL" if n.op == "AND" else "ANY"
    lines = [f"{ind}- **{word}** of these:"]
    for c in n.clauses:
        lines += _node_lines(c, sem, depth + 1, full)
    return lines


def _describe_plain(node: Node, sem: Semantics, full=False) -> str:
    if _is_simple(node):
        return "An alert matches when " + _clause_sentence(node, sem, full) + "."

    def top_item(prefix, c):
        if _is_simple(c):
            return f"{prefix} {_clause_sentence(c, sem, full)}"
        if c.op == "NOT":
            sub = []
            for d in c.clauses[0].clauses:
                sub += _node_lines(d, sem, 1, full)
            return f"{prefix} it does **NOT** match any of:\n" + "\n".join(sub)
        word = "ALL" if c.op == "AND" else "ANY"
        sub = []
        for d in c.clauses:
            sub += _node_lines(d, sem, 1, full)
        return f"{prefix} **{word}** of these:\n" + "\n".join(sub)

    if node.op == "AND":
        intro = "**To match, an alert must meet _every_ requirement below:**"
        items = [top_item(f"{i}. **{_category(c)}** —", c) for i, c in enumerate(node.clauses, 1)]
    else:  # OR
        intro = "**An alert matches if _any one_ of these paths is true:**"
        items = [top_item(f"{i}.", c) for i, c in enumerate(node.clauses, 1)]
    return intro + "\n\n" + "\n\n".join(items)


def describe_esdc(e: ESDC, sem: Semantics, expansion: dict, full: bool = False) -> dict:
    """Plain-language summary of what an ESDC matches and what it outputs.
    full=True shows every term in each component (no truncation)."""
    e.logic = resolve_logic(e)
    counts: Dict[str, int] = {}
    for leaf in _iter_leaves(e.logic):
        counts[leaf.field] = counts.get(leaf.field, 0) + 1
    out_topics = output_topic_names(e.name, expansion, sem)
    out_keys, powerstream = [], None
    for m in expansion["data"]["mapping"]:
        if m["esdcName"].strip() == e.name:
            out_keys = [k for k in m.get("esdcKeys", []) if k]
            powerstream = m.get("powerstreamId")
            break
    stream_id = powerstream or e.id
    return {
        "name": e.name,
        "id": e.id,
        "stemming": e.stemming,
        "field_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "plain_md": _describe_plain(e.logic, sem, full=full),
        "logic_md": "\n".join(_describe_node(e.logic, sem)),
        "logic_html": _describe_html(e.logic, sem, full=full),
        "output_topics": out_topics,
        "output_keys": out_keys,
        "athena_url": f"{ATHENA_BASE}{stream_id}" if stream_id else "",
        "parse_error": e.parse_error,
        "raw_query": e.raw_query,
    }


def _iter_leaves(node: Node):
    if is_leaf(node):
        yield node
    else:
        for c in node.clauses:
            yield from _iter_leaves(c)


def _iter_leaves_pol(node: Node, negated: bool = False):
    """Like _iter_leaves but also yields each leaf's polarity: True if the leaf
    sits under an odd number of NOT clauses (i.e. it's an EXCLUSION term)."""
    if is_leaf(node):
        yield node, negated
    else:
        flip = negated ^ (node.op == "NOT")
        for c in node.clauses:
            yield from _iter_leaves_pol(c, flip)


def git_config(pkg_dir: str) -> dict:
    """Git source-of-truth config from esdc_qa/git_config.json (+ env overrides).
    Keys: repo (local checkout path), path (file in repo), prod_ref, staged_ref."""
    import json
    import os
    cfg = {}
    p = os.path.join(pkg_dir, "git_config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    for key, env in [("repo", "ESDC_GIT_REPO"), ("path", "ESDC_GIT_PATH"),
                     ("prod_ref", "ESDC_GIT_PROD_REF"), ("staged_ref", "ESDC_GIT_STAGED_REF")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("path", "expansion.json")
    cfg.setdefault("prod_ref", "HEAD")
    cfg.setdefault("staged_ref", "")
    return cfg


REFERENCE_FILES = {
    "Topic abstractions": "Topic Abstractions 3.0 - Official Abstractions Copy.csv",
    "Threshold mapping": "Alert Threshold Mapping - Sheet1.csv",
    "Geo GUIDs": "Geo GUIDs.txt",
    "Internal education doc": "ESDc - Internal Education 2024.txt",
}


def reference_tables(sem: Semantics) -> dict:
    """Browsable reference tables built from the loaded semantics."""
    topics = ([["DE GUID", "Topic name"]],
              sorted(([g, n] for g, n in sem.topic_id_to_name.items()), key=lambda r: r[1].lower()))
    thresholds = ([["Threshold code", "Tier"]],
                  sorted(([c, t] for c, t in sem.threshold_code_to_tier.items()), key=lambda r: r[0]))
    geo = ([["GUID", "Place"]],
           sorted(([g, n] for g, n in sem.geo_guid_to_name.items()), key=lambda r: r[1].lower()))
    return {
        "Topic abstractions": {"cols": topics[0][0], "rows": topics[1]},
        "Threshold mapping": {"cols": thresholds[0][0], "rows": thresholds[1]},
        "Geo GUIDs": {"cols": geo[0][0], "rows": geo[1]},
    }


EDUCATION_MD = """\
## ESDc — Enhanced Stream Definition capability

A **rules engine** that enriches alerts after HITL/draft or modelset auto-alert
generation. The enrichments (internal topics and/or alert reference terms) are
used to **route alerts to customers more effectively** and reduce noise.

**Who uses it:** Content Engineers, working with Content Owners (PMs / DE Leads).
**Where:** Athena → *ESDc* tab (view/edit access gated). It borrows the AI Streams
UI, so ESDcs can be complex nested booleans using **AND / OR / NOT** — but they do
*not* inherit AI Streams' filters.

### Matchable fields (component prefixes)

| Field | Syntax | Notes |
|---|---|---|
| Internal Topics | `topicId:{internalTopicID}` | from the internal topic list |
| Caption Keywords | `captionKeyword:{"term"}` | **up to trigrams**; space-delimited; **no substring** matching |
| Summary Keywords | `summaryKeyword:{"term"}` | matches the **AI caption summary** (Alert Caption AI Summary); single words need no quotes |
| Modelset | `modelset:{idOrName}` | evolving list from R&D |
| Source | `source:{theSource}` | e.g. `shodan`, `tg` |
| Source Channel | `sourceChannel:{channel}` | e.g. CHATTER, ENTERTAINMENT |
| Original Text | `originalText:{term}` | **up to trigrams**; **case-sensitive**; substrings allowed |
| Alert Threshold | `alertThreshold:[code]` | `general.alert` syntax (alert3=Signal, alert4=Local Signal, alert5=Hyperspecific) |
| Geo | `geo:[GUID]` | country GUIDs |
| ~~Pipeline Topic~~ | ~~`pipelineTopicId:[topicId]`~~ | ~~internal topic list~~ — **never created / not available** |

### Outputs
Internal **topics** and/or alert **reference terms** when the ESDc matches.

### Constraints
- Limited to the matchable fields above (more on request).
- The character `$` is not accepted.
- Keyword phrases are limited to **trigrams (≤ 3 words)** — longer phrases never match.

_This is a cleaned summary. Download the original doc below for the full text._
"""


def education_markdown() -> str:
    return EDUCATION_MD


def read_reference_doc(pkg_dir: str, filename: str) -> str:
    import os
    path = os.path.join(pkg_dir, filename)
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except FileNotFoundError:
        return f"(not found: {filename})"


def _values_by_field(e: ESDC, sem: Semantics) -> Dict[str, set]:
    """{readable field label: set of (resolved) values} for an ESDC's leaves."""
    e.logic = resolve_logic(e)
    LABEL = {
        "captionkeyword": "caption keyword", "originaltext": "original text",
        "translatedtext": "translated text", "summarykeyword": "summary keyword",
        "modelset": "modelset", "source": "source", "sourcechannel": "channel",
        "hascompanytag": "company tag", "freetext": "free text",
    }
    out: Dict[str, set] = {}
    for l, negated in _iter_leaves_pol(e.logic):
        f = l.field.lower()
        if f in ("topicid", "pipelinetopicid"):
            lab, val = "topic", (sem.topic_name(l.value) or l.value)
        elif f == "alertthreshold":
            lab, val = "threshold", (sem.threshold_tier(l.value) or l.value)
        elif f == "geo":
            lab, val = "geo", (sem.geo_name(l.value) or l.value)
        else:
            lab, val = LABEL.get(f, l.field), l.value
        # Tag exclusion (NOT) leaves separately so the diff never conflates an
        # added exclusion (which NARROWS matches) with an added inclusion.
        if negated:
            lab = lab + EXCLUDE_SUFFIX
        out.setdefault(lab, set()).add(val)
    return out


EXCLUDE_SUFFIX = " — EXCLUDE (NOT)"


def _by_id(esdcs: Dict[str, ESDC]) -> Dict[str, ESDC]:
    """Re-key ESDCs by their stable id (falling back to name) so prod/staged
    match even if the name has whitespace/`&`/casing differences."""
    return {(e.id or name): e for name, e in esdcs.items()}


def _output_map(raw: dict, sem: Semantics) -> Dict[str, dict]:
    """{powerstreamId: {name, topics(resolved), keys}} from a raw expansion's
    mapping — i.e. the OUTPUT topics and reference terms each ESDc applies."""
    out = {}
    for m in (raw.get("data", {}) or {}).get("mapping", []) or []:
        key = str(m.get("powerstreamId") or m.get("esdcId") or m.get("esdcName") or "").strip()
        if not key:
            continue
        topics = set()
        for t in m.get("internalTopicIds", []) or []:
            t = str(t).strip()
            if t:
                topics.add(sem.topic_name(t) or t)
        keys = {str(k).strip() for k in (m.get("esdcKeys", []) or []) if str(k).strip()}
        out[key] = {"name": (m.get("esdcName", "") or "").strip(), "topics": topics, "keys": keys}
    return out


_APPROVE = "⚠ PRODUCT APPROVAL"

# Text fields match by containment, so a shorter term can SUBSUME a longer one:
# any alert text containing "estate of george harrison" also contains the
# word-bounded phrase "george harrison". Removing the longer term while a
# subsuming term remains therefore loses NO coverage — don't call it a narrowing.
_WORDBOUNDARY_LABELS = {"caption keyword", "summary keyword"}
_SUBSTRING_LABELS = {"original text", "translated text", "free text"}
_TEXT_LABELS = _WORDBOUNDARY_LABELS | _SUBSTRING_LABELS


def _subsumes(broad: str, term: str, label: str) -> bool:
    """True if a remaining term `broad` still fires on any text that matched the
    removed `term` (so removing `term` loses no coverage). Containment direction:
    the broader (shorter) phrase must be found inside the removed phrase."""
    b, t = broad.strip(), term.strip()
    if not b or b.lower() == t.lower():
        return False
    if label in _WORDBOUNDARY_LABELS:
        return _word_present(b, t)        # word-bounded containment (caption engine)
    if label in _SUBSTRING_LABELS:
        return b.lower() in t.lower()     # plain substring containment
    return False


def compare_definitions(prod: Dict[str, ESDC], staged: Dict[str, ESDC], sem: Semantics,
                        prod_raw: dict = None, staged_raw: dict = None
                        ) -> Tuple[dict, List[str], List[list]]:
    """Diff staged vs prod ESDC definitions (matched by id). Returns
    (summary, columns, rows): one row per change. The `needs_product_approval`
    column flags net-new/deleted ESDCs and output-topic / reference-term changes.
    Output diffs require prod_raw + staged_raw (the full expansion JSON)."""
    P, S = _by_id(prod), _by_id(staged)
    pol = _output_map(prod_raw, sem) if prod_raw else {}
    sol = _output_map(staged_raw, sem) if staged_raw else {}
    # `effect` is a plain-English read of the row so the table is self-explanatory
    # at a glance; `change` is a short clear category (direction-aware).
    cols = ["esdc", "change", "field", "added", "removed", "effect", "needs_product_approval"]
    rows = []
    n_added = n_removed = n_modified = 0
    for key in sorted(set(P) | set(S)):
        pe, se = P.get(key), S.get(key)
        if pe and not se:
            n_removed += 1
            rows.append([pe.name, "ESDC deleted", "(whole rule)", "", "(entire rule)",
                         "This ESDC is removed — it will no longer match or tag any alerts.",
                         _APPROVE + " — deleted ESDC"])
            continue
        if se and not pe:
            n_added += 1
            rows.append([se.name, "ESDC added (new)", "(whole rule)", "(entire rule)", "",
                         "Brand-new ESDC — it will start matching and tagging alerts.",
                         _APPROVE + " — net-new ESDC"])
            continue
        name, changed = se.name, False
        if pe.name != se.name:  # same id, renamed
            changed = True
            rows.append([name, "Renamed", "(name)", se.name, pe.name,
                         f"Display name changed from “{pe.name}” to “{se.name}” (no matching impact).", ""])
        pv, sv = _values_by_field(pe, sem), _values_by_field(se, sem)
        for field in sorted(set(pv) | set(sv)):
            added = sorted(sv.get(field, set()) - pv.get(field, set()))
            removed = sorted(pv.get(field, set()) - sv.get(field, set()))
            if not (added or removed):
                continue
            changed = True
            if field.endswith(EXCLUDE_SUFFIX):
                # Inside a NOT clause: meaning is inverted vs a normal include.
                # Adding an exclusion term NARROWS coverage (drops more alerts);
                # removing one BROADENS coverage.
                base = field[: -len(EXCLUDE_SUFFIX)]
                verb = ("Add" if added and not removed else
                        "Remove" if removed and not added else "Edit")
                change = f"{verb} exclusion (NOT) · {base}"
                eff = []
                if added:
                    eff.append(f"NARROWS coverage — alerts whose {base} contains "
                               f"[{', '.join(added)}] are now filtered OUT.")
                if removed:
                    eff.append(f"BROADENS coverage — [{', '.join(removed)}] are no longer "
                               f"excluded, so those alerts can match again.")
                rows.append([name, change, base, ", ".join(added), ", ".join(removed),
                             " ".join(eff), ""])
            else:
                # An include term that's only being REWORDED into a broader form
                # (e.g. "Estate of George Harrison" → "George Harrison") loses no
                # coverage: split removed terms into truly-gone vs subsumed by a
                # remaining include term in the same field.
                staged_incl = sv.get(field, set())
                if removed and field in _TEXT_LABELS:
                    subsumed = [r for r in removed
                                if any(_subsumes(b, r, field) for b in staged_incl)]
                    sset = set(subsumed)
                    genuine = [r for r in removed if r not in sset]
                else:
                    subsumed, genuine = [], removed
                verb = ("Add" if added and not genuine else
                        "Remove" if genuine and not added else "Edit")
                change = f"{verb} match term · {field}"
                eff = []
                if added:
                    eff.append(f"BROADENS coverage — alerts now also match when {field} "
                               f"contains [{', '.join(added)}].")
                if subsumed:
                    eff.append(f"No coverage loss — [{', '.join(subsumed)}] is now covered by a "
                               f"broader remaining {field} term (anything matching it still matches).")
                if genuine:
                    eff.append(f"NARROWS coverage — alerts that matched only on [{', '.join(genuine)}] "
                               f"for {field} will stop matching.")
                rows.append([name, change, field, ", ".join(added), ", ".join(removed),
                             " ".join(eff), ""])
        po, so = pol.get(key, {}), sol.get(key, {})
        for ofield, okey in [("output topic", "topics"), ("reference term", "keys")]:
            oa = sorted(so.get(okey, set()) - po.get(okey, set()))
            orem = sorted(po.get(okey, set()) - so.get(okey, set()))
            if oa or orem:
                changed = True
                what = ("topic(s) this ESDC tags onto matched alerts" if okey == "topics"
                        else "reference term(s) this ESDC outputs")
                eff = []
                if oa:
                    eff.append(f"Now outputs [{', '.join(oa)}].")
                if orem:
                    eff.append(f"No longer outputs [{', '.join(orem)}].")
                rows.append([name, f"Output change · {ofield}", ofield,
                             ", ".join(oa), ", ".join(orem),
                             f"Changes the {what}. " + " ".join(eff),
                             _APPROVE + f" — {ofield} output change"])
        if changed:
            n_modified += 1
    summary = {"changed_esdcs": n_modified, "added_esdcs": n_added, "removed_esdcs": n_removed,
               "diff_rows": len(rows), "needs_approval": sum(1 for r in rows if r[6])}
    return summary, cols, rows


def staged_change_summary_text(summary: dict, rows: List[list]) -> str:
    """Plain-text changelog of the staged diff, approval items called out first."""
    L = ["STAGED vs PRODUCTION — ESDc definition changes",
         "=" * 46,
         f"{summary['changed_esdcs']} modified · {summary['added_esdcs']} added · "
         f"{summary['removed_esdcs']} removed · {summary['needs_approval']} need product approval."]
    appr = [r for r in rows if r[6]]
    if appr:
        L += ["", f"!! NEEDS PRODUCT APPROVAL ({len(appr)}):"]
        for esdc, change, field, added, removed, effect, _ in appr:
            L.append(f"  - {esdc} — {change}")
            L.append(f"      {effect}")
    other = [r for r in rows if not r[6]]
    if other:
        L += ["", "Logic / input changes (no product approval needed):"]
        for esdc, change, field, added, removed, effect, _ in other:
            L.append(f"  - {esdc} — {change}")
            L.append(f"      {effect}")
    if not rows:
        L.append("\nNo differences between staged and production.")
    return "\n".join(L)


def run_staged_impact(prod: Dict[str, ESDC], staged: Dict[str, ESDC], sem: Semantics,
                      csv_text: str) -> Tuple[List[str], List[list]]:
    """Behavioral impact of staged vs prod on an uploaded alert sample.

    Prod ground truth = the alert's actual `ESDC Names` tag (what prod really
    applied), NOT a re-evaluation — so we never claim to "lose" an alert that
    was never tagged. Staged is evaluated (it isn't deployed). Reports flips:
      LOST   = alert WAS tagged by prod, but the staged rule no longer matches it
      GAINED = alert was NOT tagged, the staged rule matches it, and prod logic
               did not — i.e. the staged change is what newly captures it
    """
    headers, rows = load_results_text(csv_text)
    ccol = _caption_col(headers)
    ecol = esdc_names_column(headers)
    P, S = _by_id(prod), _by_id(staged)
    to_check = []
    for key in sorted(set(P) | set(S)):
        pe, se = P.get(key), S.get(key)
        if pe and se and _values_by_field(pe, sem) == _values_by_field(se, sem):
            continue  # unchanged logic
        to_check.append(key)
    cols = ["esdc", "alert_id", "change", "prod_tagged", "staged_match", "reason", "caption"]
    out = []
    for key in to_check:
        pe, se = P.get(key), S.get(key)
        disp = (se or pe).name
        prod_name = pe.name if pe else None  # ESDC Names carries prod names
        if pe:
            pe.logic = resolve_logic(pe)
        if se:
            se.logic = resolve_logic(se)
        for r in rows:
            aid = _alert_id(r, headers)
            cap = str(r.get(ccol, "") or "").replace("\n", " ")[:200]
            tagged = bool(prod_name) and prod_name in set(which_esdcs(r, ecol))
            sres = evaluate(se.logic, r, sem, headers) if se else None
            sm = sres.matched if sres else False
            if tagged and not sm:
                pres = evaluate(pe.logic, r, sem, headers) if pe else None
                out.append([disp, aid, "LOST", "yes", "no",
                            (pres.why() if pres else "previously tagged"), cap])
            elif not tagged and sm:
                pres = evaluate(pe.logic, r, sem, headers) if pe else None
                if not (pres and pres.matched):  # change is what captures it
                    out.append([disp, aid, "GAINED", "no", "yes",
                                (sres.why() if sres else ""), cap])
    return cols, out


def _leaf_label(leaf: LeafNode, sem: Semantics) -> str:
    """Readable 'field "value"' for a leaf, resolving topic/threshold/geo names."""
    f = leaf.field.lower()
    if f in ("topicid", "pipelinetopicid"):
        return f'topic "{sem.topic_name(leaf.value) or leaf.value}"'
    if f == "alertthreshold":
        return f'threshold "{sem.threshold_tier(leaf.value) or leaf.value}"'
    if f == "geo":
        return f'geo "{sem.geo_name(leaf.value) or leaf.value}"'
    return f'{leaf.field} "{leaf.value}"'


def _leaf_delta(pe: ESDC, se: ESDC):
    """Leaf-level diff between two definitions, polarity-aware. Returns four
    lists of LeafNodes: (added_incl, removed_incl, added_excl, removed_excl)."""
    pk = {(l.field.lower(), l.value, neg): l for l, neg in _iter_leaves_pol(pe.logic)} if pe else {}
    sk = {(l.field.lower(), l.value, neg): l for l, neg in _iter_leaves_pol(se.logic)} if se else {}
    added_incl = [l for k, l in sk.items() if k not in pk and not k[2]]
    removed_incl = [l for k, l in pk.items() if k not in sk and not k[2]]
    added_excl = [l for k, l in sk.items() if k not in pk and k[2]]
    removed_excl = [l for k, l in pk.items() if k not in sk and k[2]]
    return added_incl, removed_incl, added_excl, removed_excl


def _fired_leaves(leaves: List[LeafNode], row, sem, headers) -> List[str]:
    """Of these leaves, the ones that evaluate TRUE on this alert, as labels
    with matched evidence (so the reason mirrors the Alert Match output)."""
    out = []
    for l in leaves:
        le = _eval_leaf(l, row, sem, headers)
        if le.state == TRUE:
            ev = f' → "{le.evidence}"' if le.evidence else ""
            out.append(_leaf_label(l, sem) + ev)
    return out


def run_staged_impact_explain(prod: Dict[str, ESDC], staged: Dict[str, ESDC], sem: Semantics,
                              esdc_name: str, csv_text: str) -> Tuple[List[str], List[list]]:
    """Explain WHY each alert in a stage-vs-prod impact result flipped GAINED/LOST.

    Drop in the CSV produced by the Step 3 impact query. For each alert we
    re-evaluate the prod and staged definitions and attribute the flip to the
    specific definition delta the alert hit — mirroring the Alert Match output
    (which term fired, with matched evidence):

      GAINED (staged matches, prod didn't) ← alert matched a newly-ADDED include
        term, and/or prod had EXCLUDED it via a NOT term that staging removed.
      LOST   (prod matched, staged doesn't) ← a newly-ADDED exclusion (NOT) term
        now fires on the alert, and/or it only matched an include term staging removed.
    """
    headers, rows = load_results_text(csv_text)
    ccol = _caption_col(headers)
    P, S = _by_id(prod), _by_id(staged)
    key = next((k for k, e in {**P, **S}.items() if e.name == esdc_name), None)
    pe, se = (P.get(key), S.get(key)) if key else (prod.get(esdc_name), staged.get(esdc_name))
    if pe:
        pe.logic = resolve_logic(pe)
    if se:
        se.logic = resolve_logic(se)
    add_incl, rem_incl, add_excl, rem_excl = _leaf_delta(pe, se)

    cols = (["esdc", "alert_id", "impact", "why_changed", "prod_match", "staged_match", "caption"]
            + [name for name, _ in _FIELD_COLS])
    out = []
    for r in rows:
        aid = _alert_id(r, headers)
        cap = str(r.get(ccol, "") or "").replace("\n", " ")[:240]
        pres = evaluate(pe.logic, r, sem, headers) if pe else None
        sres = evaluate(se.logic, r, sem, headers) if se else None
        pm = bool(pres and pres.matched)
        sm = bool(sres and sres.matched)
        if sm and not pm:
            impact = "GAINED"
            bits = []
            fired_add = _fired_leaves(add_incl, r, sem, headers)
            if fired_add:
                bits.append("now matches new term(s): " + "; ".join(fired_add))
            fired_relax = _fired_leaves(rem_excl, r, sem, headers)
            if fired_relax:
                bits.append("prod had excluded it (NOT removed in stage): " + "; ".join(fired_relax))
            why = " · ".join(bits) or "staged definition now matches; prod did not"
        elif pm and not sm:
            impact = "LOST"
            bits = []
            fired_excl = _fired_leaves(add_excl, r, sem, headers)
            if fired_excl:
                bits.append("now EXCLUDED by new NOT term(s): " + "; ".join(fired_excl))
            fired_drop = _fired_leaves(rem_incl, r, sem, headers)
            if fired_drop:
                bits.append("only matched prod term(s) removed in stage: " + "; ".join(fired_drop))
            why = " · ".join(bits) or "prod definition matched; staged no longer does"
        else:
            # No flip — keep it in the output but labelled, so an unexpected row
            # in the dropped CSV is visible rather than silently dropped.
            impact = "NO CHANGE" if pm == sm else "?"
            why = "both match" if pm and sm else "neither matches"
        # field breakdown reflects the staged eval for GAINED/NO-CHANGE, prod for LOST
        ref = sres if (sm or not pm) else pres
        cells = _field_cells(ref, r, headers) if ref else [""] * len(_FIELD_COLS)
        out.append([(se or pe).name, aid, impact, why,
                    "yes" if pm else "no", "yes" if sm else "no", cap] + cells)
    return cols, out


def run_staged_impact_sql(prod: Dict[str, ESDC], staged: Dict[str, ESDC], sem: Semantics,
                          esdc_name: str, days: int = 7, limit: int = 1000) -> str:
    """System-wide impact query for one ESDC (alerts that flip GAINED/LOST).
    Resolves the prod/staged counterparts by id so a rename still pairs up."""
    from .ast_to_sql import build_staged_impact_sql
    P, S = _by_id(prod), _by_id(staged)
    key = next((k for k, e in {**P, **S}.items() if e.name == esdc_name), None)
    pe, se = (P.get(key), S.get(key)) if key else (prod.get(esdc_name), staged.get(esdc_name))
    if pe:
        pe.logic = resolve_logic(pe)
    if se:
        se.logic = resolve_logic(se)
    return build_staged_impact_sql(esdc_name, pe, se, sem, days_back=days, limit=limit)


def changed_esdc_names(prod: Dict[str, ESDC], staged: Dict[str, ESDC], sem: Semantics) -> List[str]:
    """Names of ESDCs whose definition differs between prod and staged (matched by id)."""
    P, S = _by_id(prod), _by_id(staged)
    out = []
    for key in sorted(set(P) & set(S)):
        pe, se = P[key], S[key]
        if pe.name != se.name or _values_by_field(pe, sem) != _values_by_field(se, sem):
            out.append(se.name)
    return out


def run_audit(esdcs: Dict[str, ESDC], sem: Semantics) -> Tuple[List[str], List[list]]:
    cols = ["name", "status", "unverifiable", "trigram"]
    rows = []
    for name, e in esdcs.items():
        try:
            e.logic = resolve_logic(e)
        except Exception as ex:
            rows.append([name, "PARSE_FAIL", "", str(ex)[:60]])
            continue
        rep = compile_report(e, sem)
        status = ("HAS_UNVERIFIABLE_FIELDS" if rep["unverifiable"]
                  else "HAS_TRIGRAM_BUG" if rep["trigram"] else "FULLY_VERIFIABLE")
        rows.append([name, status, " | ".join(rep["unverifiable"]), " | ".join(rep["trigram"])])
    return cols, rows
