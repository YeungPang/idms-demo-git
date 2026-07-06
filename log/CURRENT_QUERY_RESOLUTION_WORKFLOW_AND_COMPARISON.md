# Current Query Resolution Workflow and Comparison to the Older Pipeline

This document describes the current query-resolution pipeline implemented in `interaction.py` and `query_engine.py`, then compares it with the older pipeline described in `QUERY_RESOLUTION_WORKFLOW_AND_FALLBACKS.md`.

The goal is to explain why many queries still return correct answers, but now follow different routes, produce different source labels, and often generate more deterministic outputs.

## 1. Scope and current code anchors

Primary current control path:
- `interaction.IDMSInteractionTools.autonomous_query(...)`
- `interaction.IDMSInteractionTools._planner_step(...)`
- `interaction.IDMSInteractionTools._classify_query_style(...)`
- `query_engine.QueryEngine.parse_query(...)`
- `query_engine.QueryEngine.answer(...)`
- `query_engine.QueryEngine.lookup_attribute(...)`
- `query_engine.QueryEngine.search_by_criteria(...)`
- `query_engine.QueryEngine.resolve_search_scope(...)`

This is no longer just a simple planner + retrieval + LLM answer flow. The current implementation is a hybrid of:
- deterministic intent gates
- SQL-first direct resolution
- criteria-first document retrieval for list requests
- contextual LLM bridges for value extraction from document sets
- planner loop guardrails
- policy gates
- grounded deterministic answer formatting before final free-form answer generation

## 2. Current end-to-end pipeline

## 2.1 Intake and immediate special routing

1. `autonomous_query(...)` trims and validates the question.
2. If the question is an explicit SOLF clause request, it bypasses the normal retrieval pipeline and returns a `solf_clause` result.
3. If query cache is enabled and a cached answer exists, the cached result is returned.
4. `QueryEngine.parse_query(...)` builds the first structured interpretation.
5. `_classify_query_style(...)` derives an initial route such as:
   - `search_criteria` for list/filter requests
   - `lookup_attribute` for attribute/document-file requests
   - `resolve_scope` for broader semantic requests
6. Query policy and style policy are applied before heavy execution.

## 2.2 Direct resolution before planner loop

The current system now tries hard to avoid planner drift before entering the multi-step loop.

There are two important fast paths:

### A. Direct QueryEngine answer fast path

If parsed intent is one of:
- `attribute_lookup`
- `semantic_lookup`
- `relationship_lookup`

and the question is not a value-from-document-context question, `autonomous_query(...)` first calls:
- `QueryEngine.answer(question)`

If that returns a non-`not_found` answer, the planner loop is skipped entirely.

This is a major difference from the older description because many correct answers now finish before scope resolution, discovery search, or final LLM synthesis.

### B. Direct `lookup_attribute(...)` fast path

If parsed intent is `attribute_lookup` and both entity and attribute are already known, `autonomous_query(...)` directly calls:
- `QueryEngine.lookup_attribute(attribute_name, entity_name, scope=None)`

If found, it returns a deterministic `sql_exact` style answer immediately.

This path reduces cost and prevents vector/discovery absence from blocking simple SQL-backed questions.

## 2.3 Planner loop when direct paths do not finish

If direct resolution does not finish the query, the current planner loop runs.

### Planner action sources in priority order

1. Business-rule workflow hint from SOLF clauses.
2. Style-routed initial action from `_classify_query_style(...)`.
3. Media-intent routing to `search_discovery`.
4. LLM planner step.
5. Deterministic safety correction if LLM output is invalid.

Allowed actions are still:
- `resolve_scope`
- `search_discovery`
- `lookup_attribute`
- `search_criteria`
- `finalize`

But the current system adds strong guardrails:
- `criteria_lookup` queries are forced toward `search_criteria` before discovery-only loops.
- empty scope no longer automatically causes repeated scope/search churn
- value + document-context questions can force `search_criteria` first
- relation-driven queries can enforce strict relation traversal

## 2.4 Action execution behavior

### `resolve_scope`
Builds a scoped evidence bundle from Qdrant and Discovery:
- candidate counts
- node/edge/doc ids
- top candidates
- discovery results
- object/relationship/document slices

### `search_discovery`
Runs explicit discovery search and stores summary/results.

### `lookup_attribute`
Runs `QueryEngine.lookup_attribute(...)` with:
- parsed attribute/entity
- relation name when present
- scope when present
- criteria with strict relation flags when needed

Current implementation also includes recovery logic here, for example:
- canonical shareholding recovery when planner emits non-canonical share attributes
- shareholding scope recovery when no attribute result is found

### `search_criteria`
Runs SQL/semantic criteria retrieval for list/filter/document-match style queries.

## 2.5 Grounding post-processing before answer generation

After planner execution, the current pipeline does more grounding work than the old version described.

### A. Criteria contextual bridge

If `criteria_result` exists and the user asked for a value-style question grounded in documents rather than a list, the system can call:
- `_criteria_contextual_llm_resolution_fallback(...)`

This produces a grounded value answer from retrieved documents and returns source:
- `criteria_contextual_llm`

This is one of the biggest current changes.

### B. Anchor sanitization

If there is a parsed entity anchor and the intent is not `criteria_lookup`, grounding is sanitized so unrelated entities are removed before final answer synthesis.

This reduces hallucinated contamination from nearby but unrelated evidence.

### C. Deterministic criteria listing

Before any final free-form answer generation, the system now tries:
- `_deterministic_criteria_answer(...)`

This avoids vague count-only answers for explicit document/note listing requests.

### D. Final LLM answer synthesis only when needed

Only after the deterministic paths above fail does the system build an answer-generation prompt and call the answer model.

So in the current implementation, final LLM wording is no longer the default path for many successful queries.

## 3. Current `QueryEngine.parse_query(...)` behavior

The parser is much more layered than the older document suggests.

High-priority parsing stages now include:
- generic relationship gate
- explicit ownership gate
- file name + full path gate -> `document_file_info`
- degree/institution extraction
- table-cell extraction
- explicit lexical gates such as registration day and total capital
- deterministic semantic pattern matching
- optional LLM structured-plan candidate
- identifier-first multilingual attribute parsing
- criteria-first semantic/document routing

Important current parser characteristics:
- criteria/document/note requests are protected against attribute-pattern hijacking
- file-info requests are normalized into `document_file_info`
- shareholding/ownership questions are normalized into generic relation-aware forms
- multilingual identifier parsing exists before broad criteria routing

This parser is still hybrid, but it is more defensive and much more generic than the older workflow document describes.

## 4. Current `QueryEngine.answer(...)` pipeline

The current `answer(...)` implementation is not just a linear fallback chain anymore.

## 4.1 Multi-question split

If the input looks like a composite question, it may be split into subquestions first and merged back into a composite result.

## 4.2 Parse and normalize

1. `parse_query(...)`
2. `_normalize_parsed_for_execution(...)`
3. detect whether document-context value bridging should be preferred
4. optional explicit table-cell override

## 4.3 Candidate retrieval

The engine retrieves:
- Qdrant candidates
- Discovery candidates
- scoped node/edge/doc ids

These can support downstream SQL, document, and semantic fallback paths.

## 4.4 Criteria-first path

If parsed intent is `criteria_lookup`, current behavior is:
1. run `search_by_criteria(...)`
2. if the question is value-style, try `criteria_contextual_llm`
3. if response format is `entity_directory`, build a deterministic entity-directory payload
4. if response format is document-title/file listing, build deterministic listing text
5. otherwise return deterministic matches summary

This is a strong shift from older behavior, where criteria results were more likely to be summarized generically.

## 4.5 SQL-first attribute path

For `attribute_lookup`, current behavior heavily favors deterministic SQL-backed resolution:
- full-profile composite path -> `sql_profile_composite`
- share-count special relation path
- strict relation traversal path
- multi-attribute expansion and composite assembly -> `sql_exact_composite`
- derived-rule answer generation -> `sql_exact_derived`
- direct single attribute answer -> `sql_exact`

The current code also performs:
- canonical entity normalization
- relation-filter normalization
- semantic attribute candidate expansion
- LLM-guided attribute advice for broad-scope questions
- known-attribute expansion for broad profile-style requests

## 4.6 Strict safeguards

Current implementation has several stop conditions that intentionally avoid drifting into unrelated answers:
- strict relation lookup returns `not_found` instead of broad semantic fallback
- strict table-cell requests stop if grounded cell was not found
- identifier queries avoid some broader LLM fallback paths

This improves precision but can also change which source is returned compared with the older pipeline.

## 4.7 Retrieval-guided fallbacks

If SQL-first paths miss, current fallback order includes several grounded bridges:
- semantic document file info from candidate docs
- `qdrant_sql_fallback`
- `discovery_fallback`
- `semantic_llm_fallback`
- `sql_rescue_fallback`
- semantic contextual resolution for `semantic_lookup`
- LLM intent extraction followed by renewed SQL lookup
- final contextual LLM resolution
- relation-guided criteria contextual bridge
- generic value-document-context criteria bridge
- final `not_found`

So the modern fallback system is no longer a simple one-way descent. It includes multiple document-bridging loops that route back into grounded extraction.

## 5. Why correct answers now often look different

Many correct answers changed because the current pipeline tries to finish earlier and more deterministically.

Common reasons:

### 1. Deterministic SQL answers now win more often

Queries that previously went through planner -> scope -> final LLM may now terminate at:
- `sql_exact`
- `sql_exact_composite`
- `sql_profile_composite`

This changes wording, source labels, and sometimes answer structure.

### 2. Criteria queries are no longer allowed to drift as easily

Document/note/list requests now often go through:
- `criteria_docs_sql`
- `criteria_semantic_qdrant`
- deterministic criteria listing

instead of free-form LLM summaries.

This changes outputs from vague counts to explicit document/file lists.

### 3. Value-from-document queries now use document bridges

Questions that ask for a value grounded in one or more documents can now return through:
- `criteria_contextual_llm`

This did not have the same role in the older description.

### 4. Relation safety is stricter

Some questions that previously drifted into nearby entity attributes now either:
- recover through canonical relation logic
- or intentionally stop with `not_found`

This improves correctness, but it changes answer distribution and fallback behavior.

### 5. Final answer generation is no longer the default successful ending

The current system often reaches a good answer before final LLM answer synthesis. That means:
- fewer answers are phrased by the final answer model
- more answers expose underlying deterministic source types
- answer wording is more stable but may differ from the old style

## 6. Old vs current comparison

| Area | Older workflow doc | Current implementation |
|---|---|---|
| Direct resolver role | QueryEngine direct answer described as one phase | Used as an early fast path before planner loop in `autonomous_query(...)` |
| Attribute fast path | Not emphasized | Direct `lookup_attribute(...)` short-circuit exists before planner loop |
| Criteria routing | Criteria path existed, but planner/final answer still central | Criteria-first guardrails force SQL criteria retrieval before discovery drift |
| List answers | More LLM-summary oriented | Deterministic criteria listing now preferred |
| Document-context value extraction | Mentioned only indirectly through fallbacks | Explicit `criteria_contextual_llm` bridge is now a core route |
| Relation safety | Fallback matrix allowed broader fallback after misses | Strict relation lookup can stop fallback to avoid unrelated answers |
| Grounding cleanup | Not described as central | Anchor sanitization is now important before final answer synthesis |
| Shareholding recovery | Not described | Canonical shareholding attribute and scope recovery now exists |
| File info retrieval | High-priority intent mentioned | Dedicated `document_file_info` routing and semantic doc-id fallback are now stronger |
| Final answer generation | Presented as the normal terminal synthesis step | Often bypassed by deterministic answer paths |
| Output stability | More dependent on final LLM phrasing | More deterministic source-specific formatting |
| Fallback shape | Largely linear | Multi-bridge, route-correcting, and guarded |

## 7. Practical interpretation for regressions

When comparing old and new results, a regression should not be judged only by textual difference.

The current system may legitimately change along these dimensions while still being improved:
- different `answer_source`
- different answer wording
- deterministic list instead of count summary
- composite attribute answer instead of single-field answer
- stricter `not_found` instead of a broad but weak semantic answer

A better evaluation frame for the current system is:
- correctness of requested fact or list
- grounding quality
- whether unrelated entities were excluded
- whether the route is deterministic when it should be
- whether fallback occurred only when justified

## 8. Main architectural shift

The older workflow document described a mostly planner-centered retrieval pipeline with linear fallbacks.

The current system is better described as:
- parser- and SQL-first
- criteria-first for list/document retrieval
- planner-assisted rather than planner-dominated
- guarded against drift
- deterministic when possible
- LLM-bridged only when grounded evidence exists but direct structured lookup is insufficient

That is the main reason many correct answers now come back differently from before.

## 9. Recommended follow-up

If this file is kept as the current reference, the older file should be treated as historical documentation. Future regressions should compare:
- semantic correctness
- source-route correctness
- deterministic-vs-LLM route choice
- grounding relevance

rather than exact answer phrasing alone.

## 10. Concrete case study: EORI contact name + email query

Target query:

`Wie lauten der Name und die E-Mail-Adresse des Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?`

This query is a good comparison case because historically this area was brittle: contact-person wording, EORI wording, and multi-attribute retrieval could drift toward the wrong identifier field or time out.

### 10.1 Older approach: likely route and observed historical behavior

Under the older workflow description, this class of query was more likely to follow a planner-centered route:

1. parse query
2. enter policy gates
3. try direct factual route if parser happened to land on the right attribute
4. otherwise continue through planner loop
5. resolve scope / discovery / lookup
6. rely on final synthesis to produce the answer

Why this was fragile for this query:
- the old description did not emphasize generic process-contact extraction as a primary route
- document/process wording could drift toward nearby attributes such as `eori_no`
- the old flow relied more on later fallback behavior and final synthesis than on deterministic multi-attribute contact retrieval

Historically observed nearby outcomes support this:
- the English historical variant `What are the name and email address of the contact person for the EORI registration from Aphotonix GmbH?` appeared in retest runs and failed by timeout rather than returning a stable deterministic answer
- the German email-only variant `Was ist der email für Kontakt für EORI-Ansprechpartner von Aphotonix GmbH?` previously regressed to the wrong business identifier answer in an earlier historical report snapshot, returning the EORI number instead of an email

So the older approach was vulnerable in two ways:
- timeout / route instability
- semantic drift from contact-email intent to identifier lookup intent

### 10.2 Current approach: actual live route

Current live execution for the target query follows this route:

1. `autonomous_query(...)` starts
2. `parse_query(...)` builds an `attribute_lookup` parse for entity `Aphotonix GmbH`
3. parse metadata comes from `llm_structured_plan`
4. the structured plan identifies two targets:
   - `eori_contact_person`
   - `eori_contact_email`
5. relation filter is added for `eori_contact_person_of -> Aphotonix GmbH`
6. before the planner loop, `autonomous_query(...)` uses the direct QueryEngine answer fast path
7. `QueryEngine.answer(...)` returns immediately with `sql_exact_composite`
8. planner loop is skipped

So this query currently does not depend on:
- `resolve_scope`
- `search_discovery`
- final free-form answer synthesis

It succeeds through the newer deterministic-first architecture.

### 10.3 Current live answer

Current live answer returned:

`[source: sql_exact_composite] Aphotonix GmbH -> email of eori contact person: services@ynoves.ch.`

Current route metadata:
- `answer_source = sql_exact_composite`
- direct fast path used: `query_engine_direct_fallback`
- parsed intent: `attribute_lookup`
- parsed attribute seed: `eori_contact_person`
- structured-plan target attributes: `eori_contact_person`, `eori_contact_email`

### 10.4 Comparison of old vs current answer behavior

| Aspect | Older approach tendency | Current approach |
|---|---|---|
| Primary route | planner-centered with more late fallback risk | direct QueryEngine fast path before planner |
| Process-contact handling | weaker in workflow description, more drift-prone | explicit structured-plan + relation-filter grounding |
| Common failure mode | timeout or wrong EORI-number answer | deterministic SQL-backed answer |
| Returned source | unstable, often depending on fallback or failure | `sql_exact_composite` |
| Answer content | could be wrong or absent | returns correct email |
| Multi-attribute completeness | weak | improved, but still incomplete |

### 10.5 Important current limitation

Although the current approach is clearly better than the older one for this query, it is still not fully correct.

The user asked for:
- the contact name
- the email address

But the current live answer returns only:
- the email address

It does not return the contact name, even though the structured plan clearly identified both requested attributes.

That means the current system improved:
- routing correctness
- stability
- grounding

but still has a remaining completeness defect in composite attribute assembly for this query class.

### 10.6 Practical conclusion

For this query, the current approach is better than the older one because it:
- avoids planner drift
- avoids the old identifier-misrouting pattern
- avoids timeout in the tested live run
- returns a grounded SQL-backed result

### 10.7 Current result after the generic fix

After the generic lookup fix, the same query now returns both requested values:

`[source: sql_exact_composite] Aphotonix GmbH -> eori contact person: Daniel Trottmann; email of eori contact person: services@ynoves.ch.`

This improvement was not implemented as a query-specific exception. The change was generic:
- legacy `eori_contact_person` now routes through the same generic process-contact lookup path already used for process-contact email retrieval
- the structured plan still drives the requested attribute set
- composite answer assembly now returns both resolved attributes together for this query class

### 10.8 Provenance of `eori_contact_person` and `eori_contact_email`

These two names do not have the same provenance.

#### A. `eori_contact_person`

`eori_contact_person` is best understood as a query-layer canonical attribute label over an ingested relationship fact.

What is stored in the database is the relationship fact itself. For Aphotonix GmbH, the database contains a relationship row equivalent to:
- `APhotonix GmbH -> Daniel Trottmann -> has_eori_contact_person`

So the underlying contact-person link is ingestion-backed data in `object_relationship`.

However, the exact query-facing name `eori_contact_person` is not something that was found in the ingestion code as a raw document attribute being created in `ingest.py`. Instead, the query layer normalizes and exposes that stored relationship using canonical names such as:
- `eori_contact_person`
- `process_contact_person`

So this is partly ingestion-backed and partly query-normalized:
- stored fact: yes, as a relationship
- query-facing canonical name: yes, provided by parser/query normalization logic

#### B. `eori_contact_email`

`eori_contact_email` is not stored as a native attribute in the ingestion layer.

What is stored is:
- the related contact person entity
- that person’s regular `email` attribute

For this case, the database contains normal object attributes for Daniel Trottmann, including:
- `email = services@ynoves.ch`

The query result `eori_contact_email` is then derived at query time by:
1. finding the process/EORI contact person relationship for the company
2. following that relationship to the related person entity
3. reading the person’s ordinary `email` attribute
4. returning it under the query-facing semantic label `eori_contact_email`

So for `eori_contact_email` the answer is:
- not a raw ingestion-created document attribute with that exact name
- yes, a query-time derived semantic attribute over stored relationship + stored person email data

#### C. Practical interpretation

This means the recent fix did not invent Daniel Trottmann or his email.

The fix only corrected how already-stored facts are surfaced:
- ingested relationship: `has_eori_contact_person`
- ingested person attribute: `email`
- query-time semantic projections: `eori_contact_person`, `eori_contact_email`

So the data came from ingestion, but the exact query attribute names are part of the retrieval/normalization layer rather than raw document-field names inserted directly by ingestion.

### 10.9 Do we need to reprocess documents for the new entity/attribute/relationship forms?

For the changes described in this case study, the answer is generally no.

Why reprocessing is not required here:
- the recent improvements were implemented in the query layer, not in the ingestion contract
- no new storage schema was required for these process-contact queries
- the underlying facts were already present in the database:
   - relationship facts in `object_relationship`
   - normal attributes such as `email` in `attribute`
- the change was mainly about better parsing, normalization, arbitration, and query-time projection

So for this family of fixes:
- existing documents do not need to be re-ingested just to support the new query behavior
- existing rows can already answer the new generic process-contact and responsible-company queries

When reprocessing would be needed:
- if ingestion itself changes so that new entities, relationships, or attributes should be extracted and stored that were previously not stored at all
- if you want to canonicalize old stored relationship names into a new normalized ontology at rest rather than resolving aliases at query time
- if older documents were ingested before relationship/attribute extraction improved and therefore the needed facts are simply missing in the DB
- if you introduce new embedding/index artifacts that must exist physically for old records, and there is no backfill path

Practical rule:
- query-time normalization improvement only: no reprocessing needed
- ingestion-time extraction/storage improvement: reprocessing or backfill is usually needed

For the current process-contact work, this is a query-time normalization improvement, so the answer is no unless you later decide to rewrite stored relationship/attribute forms in the database for consistency.

### 10.10 Recommended ingestion-query contract for future process families

The safest long-term design is to keep ingestion responsible for durable factual storage and keep query resolution responsible for language normalization.

#### A. What ingestion should guarantee

Ingestion should not try to anticipate every user phrasing such as:
- "Who handled the VAT registration?"
- "Who is the licensing contact?"
- "Which company was responsible for the permit application?"

Instead, ingestion should guarantee that the underlying graph contains reusable facts in a stable form:
- a company object for the client or applicant
- a person object for the process contact when one exists
- a company object for the advisor, broker, law firm, or service provider when one exists
- a process-linked relationship whose name or metadata still preserves the process concept, for example:
   - `has_customs_contact_person`
   - `has_vat_contact_person`
   - `has_license_contact_person`
   - `has_permit_contact_person`
- normal person attributes such as `email`, `phone`, and optionally role/title
- an organization link such as `employed_by` when responsible-company inference should be possible
- source provenance through existing document/part linkage so answers can still be traced back

#### B. What query search should do

Query search should map natural-language variation onto that stored graph at runtime:
- identify the legal entity anchor from the question
- detect whether the user is asking for a contact person, contact email, phone, or responsible company
- infer the process concept from wording such as customs, EORI, VAT, licensing, permit, registration, application, or local-language variants
- resolve legacy or surface aliases into canonical query attributes such as:
   - `process_contact_person`
   - `process_contact_email`
   - `responsible_company`
- derive answer-friendly projections without requiring those exact attribute names to be stored at ingestion time

#### C. Recommended boundary

Good boundary:
- ingestion stores facts
- query resolves semantics
- answer assembly projects facts into user-facing fields

Avoid this boundary:
- ingestion creates many query-specific attributes just because users may ask for them later
- query logic depends on one exact stored relationship spelling with no aliasing or concept recovery

#### D. When ingestion should evolve

Ingestion should be changed only when the current stored graph is not expressive enough, for example:
- the source documents contain process contacts but no relationship is extracted at all
- responsible organizations exist in the source but there is no company-level object or link to represent them
- process identity is lost because relationship names and metadata do not preserve whether the contact belongs to customs, VAT, licensing, permit, or another process family
- new answer types require facts that are currently absent, such as phone, title, jurisdiction, filing date, or submission status

#### E. Practical recommendation for upcoming families

For future families such as licensing, permits, trade register, or other registrations:
- reuse the same storage pattern rather than inventing query-specific fields
- preserve the process concept in relationship naming or metadata
- keep person contact details as normal attributes on the person object
- keep responsible-company inference based on explicit company links when available

This keeps ingestion generic and scalable while letting query search improve independently over time.

### 10.11 Implementation checklist for ingestion alignment

When adding a new process family in ingestion, use this checklist.

#### A. Entity extraction

- create or resolve the client/applicant company object
- create or resolve the person object for the operational contact if the source names one
- create or resolve the service-provider company object if the source identifies an advisor, broker, law firm, agent, or filing company
- preserve stable names and normal dedupe metadata so later query-time resolution can find the same entities reliably

#### B. Relationship extraction

- create a process-linked contact relationship that preserves the process concept in its name or metadata
- prefer a consistent naming family such as `has_<process>_contact_person`
- if the filing or handling company is distinct from the client, store an explicit company link when possible rather than relying only on query-time inference
- if the contact person works for the responsible company, preserve that with `employed_by` or an equivalent stable organization link

#### C. Attribute extraction

- store contact details as normal person attributes, especially `email`
- store `phone`, `title`, `role`, or jurisdiction only when present in source material
- avoid manufacturing semantic query fields such as `process_contact_email` or `responsible_company` at ingestion time unless there is a strong storage-level reason to do so

#### D. Provenance and traceability

- keep the document and part linkage for extracted relationships and attributes
- keep enough metadata to understand which document fragment produced the contact or company link
- ensure new process-family extraction still participates in existing provenance patterns used elsewhere in the graph

#### E. Naming discipline

- preserve the process concept in a recoverable way, either in relationship naming, normalized metadata, or both
- avoid opaque relationship names that lose whether the fact belongs to customs, VAT, licensing, permit, trade register, or another process family
- prefer one consistent pattern across families rather than one-off names per source system

#### F. Backfill trigger points

Consider reprocessing or backfill only if one of these is true:
- old records are missing the process relationship entirely
- old records have the person but no contact attributes such as `email`
- old records have the contact but no company link for responsible-company inference
- old records use relationship names so inconsistent that generic concept recovery becomes unreliable

### 10.12 Regression-alignment note for future process families

Whenever a new process family is added, keep ingestion and query behavior aligned by validating all three layers.

#### A. Parser and routing regression expectations

Add or confirm query-style regressions for:
- contact-person queries for the new process family
- contact email queries for the new process family
- responsible-company queries for the new process family
- at least one wording variant that uses registration or application phrasing rather than only the raw process noun

Expected runtime behavior:
- parser resolves to canonical query attributes, not one-off family-specific attribute names
- process concept is extracted generically from the question
- answer assembly can produce composite responses when name and email are requested together

#### B. Live seeded integration expectations

For each important new family, add or extend live seeded tests that verify:
- seeded process contact relationship is discoverable through the live API
- contact name and email can be returned together
- responsible company can be inferred or resolved from stored graph links
- the result source remains the expected exact SQL composite path when the graph is sufficient

#### C. Failure interpretation guide

If a new family fails at runtime, classify the failure before changing ingestion:
- parse failure: the wording is not being normalized into the generic process route
- lookup failure: the graph shape exists but the retrieval path cannot find it
- data failure: the necessary relationship or attributes were never stored

Only the last category clearly requires ingestion or backfill work.

#### D. Minimum acceptance rule

Before calling a new family supported, confirm:
- at least one parser regression passes
- at least one composite answer regression passes
- at least one live seeded API test passes end to end

This keeps future process-family expansion disciplined and prevents ingestion changes from being driven by isolated query phrasings.
