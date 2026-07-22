# Source Classification Agent

Classifies social media entities (e.g. Facebook pages/accounts) into
official source channel types — gov, emergency, news, localnews, ngo,
university, blog, majorblog, reporter, unknown — plus inferred
city/state/country and a confidence score, using the Claude Message
Batches API (`claude-haiku-4-5-20251001`, ~50% cheaper than sync calls).

The pipeline maintains one running master file, `data/entities_classified.csv`,
with **one row per unique entity ID**. It starts out empty — the first run
creates it. Every time you have a new export (a new month's pull, a new
country, a new source list), the script dedupes by ID against the master
and only pays to classify entities that aren't already there — it never
re-classifies something it's already seen.

## Setup

1. Python 3.11+ with the `anthropic` package:
   ```bash
   pip install -r scripts/requirements.txt
   ```
2. An Anthropic API key, either:
   - exported as `ANTHROPIC_API_KEY` in your shell, or
   - entered at the prompt when the script asks (input is hidden)

## Running it

Your new raw export CSV must have these columns: `ENTITY_NAME`, `ID`, `LINK`, `VERIFIED`.

```bash
cd scripts
python3 classify_new_drop.py /path/to/new_export.csv [another_export.csv ...]
```

This will:
1. Load `data/entities_classified.csv` (creates it fresh if it doesn't exist yet)
2. Dedupe the new file(s) by `ID` against everything already in the master
3. Submit only the net-new IDs to the Batches API and poll until done
4. Merge the classified results into the master in place (existing rows are untouched)

Example output (first run, starting from an empty master):
```
Current master: 0 unique entities
Net new entities to classify: 37
Using ANTHROPIC_API_KEY from environment.
Submitting 1 batch(es)...
  Submitted batch msgbatch_... (37 requests)
  Waiting for batch msgbatch_..... done.
Merged 37 new entities into ../data/entities_classified.csv.
Total unique entities now: 37
```

## Output schema (`data/entities_classified.csv`)

| column | meaning |
|---|---|
| `link` | Entity URL |
| `id` | Platform entity ID (dedup key) |
| `display_name` / `entity_name` | Name as provided in the source export |
| `external_source_description` | 1-2 sentence factual description |
| `verified` | Platform verification flag from the source export |
| `channels` | Classification: `gov`, `emergency`, `news`, `localnews`, `ngo`, `university`, `blog`, `majorblog`, `reporter`, or `unknown` |
| `city` / `state_province` / `country` | Inferred location, blank if not determinable |
| `confidence` | `high`, `medium`, or `low` — the model is instructed to fall back to `unknown` channel whenever confidence is medium/low |

## Classification rules (summary)

Full rules and examples live in the `SYSTEM_PROMPT` inside
`scripts/classify_entities_shared.py`. Key points worth knowing before
reviewing output:

- **`news` is reserved for ~top-50 globally recognized outlets** (BBC, CNN,
  Reuters, AP, AFP, etc.). Everything else national/regional/local defaults
  to `localnews` — when in doubt, the model is told to prefer `localnews`.
- **`emergency`** is only for verified official police/fire/EMS/OEM agencies
  with a clear jurisdiction in the name. Community/enthusiast/scanner pages
  covering the same beat are classified `blog` instead.
- **`gov`** is non-emergency government only (city hall, legislature, public
  health, elected officials).
- Low/medium confidence classifications are deliberately downgraded to
  `unknown` rather than guessed.

## Files

- `scripts/classify_entities_shared.py` — shared module: system prompt, Batches API
  submit/poll helpers, CSV I/O. Not run directly.
- `scripts/classify_new_drop.py` — the script you actually run; dedupe + classify + merge.
- `data/entities_classified.csv` — the running master; starts empty, created on first run.
- `.claude/agents/source-classification-agent.md` — a Claude Code subagent so you can just
  ask Claude ("classify this new export and merge it into the master") instead of running
  the script by hand. Drop the `.claude/` folder into the root of whatever project you're
  working in and Claude Code will pick it up automatically.
