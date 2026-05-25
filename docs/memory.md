# IndusBuilt local memory (context manager)

How persistent and prompt memory works: implementation in [`indusbuilt/context_manager.py`](../indusbuilt/context_manager.py), wired from [`indusbuilt/agent.py`](../indusbuilt/agent.py).

## Sandbox and storage layout

Memory is **scoped to the project sandbox**: the directory you run `indusbuilt` from (`Path.cwd()` in [`indusbuilt/cli.py`](../indusbuilt/cli.py)).

Everything lives under:

```text
<project>/.indusbuilt/memory/
  summaries/current_session.md   # rolling session summary
  knowledge/<type>/              # markdown knowledge files
  offloaded/                     # huge tool outputs saved as .txt
  memory.sqlite3                 # SQLite (+ FTS5 when supported)
```

- **Knowledge** is normal Markdown on disk; the DB is an **index** for search and retrieval previews.

## What gets sent to the model on each turn

The agent does **not** send the full chat forever. For each LiteLLM call it builds:

1. **System**: main agent instructions (`build_system_prompt`).
2. **Second system message**: a compact **“Context Manager State”** block built by `ContextManager.build_messages`:
   - **Active context** – small in-process dict (`goal`, `current_file`, `active_bug`, `next_step`), trimmed in size.
   - **Session summary** – text read from `summaries/current_session.md`, trimmed.
   - **Retrieved memory** – up to **4** hits from `search_memory(latest_user_message)`; each hit is a short line (type, topic, summary, path), not the full file.
3. **Recent conversation only** – last **20** messages from the in-memory `conversation` list (user / assistant / tool).

Char caps on the composed block (class attributes on `ContextManager`):

- `max_active_context_chars` = 1200  
- `max_summary_chars` = 1800  
- `max_retrieval_chars` = 1800  
- `max_recent_messages` = 20  

## Active context (RAM, not persisted alone)

On each user message, `register_user_turn` updates `active_context` from the latest user text (e.g. inferred filename patterns).

After each tool call, `register_tool_result` updates `active_context` from the tool result (errors, paths, etc.).

This state is **included in the prompt** but is not a separate JSON file unless you copy it elsewhere.

## Session summary (disk)

`summarize_session(conversation, reason=...)` writes `summaries/current_session.md` using:

- Current active-context fields  
- Recent user and assistant messages (short trims from the conversation list)

Triggered by:

- **`/memory summarize`** (manual)  
- **Auto**: `maybe_auto_summarize` when `len(conversation) >= 36` (after assistant reply or tool messages)

Summarization here is **deterministic** (no extra LLM call for compression).

## Long-term knowledge (disk + index)

### Saving

`save_memory(memory_type, topic, summary, content=None)`:

- Slugifies `memory_type` and `topic`.
- Writes a `.md` file under `knowledge/<memory_type>/` with a timestamped filename.
- Upserts a row in `memory_entries` and, when possible, the **FTS5** virtual table `memory_entries_fts`.

### Searching

`search_memory(query, limit)`:

- Prefer FTS `MATCH` + `bm25` ordering when FTS is available.
- On query errors or missing FTS, falls back to SQL `LIKE` on title/summary/body/topic.

### Rebuild

`/memory rebuild` calls `rebuild_index()`: clears DB tables, re-indexes all `knowledge/**/*.md`.

## Code retrieval (separate from memory KB)

`retrieve_code(query, limit)` scans the sandbox for common extensions (`*.py`, `*.md`, etc.), skips noisy dirs (e.g. `.git`, `node_modules`, `venv`), skips `.indusbuilt/memory`, scores by token frequency, returns short snippets. This is for **code context**, not for stored wiki memory.

## Large tool outputs

`maybe_offload_tool_result`:

- If `json.dumps(result)` length ≥ **4000** characters, replaces the tool payload seen by the model with a small JSON object: path to saved file under `offloaded/`, preview (~600 chars), byte count.

## Agent tools exposed to the LLM

From `agent.py`, when a `ContextManager` exists:

| Tool | Role |
|------|------|
| `save_memory` | Write markdown knowledge + index |
| `search_memory` | Query index, summaries only in response |
| `summarize_session` | Refresh `current_session.md` from conversation |
| `retrieve_code` | Snippet search over project files |
| `offload_large_output` | Explicit save huge text + preview |

## CLI slash commands

| Command | Effect |
|---------|--------|
| `/memory` or `/memory status` | JSON status (paths, counts, active context) |
| `/memory search <query>` | JSON search results |
| `/memory summarize` | Rewrite session summary from current `conversation` |
| `/memory rebuild` | Rebuild SQLite index from markdown |

The `/` palette also has **Memory status** (prints same JSON as status).

## Relationship to Skills

- **Skills** (`SkillRegistry`, `SKILL.md`): task instructions injected via system prompt and `activate_skill`.
- **Memory**: project facts, bugs, decisions stored as markdown + search index. They are intentionally separate; both can appear in the prompt, but skills are not the same as the knowledge base.

## Privacy / portability

All of the above stays **on disk under your project** (plus your normal user config for API keys). Commit `.indusbuilt/` only if you want team-shared memory; otherwise add it to `.gitignore` if you prefer local-only storage.
