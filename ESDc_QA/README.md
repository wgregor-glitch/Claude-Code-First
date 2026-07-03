# ESDc QA

A QA / debugging tool for **ESDCs** (Enhanced Stream Definition capability) — the
boolean rules engine that enriches and routes Dataminr alerts.

It answers three questions:

1. **Why did this alert match an ESDC?** — show exactly which keyword / topic /
   threshold leaf fired, with the matched text in context.
2. **Which leaves can't we verify?** — flag conditions whose field isn't in the
   export (so you know what's confirmed vs assumed).
3. **What did the ESDC miss?** — rule-based false-negative hunt: find alerts that
   match a *relaxed* version of the rule but not the original.

## How it works

The ESDC boolean logic lives in `expansion.json` as a Dataminr **query string**,
e.g. `((originalText:"РЭБ") OR (originalText:"РЛС")) AND ((topicId:188249) ...) AND UNOT (...)`.
The tool:

1. Parses the query string into a boolean AST (`query_parser.py` → `model.py`).
2. Resolves each leaf to meaning using the reference tables (`semantics.py`):
   - `topicId:N` → topic name via **Topic Abstractions** (DE GUID → DE TOPIC)
   - `alertThreshold:"general.alertN"` → tier via **Alert Threshold Mapping**
   - `geo:[GUID]` → place via **Geo GUIDs.txt**
   - `originalText` / `captionKeyword` → keyword search over the alert text
3. Evaluates the AST against each row of a results-export CSV and explains it
   (`evaluate.py`).

### Field → export-column mapping

| ESDC field | Verifiable via column | Notes |
|---|---|---|
| `originalText` | Original Text | case-sensitive, substring/trigram |
| `captionKeyword` | Caption | word-boundary, no substring |
| `summaryKeyword` | Alert Caption Ai Summary | |
| `topicId` / `pipelineTopicId` | Internal Topics | resolved GUID→name |
| `alertThreshold` | Internal Alert Threshold | resolved code→tier |
| `geo` | Country / Location Name | resolved GUID→name |
| `modelset` | Modelset Name | |
| `source` | Source | not Source Link (that's a URL) |
| `sourceChannel` | Channel | |
| `hasCompanyTag` | Has Tagged Company | |

Column matching is case/space-insensitive and tolerates suffixes
(`Has Tagged Company (Yes / No)`), so the export can rename columns freely.
If a field's column is absent, its leaves are reported **UNVERIFIED** rather than
failing — the row is still shown as matched-with-caveats.

## Web UI

A drag-drop UI wraps every mode (explain / FN report / generate FN SQL / audit),
shows results in a table, and downloads them. Themed to match the Athena ESDC
builder (dark navy + slate + teal).

```bash
pip install -r requirements.txt
streamlit run app.py            # opens http://localhost:8501
```

No-install fallback (stdlib only, same modes):

```bash
python3 -m esdc_qa.serve        # opens http://localhost:8000
```

Modes:
- **Understand an ESDC** — plain-language logic, a **visual diagram**, what it
  outputs, and a link to the rule in **Athena** (no CSV needed)
- **Alert Match Query** — why alerts matched; pick **one or many ESDCs** at once,
  and optionally generate the Snowflake to pull the matched alerts
- **Why didn't it match?** — drop in alert(s) and see why they did *not* match a
  chosen ESDC (failed clause + suggested fix) — the localized counterpart to FN
- **Find Missed Alerts (false negatives)** — system-wide: generate the Snowflake
  query, run it, then upload the results for a per-alert verdict + remedy
  (existing definition only)
- **Suggest new terms** — mine missed alerts for candidate `captionKeyword` terms
  to *add* (broaden coverage), ranked by alerts each would catch
- **Library audit** — FN-query accuracy across all ESDCs

A sidebar **Reference** section (below the Looker links) opens the topic
abstractions, threshold mapping, geo GUIDs, and the internal education doc —
searchable and downloadable.

Pull result CSVs from Looker (Alert lookup / ESDc lookup looks, or
[dashboard 5718](https://dataminr.looker.com/dashboards/5718)) — or generate the
equivalent Snowflake in-app. Generated SQL matches the house template (same
columns, joins, and timezone date window). Each mode has a "How this works"
panel; the CLI below does the same headless.

## Usage (CLI)

```bash
# List / inspect rules
python3 -m esdc_qa.cli list --filter "Cyber"
python3 -m esdc_qa.cli show --esdc "Electronic Warfare"

# Explain WHY each alert in a results CSV matched
python3 -m esdc_qa.cli explain \
    --esdc "Electronic Warfare" \
    --results "esdc_qa/ESDc_Electronic_Warfare_Example.csv" \
    --out explained.csv

# Hunt false negatives in a broad corpus (alerts NOT pre-filtered to this ESDC)
python3 -m esdc_qa.cli find-fn \
    --esdc "Electronic Warfare" \
    --corpus path/to/all_alerts.csv \
    --out fn_candidates.csv

# Generate a Snowflake FN query from an ESDC's keyword terms (scans the whole
# alert table, not just an export). --unquoted-only emits the suspected-broken
# unquoted multi-word phrases.
python3 -m esdc_qa.cli fn-sql \
    --esdc "Hacktivists 3" --unquoted-only --days 7 \
    --out hacktivists3_fn_scan.sql

# TRUE false negatives: translate the ENTIRE rule (keyword AND topic AND modelset,
# minus UNOT exclusions) into SQL. Repairs malformed queries first.
python3 -m esdc_qa.cli fn-sql \
    --esdc "Hacktivists 3" --full --days 7 \
    --out hacktivists3_TRUE_fn_scan.sql

# Generate a true-FN query for EVERY ESDC in the library (one .sql per ESDC)
python3 -m esdc_qa.cli fn-sql --full --all --days 7 --outdir fn_queries

# Audit FN-query accuracy across all ESDCs (which are fully verifiable vs caveated)
python3 -m esdc_qa.cli audit --out esdc_fn_audit.csv
```

### Engine-faithful matching (so FN results are accurate for every ESDC)

The `--full` compiler mirrors how the ESDC engine actually evaluates each field:

| Field | SQL semantics |
|---|---|
| `captionKeyword` | space-delimited, **no substring**, case-insensitive |
| `originalText` / `translatedText` | substring, **case-sensitive** |
| `topicId` | exact element of the comma-delimited topic list |
| `modelset` | **exact** match (not substring — avoids `v2` ⊂ `v2d`) |
| `alertThreshold` | tier equality |
| `geo` | resolved place = COUNTRY (or in LOCATION_NAME) |
| `source` / `sourceChannel` | exact identifier |
| `hasCompanyTag` | company-tag present / absent |

- **Trigram rule**: keywords > 3 words can never match in-engine, so they compile
  to `FALSE` and are flagged as a rule bug (not counted as a false negative).
- **Unverifiable fields** (`CVE`/`FSX`/`lagging`/`re`/`summaryKeyword`): compiled
  as non-constraining `TRUE` and flagged in the query header.

`audit` classifies every ESDC: **278 fully verifiable**, 18 with trigram rule-bugs,
2 with unverifiable fields — so you always know which FN results are exact.

The two FN modes are a precision/recall trade:
- **keyword-only** (`fn-sql`) — broad; finds anything mentioning a phrase. Good
  for "does this term occur at all?" but noisy (e.g. generic `"hacking group"`).
- **`--full`** — exact; an alert must satisfy the *whole* boolean, so results are
  true misses attributable to the rule. This is the one to act on.

# Annotate FN-scan results with WHY each should have matched + a remedy
python3 -m esdc_qa.cli fn-report \
    --esdc "Hacktivists 3" --results <fn_scan_results>.csv \
    --out Hacktivist3_FN_annotated.csv
```

Defaults assume the reference files (`expansion.json`, the two mapping CSVs,
`Geo GUIDs.txt`) live in `esdc_qa/`. Override the expansion file with
`--expansion` and the reference dir with `--refs`.

## Getting test data (Looker)

Colleagues can pull the exact result CSVs used here — **by ESDC Name and Alert
ID** — from the Looker dashboard:

  https://dataminr.looker.com/dashboards/5718

Export a look as CSV and feed it straight to `explain` / `fn-report`. The tool
handles both the friendly headers and the raw `table.column` aliases.

## Getting the expansion (definition) files

- **Production** (`esdc_qa/expansion.json`, the baseline the tool runs against):
  download directly from **Git prod** — the source of truth for what's live.
  Refresh it periodically so the baseline stays current.
- **Staged** (for *Test staged changes*) — any of:
  1. **Stage export** — if your edits were loaded into stage via a stage upload, download from stage.
  2. **Athena export** — download the expansions file from Athena (requires Athena permissions).
  3. **Athena = prod shortcut** — if the Athena file was recently released to prod with **zero
     changes since**, it's identical to the Git-prod file and can be used as-is — only with
     Athena permissions *and* certainty that nothing changed since the release.

Staged is compared against prod fully isolated — neither file is ever modified.

## Files

```
esdc_qa/
  model.py            ESDC AST (BoolNode / LeafNode)
  query_parser.py     Dataminr query string -> AST
  semantics.py        field specs + reference-table lookups
  expansion_loader.py load + parse all ESDCs from expansion.json
  loader.py           v2: load expansion.json from a git ref
  results_csv.py      read the results-export CSV
  evaluate.py         evaluate + explain (the core)
  relax.py            relaxed-rule variants for the FN hunt
  sql_gen.py          keyword-only Snowflake FN queries
  ast_to_sql.py       compile the FULL ESDC boolean -> Snowflake (true FNs)
  fn_report.py        annotate FN alerts with why-matched + remedy
  runners.py          shared run logic for the UIs
  serve.py            zero-dependency stdlib web UI
  cli.py              command line: list / show / explain / find-fn / fn-sql / fn-report / audit
app.py                Streamlit web UI (themed)
.streamlit/config.toml  Athena-matched dark theme
requirements.txt      streamlit + pandas (UI only)
  <reference files>   expansion.json, Topic Abstractions, Alert Threshold
                      Mapping, Geo GUIDs.txt, ESDc Internal Education
```

## Status & known gaps

- **295 / 298 ESDC queries parse cleanly (99%).** The 6 World Cup
  `"...abcworldcupblockerrr"` cases are *intentional* kill-switch phrases (now
  parsed as free-text terms). The remaining **3 are genuine authoring bugs**:
  - `ESDC - Aviation - Safety`: missing operator —
    `(captionKeyword:"dies" captionKeyword:"casualty")` should be `... OR ...`
  - `ESDc - Cyber - Hacktivists 2` & `3`: **unquoted multi-word `captionKeyword`
    values** (e.g. `captionKeyword:anonymous italia`) — must be quoted.
- **Hacktivists 3 finding:** across 161 matched alerts, **0 of the 41 unquoted
  group-name phrases appear in any caption** — they aren't contributing matches.
  Use `fn-sql --unquoted-only` to scan the whole system for alerts that mention
  those groups but were missed (evidence that quoting them recovers coverage).
- **A few topicIds aren't in Topic Abstractions** (e.g. 188625, 188287) — likely
  newer/inactive topics; they show as UNVERIFIED.

## Roadmap (v2)

- **Pull ESDC logic directly from Git** (`loader.load_expansion_from_git`) so QA
  always runs against the committed source of truth.
- **Coverage-gap report**: surface alerts that *should* match an ESDC but are
  excluded by the current rules (built on the same relaxation engine).
- **Snowflake-side FN hunt**: translate the relaxed AST into a Snowflake query to
  scan alerts that were never exported, instead of a local corpus CSV.
- Stemming-aware matching when an ESDC has `isStemming: true`.
```
