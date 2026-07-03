"""ESDc QA — Streamlit UI.

Run:  streamlit run app.py

Theme matches the Athena ESDC builder (dark navy + slate + teal/blue).
"""
import json
import os

import pandas as pd
import streamlit as st

from esdc_qa.expansion_loader import load_expansion, load_expansion_text
from esdc_qa.loader import git_show
from esdc_qa.runners import (REFERENCE_FILES, changed_esdc_names, compare_definitions,
                             describe_esdc, education_markdown, esdcs_in_csv, git_config,
                             pick_esdc, read_reference_doc, reference_tables, run_audit,
                             run_diagnose, run_explain_multi, run_fn_report, run_fn_sql,
                             run_match_sql, run_staged_impact, run_staged_impact_explain,
                             run_staged_impact_sql, run_suggest_terms,
                             staged_change_summary_text)
from esdc_qa.semantics import load_semantics


def _jload(text):
    return json.loads(text[1:] if text[:1] == "﻿" else text)

PKG = os.path.join(os.path.dirname(__file__), "esdc_qa")
EXPANSION_PATH = os.path.join(PKG, "expansion.json")
LOOKER = "https://dataminr.looker.com/dashboards/5718"
LOOK_ALERT = "https://dataminr.looker.com/x/sORb5R6nRSAwCs5wjOhkqQ"
LOOK_ESDC = "https://dataminr.looker.com/x/wIWIdul1xbhg3rlAMOWZXY"

TEAL = "#57C7BC"
st.set_page_config(page_title="ESDc QA", page_icon="🛰️", layout="wide")
st.markdown(f"""
<style>
  .block-container {{ padding-top: 2.2rem; }}
  h1, h2, h3 {{ letter-spacing: .02em; }}
  .esdc-tag {{ color:{TEAL}; font-weight:700; letter-spacing:.14em; font-size:.72rem; }}
  .esdc-title {{ font-size:1.6rem; font-weight:700; }}
  .esdc-rule {{ border-bottom:2px solid {TEAL}; width:64px; margin:.2rem 0 1.1rem; }}
  .out-topic {{ display:inline-block; background:#143; color:{TEAL}; border:1px solid {TEAL};
               border-radius:6px; padding:3px 10px; margin:3px 4px 3px 0; font-size:.85rem; }}
  .stButton>button[kind="primary"] {{ font-weight:700; letter-spacing:.04em; }}
  section[data-testid="stSidebar"] {{ border-right:1px solid #2a3a4d; }}
  div[data-testid="stDataFrame"] {{ border:1px solid #2a3a4d; border-radius:8px; }}
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def _load_sem():
    return load_semantics(PKG)


@st.cache_resource
def _load_prod():
    """Prod definitions — Git source of truth if configured, else bundled file."""
    cfg = git_config(PKG)
    if cfg.get("repo"):
        try:
            blob = git_show(cfg["repo"], cfg["path"], cfg["prod_ref"])
            return load_expansion_text(blob), _jload(blob), f"Git · {cfg['prod_ref']}"
        except Exception as e:
            with open(EXPANSION_PATH, encoding="utf-8-sig") as f:
                raw = f.read()
            return load_expansion_text(raw), _jload(raw), f"bundled (Git unavailable: {type(e).__name__})"
    with open(EXPANSION_PATH, encoding="utf-8-sig") as f:
        raw = f.read()
    # Git prod integration pending — bundled expansion.json is the source of truth for now.
    return load_expansion_text(raw), _jload(raw), "source-of-truth file (expansion.json)"


@st.cache_resource
def _load_staged():
    """Staged definitions — Git staged_ref if configured, else project file, else none."""
    cfg = git_config(PKG)
    if cfg.get("repo") and cfg.get("staged_ref"):
        try:
            blob = git_show(cfg["repo"], cfg["path"], cfg["staged_ref"])
            return load_expansion_text(blob), _jload(blob), f"Git · {cfg['staged_ref']}"
        except Exception as e:
            return None, None, f"Git staged unavailable ({type(e).__name__})"
    sp = os.path.join(PKG, "expansion_staged.json")
    if os.path.exists(sp):
        with open(sp, encoding="utf-8-sig") as f:
            raw = f.read()
        return load_expansion_text(raw), _jload(raw), "project file (expansion_staged.json)"
    return None, None, "not configured"


SEM = _load_sem()
PROD_ESDCS, PROD_EXPANSION, PROD_SRC = _load_prod()
STAGED_ESDCS, STAGED_EXPANSION, STAGED_SRC = _load_staged()
# active definitions (set by the sidebar Definitions toggle); default to prod
ESDCS, EXPANSION = PROD_ESDCS, PROD_EXPANSION

MODES = {
    "Understand an ESDC": (
        "See what an ESDC's logic requires and what it outputs.",
        "No CSV needed — pick an ESDC. Shows the plain-language logic, a visual "
        "diagram, the fields it matches on, what it outputs, and a link to Athena."),
    "Alert Match Query": (
        "Explain why a specific alert matched an ESDC (e.g. the Banking ESDC).",
        f"This explains alerts that **already matched** — it is *not* a false-negative finder "
        f"(use **False Negatives** for that).\n\n"
        f"1. Pull data from Looker — [Alert lookup]({LOOK_ALERT}) (by Alert ID) or "
        f"[ESDc lookup]({LOOK_ESDC}) (by ESDC Name), or [dashboard 5718]({LOOKER}). "
        f"No Looker? Use the **Pull matched alerts (Snowflake)** panel below.\n"
        "2. Export as CSV → drop it below → per-leaf explanation of *why* each alert matched."),
    "Why didn't it match?": (
        "Drop in alert(s) and see why they did NOT match a chosen ESDC, with a fix.",
        "Same CSV format as Alert Match Query. For each alert it shows which clause "
        "failed (missing keyword, wrong topic, excluded by a NOT, …) and a suggested "
        "fix consistent with the ESDC definition.\n\n"
        "_Use this when you already have the alerts. To FIND missed alerts across the "
        "whole system, use **Find Missed Alerts**._"),
    "Find Missed Alerts (false negatives)": (
        "Scan the system for alerts that should have matched but weren't tagged (existing definition only).",
        "A false negative = an alert that satisfies the entire rule yet wasn't tagged "
        "with the ESDC. Two steps, both here:\n"
        "1. **Generate** the Snowflake query for the whole rule and run it in Snowflake.\n"
        "2. **Upload** those results to get a per-alert verdict + proposed remedy.\n\n"
        "⚠️ **Existing definition only** — this finds alerts that satisfy the rule *as written* "
        "but weren't tagged. It does **not** find alerts that arguably *should* match by intent "
        "but fall outside the current logic (broadening the definition is a separate exercise).\n\n"
        "_Difference vs **Why didn't it match?**: that diagnoses alerts you already have; "
        "this scans the whole system to surface ones you've missed._"),
    "Suggest new terms": (
        "Mine missed alerts for additions across ALL logic fields (broaden coverage).",
        "Upload alerts that should be in scope (e.g. topic-filtered, or FN-scan output). "
        "From alerts the ESDC **doesn't currently catch**, the tool proposes candidate "
        "additions for **every field type** — caption keyword, original text, topic, "
        "modelset, source, channel, geo — i.e. values present in those alerts that aren't "
        "yet in the definition, ranked by how many alerts each would add.\n\n"
        "⚠️ Suggestions are statistical — review each before adding; keywords are ≤3 words (trigram limit)."),
    "Library audit": (
        "Check which ESDCs' FN queries are fully accurate across the whole library.",
        "No input needed — flags ESDCs with unverifiable fields or trigram rule-bugs."),
    "Test staged changes": (
        "Compare stage (unreleased) ESDc definitions against production and see the impact.",
        "Compares a **stage** definition against the **production** baseline — fully isolated "
        "(neither file is modified).\n\n"
        "**Where to get the files**\n"
        "- **Production** (`esdc_qa/expansion.json`, the baseline): the source-of-truth file "
        "(Git prod integration pending). Refresh it periodically so the baseline stays current.\n"
        "- **Stage** (the changed definitions) — any of:\n"
        "    1. **Stage export** — if your edits were loaded into stage via a previous stage "
        "upload, download the expansions file from stage.\n"
        "    2. **Athena export** — download the expansions file from Athena (needs Athena permissions).\n"
        "    3. **Athena = prod shortcut** — if the Athena file was recently released to prod with "
        "**zero changes since**, it's identical to the Git-prod file and can be used directly — "
        "but only if you have Athena permissions **and** are certain nothing has changed since the release.\n\n"
        "**Steps:** 1) definition diff · 2) sample impact (upload alerts → gained/lost/unchanged) · "
        "3) system-wide impact SQL."),
}
MODE_LIST = list(MODES)
REF_LIST = ["Topic abstractions", "Threshold mapping", "Geo GUIDs", "Internal education doc"]

# display labels (underlying keys/logic unchanged)
DISPLAY_LABELS = {
    "Alert Match Query": "Alert Check — Why did it match?",
    "Why didn't it match?": "Alert Check — Why didn't it match?",
    "Suggest new terms": "Suggest ESDc definition expansions (WIP)",
    "Test staged changes": "Test stage changes",
}


def _label(m):
    return DISPLAY_LABELS.get(m, m)
if "page" not in st.session_state:
    st.session_state.page = MODE_LIST[0]

st.markdown('<div class="esdc-tag">DATAMINR · ESDC QA</div>', unsafe_allow_html=True)
st.markdown('<div class="esdc-title">ESDc QA — match explainer & false-negative finder</div>', unsafe_allow_html=True)
st.markdown('<div class="esdc-rule"></div>', unsafe_allow_html=True)


def _go_mode():
    st.session_state.page = st.session_state.nav_mode
    st.session_state.nav_ref = None


def _go_ref():
    st.session_state.page = st.session_state.nav_ref
    st.session_state.nav_mode = None


with st.sidebar:
    st.markdown('<div class="esdc-tag">ANALYSIS</div>', unsafe_allow_html=True)
    st.radio("Mode", MODE_LIST, key="nav_mode", on_change=_go_mode, format_func=_label,
             index=MODE_LIST.index(st.session_state.page) if st.session_state.page in MODE_LIST else None,
             label_visibility="collapsed")
    st.markdown('<div class="esdc-tag" style="margin-top:10px">REFERENCE</div>', unsafe_allow_html=True)
    st.radio("Reference", REF_LIST, key="nav_ref", on_change=_go_ref,
             index=REF_LIST.index(st.session_state.page) if st.session_state.page in REF_LIST else None,
             label_visibility="collapsed")

    page = st.session_state.page
    mode = page if page in MODES else None
    ref = page if page in REF_LIST else None

    st.divider()
    # Global definitions source — every analysis mode runs against this.
    st.markdown('<div class="esdc-tag">DEFINITIONS</div>', unsafe_allow_html=True)
    defs = st.radio("Definitions", ["Production", "Stage"], horizontal=True,
                    label_visibility="collapsed")

    # session-uploaded staged overrides Git/project, and persists across modes
    if st.session_state.get("staged_up_esdcs"):
        STAGED_ESDCS = st.session_state["staged_up_esdcs"]
        STAGED_EXPANSION = st.session_state["staged_up_expansion"]
        STAGED_SRC = st.session_state["staged_up_src"]

    if defs == "Stage":
        if not STAGED_ESDCS:
            st.caption(f"Stage not configured ({STAGED_SRC}). Load a stage expansion file:")
            up_staged = st.file_uploader("Stage expansion.json", type=["json"], key="staged_global_up")
            if up_staged is not None:
                try:
                    t = up_staged.getvalue().decode("utf-8-sig")
                    st.session_state["staged_up_esdcs"] = load_expansion_text(t)
                    st.session_state["staged_up_expansion"] = _jload(t)
                    st.session_state["staged_up_src"] = "uploaded (Athena)"
                    STAGED_ESDCS = st.session_state["staged_up_esdcs"]
                    STAGED_EXPANSION = st.session_state["staged_up_expansion"]
                    STAGED_SRC = st.session_state["staged_up_src"]
                except Exception as e:
                    st.error(f"Couldn't parse staged file: {type(e).__name__}: {e}")
        if STAGED_ESDCS:
            ESDCS, EXPANSION = STAGED_ESDCS, STAGED_EXPANSION
            active_src = f"STAGE · {STAGED_SRC}"
            if st.session_state.get("staged_up_esdcs") and st.button("Clear uploaded stage file", key="clr_staged"):
                for k in ("staged_up_esdcs", "staged_up_expansion", "staged_up_src"):
                    st.session_state.pop(k, None)
                st.rerun()
        else:
            ESDCS, EXPANSION = PROD_ESDCS, PROD_EXPANSION
            active_src = f"PROD · {PROD_SRC} (no stage loaded)"
    else:
        ESDCS, EXPANSION = PROD_ESDCS, PROD_EXPANSION
        active_src = f"PROD · {PROD_SRC}"
    st.caption(f"Active: **{active_src}**")

    st.divider()
    esdc_name = None
    # Alert Match Query picks ESDC(s) in-panel (multi); other modes use one here
    if mode in ("Understand an ESDC", "Find Missed Alerts (false negatives)",
                "Why didn't it match?", "Suggest new terms"):
        st.markdown('<div class="esdc-tag">ESDC</div>', unsafe_allow_html=True)
        esdc_name = st.selectbox("ESDC", sorted(ESDCS), label_visibility="collapsed")
    st.divider()
    st.markdown('<div class="esdc-tag">GET TEST DATA (LOOKER)</div>', unsafe_allow_html=True)
    st.markdown(f"- [Alert lookup look]({LOOK_ALERT}) — by Alert ID\n"
                f"- [ESDc lookup look]({LOOK_ESDC}) — by ESDC Name\n"
                f"- [Dashboard 5718]({LOOKER})")

def _df(cols, rows):
    return pd.DataFrame(rows, columns=cols)


def _download(df, name):
    st.download_button("⬇ Download CSV", df.to_csv(index=False).encode("utf-8"),
                       file_name=name, mime="text/csv", type="primary")


def _show_sql(sql, fname):
    """Render generated SQL. Large queries are shown as plain text (no syntax
    highlighting) to keep the browser responsive — highlighting a big string
    can hang the page. Download is always available."""
    st.download_button("⬇ Download .sql", sql.encode("utf-8"), file_name=fname,
                       mime="text/plain", type="primary")
    if len(sql) > 12000:
        st.caption(f"Large query ({len(sql):,} chars) — shown as plain text for performance. "
                   "Use Download, or click in the box and Cmd/Ctrl-A then Cmd/Ctrl-C to copy.")
        st.text_area("SQL", sql, height=340, label_visibility="collapsed")
    else:
        st.code(sql, language="sql")


# ---------------- reference (sidebar selection takes over the view) ----------
if ref:
    st.markdown(f"### Reference — {ref}")
    st.caption(f"Source: {REFERENCE_FILES[ref]}")
    if ref == "Internal education doc":
        st.markdown(education_markdown())
        with st.expander("Original document (raw text)"):
            doc = read_reference_doc(PKG, REFERENCE_FILES[ref])
            st.download_button("⬇ Download original", doc.encode("utf-8"),
                               file_name=REFERENCE_FILES[ref], mime="text/plain")
            st.text(doc)
    else:
        t = reference_tables(SEM)[ref]
        df = _df(t["cols"], t["rows"])
        q = st.text_input("Search").strip().lower()
        if q:
            df = df[df.apply(lambda r: q in " ".join(map(str, r)).lower(), axis=1)]
        st.caption(f"{len(df)} rows")
        st.dataframe(df, use_container_width=True, height=460)
        _download(df, REFERENCE_FILES[ref].replace(" ", "_"))
    st.stop()

caption, howto = MODES[mode]
st.markdown(f"### {_label(mode)}")
st.caption(caption)
with st.expander("How this works / where to get the data", expanded=False):
    st.markdown(howto)


# ---------------- modes ----------------
if mode == "Understand an ESDC":
    full = st.checkbox("Show full term lists (don't truncate)")
    if st.button("Explain this ESDC", type="primary"):
        d = describe_esdc(pick_esdc(ESDCS, esdc_name), SEM, EXPANSION, full=full)
        top = f"**{d['name']}**  ·  id `{d['id']}`  ·  stemming `{d['stemming']}`"
        if d["athena_url"]:
            top += f"  ·  [↗ Open in Athena]({d['athena_url']})"
        st.markdown(top)
        if d["parse_error"]:
            st.warning(f"Query had a syntax issue (auto-repaired to read): {d['parse_error']}")
        st.markdown("#### What it outputs")
        if d["output_topics"]:
            st.markdown("**Internal topic(s):** " +
                        " ".join(f'<span class="out-topic">{t}</span>' for t in d["output_topics"]),
                        unsafe_allow_html=True)
        if d["output_keys"]:
            st.markdown("**Reference term(s):** " +
                        " ".join(f'<span class="out-topic">{k}</span>' for k in d["output_keys"]),
                        unsafe_allow_html=True)
        if not d["output_topics"] and not d["output_keys"]:
            st.caption("No output topic / reference term recorded in the mapping.")
        st.markdown("#### Fields it matches on")
        fc = d["field_counts"]
        cc = st.columns(len(fc) or 1)
        for col, (f, n) in zip(cc, fc.items()):
            col.metric(f, n)
        st.markdown("#### What makes an alert match (plain English)")
        st.markdown(d["plain_md"])
        with st.expander("Visual breakdown (nested boxes)"):
            st.markdown(d["logic_html"], unsafe_allow_html=True)
        with st.expander("Outline"):
            st.markdown(d["logic_md"])
        with st.expander("Raw ESDC query"):
            st.code(d["raw_query"], language="text")

elif mode == "Alert Match Query":
    with st.expander("Pull matched alerts from Snowflake (no Looker needed)"):
        pull = st.selectbox("ESDC to pull", sorted(ESDCS), key="pull_esdc")
        c1, c2 = st.columns(2)
        m_days = c1.number_input("Lookback (days)", 1, 90, 7, key="m_days")
        m_limit = c2.number_input("Row limit", 50, 10000, 500, step=50, key="m_limit")
        if st.button("Generate extraction SQL"):
            sql = run_match_sql(pick_esdc(ESDCS, pull), days=int(m_days), limit=int(m_limit))
            _show_sql(sql, f"{pull}_matched_alerts.sql")

    up = st.file_uploader("Drop a results CSV (matched alerts)", type=["csv"])
    if up:
        text = up.getvalue().decode("utf-8-sig")
        try:
            detected = [n for n in esdcs_in_csv(text) if n in ESDCS]
        except Exception:
            detected = []
        st.markdown('<div class="esdc-tag">WHICH ESDC(S) TO EXPLAIN</div>', unsafe_allow_html=True)
        st.caption(f"Detected in this file: {', '.join(detected)}" if detected
                   else "No ESDCs auto-detected — pick from the list below.")
        sel = st.multiselect("ESDCs", sorted(ESDCS), default=detected, label_visibility="collapsed")
        only_tagged = st.checkbox("Only explain alerts actually tagged with the ESDC", True)
        if sel and st.button("Run query", type="primary"):
            try:
                objs = [pick_esdc(ESDCS, n) for n in sel]
                cols, rows = run_explain_multi(objs, SEM, text, only_tagged)
                df = _df(cols, rows)
                if df.empty:
                    st.info("No (ESDC, alert) pairs to explain. Try unchecking 'only tagged'.")
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("ESDCs", df["esdc"].nunique())
                    c2.metric("Explanations", len(df))
                    c3.metric("Matched", int((df["matched"] == "yes").sum()))
                    c4.metric("Fully verified", int((df["fully_verified"] == "yes").sum()))
                    st.dataframe(df, use_container_width=True, height=460)
                    st.caption("Per-field columns (caption_keywords, topics, …): "
                               "**values** = what this alert matched on that field · "
                               "**—** = the rule uses that field but the alert matched another way · "
                               "**blank** = the rule doesn't use that field · "
                               "**EXCLUDED by** = a NOT clause fired.")
                    _download(df, "alert_match_explained.csv")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
        elif not sel:
            st.info("Pick one or more ESDCs above to explain.")

elif mode == "Why didn't it match?":
    up = st.file_uploader("Drop a results CSV (the alert(s) in question)", type=["csv"])
    if up and st.button("Diagnose", type="primary"):
        try:
            cols, rows = run_diagnose(pick_esdc(ESDCS, esdc_name), SEM,
                                      up.getvalue().decode("utf-8-sig"))
            df = _df(cols, rows)
            cc = st.columns(len(df["verdict"].unique()) or 1)
            for col, (v, n) in zip(cc, df["verdict"].value_counts().items()):
                col.metric(v, int(n))
            st.dataframe(df, use_container_width=True, height=460)
            _download(df, f"{esdc_name}_why_no_match.csv")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")

elif mode == "Suggest new terms":
    up = st.file_uploader("Drop alerts that should be in scope (topic-filtered / FN-scan output)", type=["csv"])
    c1, c2 = st.columns(2)
    top_n = c1.number_input("Max suggestions", 10, 300, 50, step=10)
    min_alerts = c2.number_input("Min alerts per value", 1, 50, 2)
    if up and st.button("Suggest terms", type="primary"):
        try:
            cols, rows = run_suggest_terms(pick_esdc(ESDCS, esdc_name), SEM,
                                           up.getvalue().decode("utf-8-sig"),
                                           top_n=int(top_n), min_alerts=int(min_alerts))
            df = _df(cols, rows)
            if df.empty:
                st.info("No candidate additions met the threshold (the uploaded alerts may already be caught).")
            else:
                from collections import Counter
                by_field = Counter(df["field"])
                cc = st.columns(len(by_field) or 1)
                for col, (fld, n) in zip(cc, by_field.most_common()):
                    col.metric(fld, int(n))
                st.caption(f"{len(df)} candidate additions across all logic fields "
                           "(values in uncaught alerts not yet in the definition).")
                st.caption("⚠️ `usable_as_input` flags topics applied **by** an ESDc "
                           "(most `+` non-Cyber topics, e.g. `+ Floods`) — those don't exist "
                           "on the alert until after ESDc matching, so they can't be used as inputs.")
                st.dataframe(df, use_container_width=True, height=440)
                _download(df, f"{esdc_name}_suggested_additions.csv")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")

elif mode == "Find Missed Alerts (false negatives)":
    st.markdown("##### Step 1 — generate & run the query")
    c1, c2, c3 = st.columns([1, 1, 2])
    days = c1.number_input("Lookback (days)", 1, 90, 7)
    limit = c2.number_input("Row limit", 50, 10000, 1000, step=50)
    if st.button("Generate FN SQL", type="primary"):
        try:
            sql = run_fn_sql(pick_esdc(ESDCS, esdc_name), SEM, int(days), int(limit))
            st.caption("Run this in Snowflake, export the results as CSV, then upload below.")
            _show_sql(sql, f"{esdc_name}_fn_scan.sql")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")
    st.divider()
    st.markdown("##### Step 2 — report on the results")
    up = st.file_uploader("Drop the FN-scan results CSV", type=["csv"])
    if up and st.button("Run report", type="primary"):
        try:
            cols, rows = run_fn_report(pick_esdc(ESDCS, esdc_name), SEM, EXPANSION,
                                       up.getvalue().decode("utf-8-sig"))
            df = _df(cols, rows)
            cc = st.columns(len(df["verdict"].unique()) or 1)
            for col, (verdict, n) in zip(cc, df["verdict"].value_counts().items()):
                col.metric(verdict, int(n))
            st.dataframe(df, use_container_width=True, height=440)
            _download(df, f"{esdc_name}_fn_report.csv")
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")

elif mode == "Library audit":
    if st.button("Run audit", type="primary"):
        cols, rows = run_audit(ESDCS, SEM)
        df = _df(cols, rows)
        counts = df["status"].value_counts()
        cc = st.columns(len(counts))
        for col, (status, n) in zip(cc, counts.items()):
            col.metric(status.replace("_", " ").title(), int(n))
        flt = st.multiselect("Filter status", sorted(df["status"].unique()))
        view = df[df["status"].isin(flt)] if flt else df
        st.dataframe(view, use_container_width=True, height=460)
        _download(df, "esdc_fn_audit.csv")

elif mode == "Test staged changes":
    st.caption(f"Compares **production** (`{PROD_SRC}`) vs **stage** — always, regardless of the "
               "Definitions toggle. Neither file is modified.")
    up = st.file_uploader("Override stage with an uploaded expansion.json (e.g. from Athena)", type=["json"])
    staged_esdcs, staged_raw, ssrc = STAGED_ESDCS, STAGED_EXPANSION, STAGED_SRC
    if up is not None:
        try:
            t = up.getvalue().decode("utf-8-sig")
            staged_esdcs, staged_raw, ssrc = load_expansion_text(t), _jload(t), "uploaded (Athena)"
        except Exception as e:
            st.error(f"Couldn't parse uploaded staged file: {type(e).__name__}: {e}")

    if not staged_esdcs:
        st.info(f"No stage definitions available ({STAGED_SRC}). Configure Git `staged_ref`, add "
                "`esdc_qa/expansion_staged.json`, or upload one above.")
    else:
        st.caption(f"Stage source: **{ssrc}** · {len(staged_esdcs)} ESDCs · prod: {len(PROD_ESDCS)} ESDCs")
        st.markdown("##### Step 1 — definition diff + change summary")
        if st.button("Show diff & summary", type="primary"):
            summary, cols, rows = compare_definitions(PROD_ESDCS, staged_esdcs, SEM,
                                                      prod_raw=PROD_EXPANSION, staged_raw=staged_raw)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Modified", summary["changed_esdcs"])
            c2.metric("Added", summary["added_esdcs"])
            c3.metric("Removed", summary["removed_esdcs"])
            c4.metric("⚠ Need approval", summary["needs_approval"])
            if summary["needs_approval"]:
                st.warning(f"{summary['needs_approval']} change(s) need **product approval** — "
                           "net-new/deleted ESDCs, or output-topic / reference-term changes.")
            txt = staged_change_summary_text(summary, rows)
            st.text(txt)
            st.download_button("⬇ Download summary (.txt)", txt.encode("utf-8"),
                               file_name="staged_change_summary.txt", mime="text/plain")
            if rows:
                df = _df(cols, rows)
                st.dataframe(df, use_container_width=True, height=400)
                _download(df, "esdc_definition_diff.csv")
            else:
                st.success("No differences between staged and production definitions.")
        st.divider()
        st.markdown("##### Step 2 — sample impact on alerts")
        st.caption("Prod = each alert's actual `ESDC Names` tag (ground truth). "
                   "🟢 GAINED = not tagged & stage would match · 🔴 LOST = tagged by prod & stage no longer matches.")
        up2 = st.file_uploader("Drop an alert CSV", type=["csv"], key="staged_csv")
        if up2 is not None and st.button("Run sample impact", type="primary"):
            try:
                cols, rows = run_staged_impact(PROD_ESDCS, staged_esdcs, SEM,
                                               up2.getvalue().decode("utf-8-sig"))
                df = _df(cols, rows)
                c1, c2, c3 = st.columns(3)
                c1.metric("🟢 Gained", int((df["change"] == "GAINED").sum()) if not df.empty else 0)
                c2.metric("🔴 Lost", int((df["change"] == "LOST").sum()) if not df.empty else 0)
                c3.metric("ESDCs impacted", df["esdc"].nunique() if not df.empty else 0)
                if df.empty:
                    st.success("No alerts change tag status between staged and prod.")
                else:
                    st.dataframe(df, use_container_width=True, height=440)
                    _download(df, "staged_sample_impact.csv")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
        st.divider()
        st.markdown("##### Step 3 — system-wide impact (Snowflake)")
        changed = changed_esdc_names(PROD_ESDCS, staged_esdcs, SEM)
        if not changed:
            st.info("No ESDCs differ between stage and prod.")
        else:
            c1, c2, c3 = st.columns([2, 1, 1])
            pick = c1.selectbox("Changed ESDC", changed, key="staged_sql_esdc")
            sdays = c2.number_input("Lookback (days)", 1, 90, 7, key="staged_days")
            slimit = c3.number_input("Row limit", 50, 10000, 1000, step=50, key="staged_limit")
            if st.button("Generate impact SQL", type="primary"):
                sql = run_staged_impact_sql(PROD_ESDCS, staged_esdcs, SEM, pick, int(sdays), int(slimit))
                _show_sql(sql, f"{pick}_staged_impact.sql")
            st.divider()
            st.markdown("##### Step 4 — explain the gains/losses")
            st.caption("Run the Step 3 query in Snowflake, then drop the results CSV here. "
                       "You get a per-alert verdict explaining **why** the logic change gained or "
                       "lost each alert — which new term it now matches, or which new NOT term "
                       "(or removed term) dropped it — like the Alert Check breakdown.")
            pick4 = st.selectbox("ESDC these results are for", changed, key="staged_explain_esdc")
            up4 = st.file_uploader("Drop the Step 3 impact results CSV", type=["csv"],
                                   key="staged_explain_csv")
            if up4 is not None and st.button("Explain gains/losses", type="primary",
                                             key="staged_explain_btn"):
                try:
                    cols, rows = run_staged_impact_explain(
                        PROD_ESDCS, staged_esdcs, SEM, pick4, up4.getvalue().decode("utf-8-sig"))
                    df = _df(cols, rows)
                    c1, c2, c3 = st.columns(3)
                    c1.metric("🟢 Gained", int((df["impact"] == "GAINED").sum()) if not df.empty else 0)
                    c2.metric("🔴 Lost", int((df["impact"] == "LOST").sum()) if not df.empty else 0)
                    c3.metric("Alerts", len(df))
                    if df.empty:
                        st.info("No rows in the uploaded CSV.")
                    else:
                        st.dataframe(df, use_container_width=True, height=440)
                        _download(df, f"{pick4}_impact_explained.csv")
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")
