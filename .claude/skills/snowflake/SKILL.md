---
name: snowflake
description: >
  Query Dataminr's alert pipeline and AI streams from Snowflake using the Snowflake Cortex MCP.
  Use whenever the user asks to pull data, troubleshoot, or ask questions about a modelset config
  or an AI stream — e.g. "shadow data", "prod data", "HNMN / GovSources / Commodities", config IDs
  (dgm_int_tw_*), "endorsed / review / discarded", "why is [config] routing to X", "what got
  discarded", "pipeline errors", "modelset dormancy / anomaly", "what's in stream N", "what does
  stream N exclude", "which streams contain handle @x". MCP-first: prefer the Cortex agents; drop to
  direct SQL only for stream composition (no agent exists for that yet).
---

# Snowflake Skill (MCP edition)

You query Snowflake through the **Snowflake Cortex MCP**. There is no local Python script — use the
MCP tools only. Two ways to get data:

1. **Cortex agents** (natural language → SQL, run for you). Use for anything about **modelsets /
   alerts / pipeline**. Each agent owns one domain; pick the right one below.
2. **Direct SQL** via `mcp__snowflake__execute_sql`. Use for **AI stream composition**, which has no
   agent yet, and for lifting an agent's SQL to pull a larger result set.

All agents live in `PROD_AN.SNOWFLAKE_INTEL`. Access needs the `BETA_INTELLIGENCE_USER` role for the
newer agents — if a call is denied, that's the likely cause.

---

## The agents — pick by domain

| If the question is about… | Use agent |
|---|---|
| Per-alert routing (ENDORSE/REVIEW/DISCARD), captions, the caption/geo model inputs & outputs, source content, prod-vs-shadow | **`ALERT_PIPELINE_ROUTING_INTELLIGENCE_AGENT`** |
| Pipeline health — execution errors, retries, service-call failures, event volumes, throughput | **`GEN_AA_INTELLIGENCE_AGENT`** |
| Modelset performance — alert-volume trends, dormancy, z-score anomalies, active/enabled status | **`DGM_MODELSET_INTELLIGENCE_AGENT`** |
| Message ingest → stream match → alert delivery, stream match rates | **`LABELED_MESSAGE_INTELLIGENCE_AGENT`** |
| **What an AI stream is built from / includes / excludes** | *(no agent yet — direct SQL, see below)* |

### How to call an agent

```
mcp__snowflake__cortex_agent_run(
  prompt      = "<the user's question, in plain English>",
  database    = "PROD_AN",
  schema      = "SNOWFLAKE_INTEL",
  agent_name  = "ALERT_PIPELINE_ROUTING_INTELLIGENCE_AGENT")
```

Dedicated per-agent tools (e.g. `mcp__snowflake__agent_alert_pipeline_routing_intelligence_agent`)
also work and take just a `prompt`. Use `mcp__snowflake__list_cortex_agents` to see what's deployed.

The agent returns a conversational answer **plus the SQL it ran** (in `tool_uses[].input.sql`) and a
result table. Always tell the user which config/time window you used.

---

## Modelset workflows

Route the question to the agent above, ask in plain English, and report the answer. Examples:

- "How is `dgm_int_tw_govsources` routing in prod vs shadow over the last 2 hours?" → routing agent
- "Show me the caption input, translation, and final caption for HNMN shadow alerts" → routing agent
- "Which configs are throwing execution errors right now, and on which step?" → operations agent
- "Is `dgm_int_tw_hnmn` dormant or anomalous today?" → modelset performance agent

### Pulling a larger dataset (no Python script)

Cortex agents cap how many rows they return. For a bulk pull:
1. Ask the agent for the data and to **include the SQL**.
2. Take the executed SQL from `tool_uses[].input.sql` (last successful `system_execute_sql`).
3. Run that SQL via `mcp__snowflake__execute_sql` to get the full result set. The agent's row-level
   SQL already includes the dedup `QUALIFY`, so it is safe to run as-is.

### Key modelset reference facts

- **Configs:** `dgm_int_tw_hnmn` (HNMN), `dgm_int_tw_govsources` (GovSources), `dgm_int_tw_commodities`
  (Commodities). "Geo" is NOT a config — it's a prediction step inside configs.
- **ROUTE:** `ENDORSE` (alerted), `REVIEW` (Apollo revision), `DISCARD` (dropped).
- **Prod vs shadow:** `SEND_ALERTS = TRUE` is prod, `FALSE` is shadow; real traffic is `RUN_MODE = 'live'`.
- **Config identity:** filter by config *name* on `MODELSET_GROUP` (version-agnostic family). `EVENT_SOURCE`
  is the versioned deployment (often `<group>_model_<N>`); exact-matching a base name on `EVENT_SOURCE`
  can return zero rows. The agents know this, but keep it in mind if you drop to SQL.

---

## Stream workflows (direct SQL — no agent yet)

AI stream **composition** lives in three tables and has no Cortex agent, so query it with
`mcp__snowflake__execute_sql`:

- `PROD_AN.ODS.AI_STREAM` — one row per stream. `ID`, `NAME`, `IS_ACTIVE`, `IS_STEMMING`, `TAGS`
  (VARIANT — includes `modelset:<config>` and `AUTOALERT` tags), and `AI_STREAM` (VARIANT: the
  recursive component tree under `AI_STREAM:root`).
- `PROD_AN.ODS.AI_STREAM_COMPONENT` — one row per component. `ID`, `NAME`, `COMPONENT_TYPE`
  (`SIMPLE_TERM_LIST` = plain terms/handles; `MATCHABLE_QUERY_LIST` = co-occurrence query terms).
- `PROD_AN.ODS.AI_STREAM_COMPONENT_CONTENT` — one row per term. `COMPONENT_ID`, `CONTENT`.

**Structure:** `AI_STREAM:root` is a tree of `Operator` nodes (`operation` = `AND` / `OR` / `NOT`)
over `ComponentRef`s (point to components by id), `Raw` nodes (inline predicates like `lang:en`,
`((threatLLMConfidence:greaterThanOrEqualTo:0.9))`), and `PowerstreamRef`s. Terms under a `NOT`
operator are **exclusions**.

### Gotchas (these will bite you)

- **Flatten `AI_STREAM:root` recursively**, not `root:children` — the tree shape varies by stream.
- **A LATERAL FLATTEN can't sit on the left of a JOIN** — do the flatten in a CTE first, then join.
- **`IS_STEMMING`** on the stream changes match semantics (stemmed vs exact).
- **Surface composition only. Do NOT try to evaluate whether a message matches** — that means
  re-implementing the matching engine. "Did message X match stream Y" belongs to the match-event
  data (labeled-message layer), not these tables.

### Pattern — what's in a stream (components + terms)

```sql
WITH refs AS (
  SELECT s.ID AS stream_id, s.NAME AS stream_name, c.value:id::NUMBER AS component_id
  FROM PROD_AN.ODS.AI_STREAM s,
    LATERAL FLATTEN(input => s.AI_STREAM:root, recursive => true) c
  WHERE c.value:_type_::TEXT = 'ComponentRef' AND s.ID = <STREAM_ID>
)
SELECT sc.COMPONENT_TYPE, sc.NAME AS component_name, cc.CONTENT
FROM refs r
JOIN PROD_AN.ODS.AI_STREAM_COMPONENT sc ON sc.ID = r.component_id
LEFT JOIN PROD_AN.ODS.AI_STREAM_COMPONENT_CONTENT cc ON cc.COMPONENT_ID = r.component_id
ORDER BY sc.COMPONENT_TYPE, component_name, cc.CONTENT;
```

### Pattern — a stream's exclusions (what it filters out)

```sql
WITH not_ops AS (
  SELECT c.value AS not_node
  FROM PROD_AN.ODS.AI_STREAM s,
    LATERAL FLATTEN(input => s.AI_STREAM:root, recursive => true) c
  WHERE s.ID = <STREAM_ID>
    AND c.value:_type_::TEXT = 'Operator'
    AND c.value:operation::TEXT = 'NOT'
)
SELECT ch.value:_type_::TEXT AS child_type,
       ch.value:content::TEXT AS raw_predicate,
       ch.value:id::NUMBER    AS component_id,
       ch.value:name::TEXT    AS component_name
FROM not_ops n, LATERAL FLATTEN(input => n.not_node, recursive => true) ch
WHERE ch.value:_type_::TEXT IN ('Raw', 'ComponentRef', 'PowerstreamRef');
```
Then pull the excluded terms with the component ids: `SELECT CONTENT FROM
PROD_AN.ODS.AI_STREAM_COMPONENT_CONTENT WHERE COMPONENT_ID IN (<ids>)`.

### Pattern — link a stream to its modelset config

```sql
SELECT ID, NAME, TAGS
FROM PROD_AN.ODS.AI_STREAM
WHERE IS_ACTIVE = TRUE
  AND CAST(TAGS AS VARCHAR) ILIKE '%modelset:<CONFIG>%';
```

---

## After running anything

Report: which agent or table you used, the config/stream and time window, the row count, and a few
sample rows so the user can sanity-check. If a stream question is really a "did it match" question,
say so and point to the match-event data — don't answer it from composition alone.
