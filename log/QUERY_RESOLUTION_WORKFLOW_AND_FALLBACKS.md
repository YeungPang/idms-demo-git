# Query Resolution and Answer Workflow (Models + Fallbacks)

This document explains how IDMS-Demo resolves and answers a query end to end, including LLM models used and fallback behavior.

## 1) Entry Points

Primary API endpoint for query answering:
- `POST /api/query` -> `interaction.Tools.autonomous_query(...)`

Core resolver/lookup engine:
- `query_engine.QueryEngine.answer(...)`

## 2) Model Mapping (Current Runtime)

From `idms_config.py` model aliases:
- `EXTRACT_MODEL` = `OPENROUTER_MODEL` (default `google/gemini-2.5-flash`)
- `CHAT_MODEL` = `OPENROUTER_COMPLEX_MODEL` (default `google/gemini-2.5-pro`)
- `QDRANT_EMBEDDING_MODEL` = `OPENROUTER_EMBEDDING_MODEL` (default `openai/text-embedding-3-large`)

Where each is used in query flow:
- `QDRANT_EMBEDDING_MODEL`:
  - query embedding for vector retrieval in `QueryEngine.qdrant_candidates(...)`
- `EXTRACT_MODEL`:
  - LLM structured query plan (`query_structured_plan`)
  - LLM intent extraction fallback (`query_intent_extract`)
  - semantic attribute fallback (`semantic_llm_attribute_fallback`)
  - contextual resolution fallback (`contextual_llm_resolution_fallback`)
- `CHAT_MODEL`:
  - autonomous planner step action selection (`interaction_planner_step`)
  - final natural-language answer generation (`interaction_answer_generation`)

## 3) OpenRouter Fallback Wrapper Behavior

All LLM calls above go through `generate_content_with_openrouter_fallback(...)`.

Behavior:
1. Uses the caller-requested model directly (no hidden model override).
2. Calls OpenRouter chat-completions endpoint.
3. Raises runtime error if OpenRouter is disabled or API key is missing.
4. Raises runtime error on HTTP/JSON/content failures.

Important note:
- In this codebase, this helper is effectively OpenRouter-only at runtime; it does not continue to a second provider inside this wrapper.

## 4) End-to-End Workflow (Autonomous Query Path)

### Step 1: Request intake and cache
- `autonomous_query(question, max_steps)` validates input.
- Checks 1-hour cache and returns cached answer when available.

### Step 2: Parse query intent/targets
- Calls `QueryEngine.parse_query(question)`.
- Parsing starts with deterministic gates (relationship gates, file-info gates, lexical gates, deterministic pattern matching).
- If enabled (`ENABLE_LLM_INTENT_PARSER`) and not identifier-heavy, parser may add LLM structured-plan candidate via `EXTRACT_MODEL`.
- Arbitration chooses the best `ParsedQuery` for execution.

### Step 3: Policy gates
- Applies query policy + style policy (`interaction_query_policy`, `query_style_policy`).
- If denied, returns `policy_blocked` without generation.

### Step 4: Direct factual fast path
- For attribute/semantic/relationship intents, tries `QueryEngine.answer(question)` first.
- If it returns a non-`not_found` source, autonomous loop short-circuits and returns that grounded answer.

### Step 5: Planner loop (0..max_steps-1)
- `_planner_step(...)` chooses one action:
  - `resolve_scope`
  - `search_discovery`
  - `lookup_attribute`
  - `search_criteria`
  - `finalize`
- Routing order is:
  1. SOLF workflow routing hints (if present)
  2. style/media heuristics
  3. LLM planner call using `CHAT_MODEL`
  4. deterministic safety fallback if planner output action is invalid

### Step 6: Tool execution per action
- `resolve_scope`: Qdrant/Discovery candidate resolution and scoped IDs.
- `search_discovery`: explicit Discovery/media search.
- `lookup_attribute`: SQL-backed attribute lookup via QueryEngine.
- `search_criteria`: SQL criteria retrieval.
- `finalize`: stops loop.

### Step 7: Answer policy + final synthesis
- Applies `interaction_answer_policy`.
- If denied, returns `policy_blocked`.
- Else generates final answer text with `CHAT_MODEL` using grounded state payload.
- Returns answer with `answer_sources`, grounding, trace, and model name.

## 5) QueryEngine.answer(...) Workflow (Direct Resolver)

### Phase A: Parse + candidate retrieval
1. Parse and normalize query.
2. Retrieve Qdrant candidates using embedding + vector search.
3. Retrieve Discovery candidates.

Qdrant retrieval mode:
- Hybrid sparse+dense if supported and heuristic matches.
- Dense-only fallback otherwise.
- Collection-name fallback cascade if configured collection is missing.
- Keyword payload fallback if vector search yields no points.

### Phase B: Deterministic SQL-first answering
4. Criteria intent -> SQL criteria search path.
5. Attribute intent -> direct SQL attribute lookup path.
6. Multi-attribute composite answer if multiple resolved attributes.
7. Strict relation safeguard: if relation-driven query has no relation match, return `not_found` (do not drift).

### Phase C: Retrieval-guided and semantic fallbacks
8. `qdrant_sql_fallback`: try SQL lookup using IDs from vector candidates.
9. `discovery_fallback`: use discovery struct data for attribute answer.
10. `semantic_llm_fallback` (if enabled): LLM extracts value from grounded context using `EXTRACT_MODEL`.
11. `sql_rescue_fallback`: deterministic attribute/entity candidate rescue loop.

### Phase D: Final LLM fallbacks
12. For semantic intent: `contextual_llm_resolution_fallback` using `EXTRACT_MODEL`.
13. Last-resort intent extraction: `_llm_extract_intent` using `EXTRACT_MODEL`, then retry SQL lookup.
14. Final contextual fallback attempt.
15. If all fail -> `not_found` with fallback diagnostics.

## 6) Fallback Scenarios Matrix

| Scenario | Trigger | Fallback Action | Result Source |
|---|---|---|---|
| Qdrant hybrid unavailable | sparse profile missing / heuristic false / errors | use dense vector query | `qdrant` (dense mode) |
| Qdrant vector query empty | no points from primary collection | refresh collection name and retry | `qdrant` refreshed collection |
| Qdrant still empty | vector retrieval failed | payload keyword lexical fallback | `keyword_payload_fallback` |
| Exact SQL miss with vector IDs present | direct lookup failed | SQL fallback from candidate IDs | `qdrant_sql_fallback` |
| Attribute miss but Discovery has struct data | direct and qdrant-sql miss | discovery attribute extraction | `discovery_fallback` |
| Deterministic miss for attribute query | SQL/retrieval tiers miss and semantic fallback enabled | LLM semantic attribute extraction (`EXTRACT_MODEL`) | `semantic_llm_fallback` |
| Attribute alias drift / parser miss | low-confidence parse | SQL rescue over candidate attrs/entities | `sql_rescue_fallback` |
| Semantic query unresolved | semantic summary insufficient | contextual LLM grounding resolve (`EXTRACT_MODEL`) | `contextual_llm_resolution_fallback` |
| Planner action invalid in autonomous loop | malformed planner JSON or unknown action | deterministic action correction | planner safety fallback |
| Policy denies query/answer | policy mode `deny` | skip generation and return blocked msg | `policy_blocked` |
| Everything fails | no deterministic or LLM-grounded match | return explicit not-found + diagnostics | `not_found` |

## 7) Operational Notes

- Discovery index is disabled by default in demo config (`ENABLE_DISCOVERY_INDEX = False`), so Discovery fallbacks may be inactive unless enabled externally.
- Identifier-style queries intentionally avoid some LLM fallback paths to reduce hallucination and misrouting.
- The final API response exposes traces/grounding/source fields that make fallback path auditing possible.
