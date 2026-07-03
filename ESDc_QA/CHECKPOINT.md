# ESDc QA — Checkpoint (2026-06-25)

Status: **working, tested, paused at a clean state.** Streamlit + zero-dep server
both boot clean (HTTP 200); full runner regression passes; 295/298 ESDC queries
parse (3 malformed auto-repair), 298/298 compile to FN SQL.

## What it is
A QA/debugging tool for ESDCs (Dataminr's boolean rules engine). The ESDC logic
is a Dataminr query string in `esdc_qa/expansion.json`; the tool parses it, makes
it explainable, and turns it into Snowflake. Core package is **stdlib-only**;
only the UI needs streamlit + pandas.

## Run it
```bash
cd /Users/mgrow/PROMPT_ENGINEERING/ESDc_QA
streamlit run app.py            # rich UI  → http://localhost:8501
python3 -m esdc_qa.serve        # zero-dep fallback → http://localhost:8000
python3 -m esdc_qa.cli --help   # headless CLI
```

## UI navigation
Sidebar has two grouped menus sharing ONE active selection (website-menu style,
no "none" toggle): **ANALYSIS** (the modes) and **REFERENCE** (lookup pages).
Implemented via `st.session_state.page` + on_change callbacks.

### Analysis modes
- **Understand an ESDC** — plain-English requirements (ANDs/ORs broken onto
  separate indented lines), nested-box visual (Athena-builder style: dotted-grid
  canvas, centered AND/OR/NOT headers, teal type-labelled cards), outputs, Athena
  link. Cards show full term text; "+N more" is an inline `<details>` expander,
  plus a global "Show full term lists" checkbox.
- **Alert Check — Why did it match?** (a.k.a. Alert Match Query) — why alerts matched
  (multi-ESDC); per-field-input breakdown columns; + matched-alert extraction SQL
- **Why didn't it match?** — diagnose provided alerts that didn't match + fix
- **Find Missed Alerts (false negatives)** — system scan: generate SQL → run → report (existing definition only)
- **Suggest new terms** — mine uncaught alerts for candidate additions across ALL
  logic fields (caption keyword, original text, topic, modelset, source, channel,
  geo), ranked by alerts-it-would-add (not just captions)
- **Library audit** — FN-query accuracy across all 298 ESDCs
- **Test stage changes** — compare stage vs production (always prod, isolated), 4 steps:
  - **Step 1 — definition diff** + plain-text change summary + **product-approval flags**
    (net-new/deleted ESDCs, output-topic & reference-term changes — read from the raw
    `mapping`). The diff is **polarity-aware**: a term added inside a `UNOT(...)` is
    reported as an EXCLUSION change (NARROWS), never conflated with added matching
    logic. Each row carries a clear `change` category (`Add match term · …`,
    `Add exclusion (NOT) · …`, `Output change · …`) **and** a plain-English `effect`
    sentence (BROADENS/NARROWS coverage), so the table is self-explanatory.
  - **Step 2 — sample impact** (prod = the alert's actual ESDC-Names tag; GAINED/LOST).
  - **Step 3 — system-wide impact SQL** (alerts whose match flips). Large SQL renders
    via `_show_sql()` (download + text_area when >12k chars) so the page never hangs.
  - **Step 4 — explain the gains/losses**: drop the Step 3 results CSV back in; per
    alert it re-evaluates prod vs stage and attributes the flip to the exact delta the
    alert hit — which new term it now matches, which new NOT term (or removed term)
    dropped it — with matched evidence, like the Alert Check breakdown.
  Functions: compare_definitions / staged_change_summary_text / run_staged_impact /
  run_staged_impact_sql / **run_staged_impact_explain** / changed_esdc_names.

Global **Definitions toggle (Production / Stage)** in the sidebar — every analysis
mode runs against the chosen set. **Git is the source of truth**: prod loads from a
git ref (and stage from `staged_ref`) when configured via `esdc_qa/git_config.json`
(keys: repo, path, prod_ref, staged_ref) or `ESDC_GIT_*` env vars; falls back to the
bundled `expansion.json` / `expansion_staged.json`. Read-only `git show` (loader.git_show);
the tool never writes/pushes. Prod/stage matched by **id** (not name) so renames pair up.

### Reference pages (sidebar)
Topic abstractions · Threshold mapping · Geo GUIDs (searchable + downloadable tables),
Internal education doc (curated markdown + raw original in an expander).

## Key design facts
- ESDC logic = Dataminr query string (`field:value` + AND/OR/UNOT). Parser in `query_parser.py`.
- Card/category labels distinguish keyword sources: CAPTION KEYWORD vs ORIGINAL-TEXT KEYWORD.
- Field semantics + reference tables in `semantics.py`. Threshold: alert3=Signal, alert4=Local Signal, alert5=Hyperspecific.
- FN SQL (`ast_to_sql.py`) is **engine-faithful**: captionKeyword space-delimited/no-substring;
  originalText case-sensitive substring; topicId/modelset/threshold/source exact; trigram
  (>3 words) → FALSE + flagged; unknown fields → TRUE + flagged.
- Generated SQL follows the **house template** (`sql_template.py`): same columns/joins/aliases
  + timezone date window as the real extraction query.
- AST node checks are duck-typed via `model.is_leaf()` (not isinstance) — survives Streamlit hot-reload.
- Athena link: `https://apollo-test.dmnr.io/athena/ai-streams/{powerstreamId}`.
- Looker: Alert lookup (`/x/sORb5R6nRSAwCs5wjOhkqQ`) + ESDc lookup (`/x/wIWIdul1xbhg3rlAMOWZXY`) + dashboard 5718.

## Files
```
app.py                      Streamlit UI            .streamlit/config.toml  theme
requirements.txt            streamlit + pandas
esdc_qa/
  model.py query_parser.py semantics.py expansion_loader.py loader.py results_csv.py
  evaluate.py relax.py ast_to_sql.py sql_template.py fn_report.py runners.py
  cli.py serve.py
  expansion.json + reference files (Topic Abstractions, Alert Threshold Mapping, Geo GUIDs, education doc)
README.md
```

## Findings on record
- **Stage/prod diff is subsumption-aware (text fields):** removing a longer term
  while a shorter one remains/was added is NOT a narrowing — caption keywords match
  by word-bounded containment, so any text matching `"Estate of George Harrison"`
  also matches `"George Harrison"`. The `effect` column reports such removals as
  "No coverage loss" and reserves "NARROWS coverage" for terms with no subsuming
  remaining term (`_subsumes` in runners.py; word-boundary for caption/summary,
  substring for original/translated/free text; exact-match fields always narrow).
- Hacktivists 3: 41 unquoted multi-word keywords; `IT Army of Ukraine` (4 words) violates
  the trigram limit and can never match → must be split. 30-day FN scan: of 26 raw hits,
  ~6 genuine, all already carrying the output topic via another mechanism; rest were
  substring artifacts (fixed by word-delimited matching).
- 18 ESDCs have over-trigram keyword bugs; 2 use unverifiable fields. See `audit`.
- **`ESDC - AI Security` had a trailing space in its name** in expansion.json
  (`'ESDC - AI Security '`). It broke the "only-tagged" match in Alert Match Query
  (CSV `ESDC Names` values are stripped, the ESDC's own name wasn't) → empty/error.
  Fixed by stripping names at load + stripping both sides in mapping lookups, so
  the tool is now robust to stray spaces in any ESDC name. Worth flagging to the
  ESDC owner to fix the name in the source definition.
- **Threshold label divergence:** Alert Threshold Mapping's "Threshold Type" says
  "Hyperspecific Signal" but FACT_ALERT.DE_THRESHOLD_ABBR reads "Hyperspecific".
  Exact-match was breaking the `UNOT(general.alert5)` exclusion in FN queries.
  Fixed via `threshold_abbr_variants()` (matches both labels) in ast_to_sql + evaluate.
- **Raw fact_alert export** uses `dim_esdc_grp.esdcs` column → ESDC auto-detect now
  handles both that and friendly `ESDC Names` (results_csv.esdc_names_column).
- **SQL NULL handling (fixed):** dimension columns come via LEFT JOIN, so a NULL
  (e.g. modelset on a non-auto-alert) made `CONTAINS(LOWER(NULL),…)`→NULL and
  `NOT(NULL)`→NULL, silently dropping alerts that should PASS a UNOT exclusion.
  Fixed with `COALESCE(col,'')` in every predicate builder (ast_to_sql._c).
- **Sample-impact semantics (fixed):** Step 2 now anchors prod = the alert's actual
  `ESDC Names` tag (not a re-evaluation), so an untagged alert can't be reported
  "LOST". GAINED = untagged + staged matches + prod logic didn't.
- **Alert Match Query output** now has a per-ESDC-input column after `why`
  (caption_keywords / topics / modelsets / threshold / source / channel / geo /
  company_tag / summary): values = matched; "—" = field in rule but matched
  another way; blank = field not used; "EXCLUDED by" = a NOT fired.

## Open items / next steps
1. **Confirm field semantics** for `re`, `FSX`, `lagging`, `CVE` (rare; currently
   treated as non-constraining + flagged).
2. **Suggest new terms** is frequency-based (some generic noise) — could add TF-IDF
   vs a background corpus or an LLM ranking pass to sharpen.
3. **v2**: load expansion.json directly from Git (`loader.load_expansion_from_git` stub).
4. Decide whether to ship the zero-dep `serve.py` UI alongside Streamlit, or Streamlit only.
5. Confirm the FN/extraction Snowflake template matches house style exactly (joins,
   date window) against a real run.
6. `<details>` "+N more" expanders rely on browser HTML support; global "Show full
   term lists" checkbox is the fallback.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
