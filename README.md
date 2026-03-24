# SAR Memory Assistant

**Phase 04 of the Claude API Learning Series**

A CLI tool that accumulates Structure-Activity Relationship (SAR) knowledge
across multiple sessions for a given drug target.  Feed it Phase 03 extraction
outputs today; ask it questions tomorrow and it remembers everything it has
ingested so far.

```
python main.py --target KRAS \
    --ingest data/compounds.json data/sar_trends.json \
    --source "JMedChem Example Paper"

python main.py --target KRAS \
    --query "What structural features improve potency in the switch-II pocket?"
```

---

## New API Concepts (Phase 04)

| Concept | What you learn |
|---|---|
| **Memory tool (`memory_20250818`)** | Client-side tool: Claude emits `tool_use` blocks with file commands; your app executes them locally and returns `tool_result` blocks |
| **Prompt caching (`cache_control`)** | Mark large stable context blocks so repeated reads cost ~10% of normal rate |
| **Cross-session persistence** | Memory files live on disk; every CLI invocation inherits state from the last run |

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your API key
#    Option A — shell environment variable:
export ANTHROPIC_API_KEY=sk-ant-...
#    Option B — create a .env file in the project root:
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

---

## Usage

### Ingest mode

Read Phase 03 outputs and write them into the memory store for a target:

```bash
# Ingest both files in one call
python main.py --target KRAS \
    --ingest data/compounds.json data/sar_trends.json \
    --source "Example Paper"

# With verbose tool-call trace
python main.py --target KRAS \
    --ingest data/compounds.json data/sar_trends.json \
    --verbose

# Custom memory directory
python main.py --target KRAS \
    --ingest data/compounds.json \
    --memory-dir /path/to/my/memory
```

### Query mode

Synthesise an answer from the accumulated memory:

```bash
# Streaming answer (default)
python main.py --target KRAS \
    --query "What features improve potency?"

# With cache hit statistics
python main.py --target KRAS \
    --query "Which compounds have oral bioavailability data?" \
    --verbose

# Non-streaming + write to file
python main.py --target KRAS \
    --query "Summarise all SAR findings" \
    --no-stream \
    --output reports/KRAS/summary.md
```

### Ingest then query in one call

```bash
python main.py --target KRAS \
    --ingest data/compounds.json data/sar_trends.json \
    --source "Example Paper" \
    --query "What is the most potent compound in the memory?"
```

### All flags

| Flag | Default | Description |
|---|---|---|
| `--target` | *(required)* | Drug target name — memory namespace |
| `--ingest` | — | One or more JSON files to ingest |
| `--source` | derived from filenames | Source provenance label for MEMORY.md |
| `--query` | — | Question to synthesise against memory |
| `--no-stream` | False | Disable streaming for query output |
| `--output` | — | Write query answer to this file |
| `--model-ingest` | `claude-sonnet-4-6` | Model for ingest mode |
| `--model-query` | `claude-sonnet-4-6` | Model for query mode |
| `--memory-dir` | `memory` | Base directory for memory files |
| `--verbose` | False | Tool call trace + cache stats |

---

## How it works

### Memory file layout

```
memory/
  KRAS/
    MEMORY.md       ← index: sources ingested, record counts, last updated
    compounds.md    ← one ## section per compound
    sar_trends.md   ← one ## section per SAR finding
    hypotheses.md   ← inferred patterns, open questions, [INCONSISTENCY] notes
  CETP/
    MEMORY.md
    ...
```

`memory/` is gitignored — it lives only on your local machine.

### Ingest: the Memory tool loop

```
your app                         Claude
---------                        ------
messages.create(tools=[memory])
                        →        thinks about what to write
                        ←        tool_use: {command: "view", path: "compounds.md"}
execute_memory_command()
  → reads file from disk
tool_result: "<file content>"
                        →
                        ←        tool_use: {command: "str_replace", ...}
execute_memory_command()
  → writes to disk
tool_result: "str_replace succeeded"
                        →
                        ←        stop_reason: "end_turn"
done
```

Claude decides *what* to write and *where* to put it.  Your app executes the
actual file I/O.  This is the foundational pattern for all client-side tools.

### Query: prompt caching

On every query the memory files are concatenated into a single block and marked
with `cache_control: {type: "ephemeral"}`:

```
[system]  "You are a SAR analyst..."           ~50 tokens   — not cached
[user]
  block 1: full memory body (all 4 files)      ~2000+ tokens — CACHED ←
  block 2: "Question: ..."                     ~20 tokens   — not cached
```

- **First query:** full input cost + 25% write premium on the cached block.
- **Subsequent queries (within 5 min):** cached block costs 10% of normal rate.
- As memory grows, the savings per query increase proportionally.

Use `--verbose` to see `cache_read_input_tokens` in the output.

---

## Input format (Phase 03 outputs)

### compounds.json

List of objects; fields used by the ingestor:

| Field | Required | Description |
|---|---|---|
| `compound_name` | yes | Name or code (e.g. "divarasib", "compound_B") |
| `smiles` | no | SMILES string |
| `activity_value` | no | Numeric activity (e.g. "12") |
| `activity_unit` | no | Unit (e.g. "nM") |
| `activity_type` | no | IC50, Ki, EC50 … |
| `assay_type` | no | biochemical, cellular … |
| `assay_target` | no | Target name string |
| `assay_species` | no | human, rat, mouse … |
| `source_quote` | no | Verbatim quote from the paper |
| `page_reference` | no | Page number string |

### sar_trends.json

| Field | Required | Description |
|---|---|---|
| `finding` | yes | One-sentence description of the finding |
| `structural_feature` | yes | The structural element discussed |
| `direction` | yes | "improve" or "decrease" |
| `magnitude` | no | e.g. "3-fold" |
| `evidence_quote` | no | Verbatim quote from the paper |
| `page_reference` | no | Page number string |

---

## Limitations

- **Memory accuracy:** the tool is only as accurate as the JSON files you feed
  it.  It does not verify chemistry, check SMILES validity, or cross-reference
  external databases.
- **No hallucination guard on ingest:** the ingest system prompt instructs
  Claude to omit fields that are absent from input, but you should review the
  generated markdown files to confirm no properties were invented.
- **Single-user, single-machine:** memory files are plain markdown on your local
  disk.  There is no locking, versioning, or multi-user access control.
- **Prompt cache TTL:** the default cache window is 5 minutes.  If you run
  queries more than 5 minutes apart the cache will miss on the first query of
  each session (and re-write the cache).
- **Context window:** very large memory corpora (many papers ingested) may
  eventually approach the model's context window limit.  Phase 05 will address
  this with multi-agent routing.

---

## Running the sample fixtures

```bash
# Ingest the synthetic sample data included in data/
python main.py --target KRAS \
    --ingest data/compounds.json data/sar_trends.json \
    --source "Synthetic Example" \
    --verbose

# Inspect the generated memory files
cat memory/KRAS/compounds.md
cat memory/KRAS/sar_trends.md

# Query
python main.py --target KRAS \
    --query "Which compound has the best biochemical potency?" \
    --verbose
```

---

## Project structure

```
sar-memory-assistant/
├── main.py                  CLI entry point and mode dispatch
├── requirements.txt
├── README.md
├── .gitignore
├── data/
│   ├── compounds.json       Synthetic sample compounds (3 records)
│   └── sar_trends.json      Synthetic sample SAR trends (3 records)
└── src/
    ├── __init__.py
    ├── models.py            Pydantic schemas: IngestInput, MemoryFileState, QueryRequest
    ├── memory_store.py      File I/O + execute_memory_command (security sandbox)
    ├── ingestor.py          Ingest mode: manual tool loop + optional tool_runner
    ├── querier.py           Query mode: prompt caching + streaming synthesis
    └── deduplicator.py      Dedup rules injected into the ingest system prompt
```
