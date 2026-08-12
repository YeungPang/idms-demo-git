import tempfile
import unittest
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
import json

import ingest


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        return None


class _CursorStub:
    def __init__(self, fetchone_results: list[object]) -> None:
        self._fetchone_results = list(fetchone_results)
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> object:
        if not self._fetchone_results:
            return None
        return self._fetchone_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _ConnectionStub:
    def __init__(self, fetchone_results: list[object]) -> None:
        self.cursor_stub = _CursorStub(fetchone_results)
        self.commit_count = 0

    def cursor(self) -> _CursorStub:
        return self.cursor_stub

    def commit(self) -> None:
        self.commit_count += 1


class TestIngestMarkdownWorkflow(unittest.TestCase):
    def test_person_similarity_review_filters_surname_only_candidates(self):
        ingested_payload = {
            "solf_entities": [
                {
                    "entity_id": "e1",
                    "class_name": "person",
                    "name": "Chung Yeung Pang",
                }
            ]
        }

        db_summary = {"entity_rows": []}
        candidates = [
            {
                "object_id": 9,
                "object_name": "Käthi Johanna Pang Gerber",
                "similarity": 0.25,
                "token_overlap": ["pang"],
            },
            {
                "object_id": 18,
                "object_name": "Yeung Pang",
                "similarity": 0.29,
                "token_overlap": ["pang", "yeung"],
            },
        ]

        with patch.object(ingest.object_db, "get_connection", return_value=_DummyConn()), patch.object(
            ingest.object_db,
            "suggest_similar_objects",
            return_value=candidates,
        ):
            review = ingest._build_person_similarity_review(ingested_payload, db_summary)

        self.assertTrue(review.get("attention_required"))
        alerts = review.get("alerts") or []
        self.assertEqual(len(alerts), 1)
        reviewed_candidates = alerts[0].get("candidates") or []
        self.assertEqual(len(reviewed_candidates), 1)
        self.assertEqual(reviewed_candidates[0].get("object_name"), "Yeung Pang")
        self.assertEqual(reviewed_candidates[0].get("confidence_level"), "medium")

    def test_person_similarity_review_keeps_two_token_alias_candidate(self):
        ingested_payload = {
            "solf_entities": [
                {
                    "entity_id": "e1",
                    "class_name": "person",
                    "name": "Käthi Pang",
                }
            ]
        }

        db_summary = {"entity_rows": []}
        candidates = [
            {
                "object_id": 9,
                "object_name": "Käthi Johanna Pang Gerber",
                "similarity": 0.31,
                "token_overlap": ["käthi", "pang"],
            }
        ]

        with patch.object(ingest.object_db, "get_connection", return_value=_DummyConn()), patch.object(
            ingest.object_db,
            "suggest_similar_objects",
            return_value=candidates,
        ):
            review = ingest._build_person_similarity_review(ingested_payload, db_summary)

        alerts = review.get("alerts") or []
        self.assertEqual(len(alerts), 1)
        reviewed_candidates = alerts[0].get("candidates") or []
        self.assertEqual(reviewed_candidates[0].get("object_name"), "Käthi Johanna Pang Gerber")
        self.assertEqual(reviewed_candidates[0].get("confidence_level"), "medium")

    def test_person_similarity_review_suppresses_common_surname_initial_only_overlap(self):
        ingested_payload = {
            "solf_entities": [
                {
                    "entity_id": "e1",
                    "class_name": "person",
                    "name": "John A Miller",
                }
            ]
        }

        db_summary = {"entity_rows": []}
        candidates = [
            {
                "object_id": 27,
                "object_name": "James A Miller",
                "similarity": 0.26,
                "token_overlap": ["a", "miller"],
            }
        ]

        with patch.object(ingest.object_db, "get_connection", return_value=_DummyConn()), patch.object(
            ingest.object_db,
            "suggest_similar_objects",
            return_value=candidates,
        ):
            review = ingest._build_person_similarity_review(ingested_payload, db_summary)

        self.assertFalse(review.get("attention_required"))
        self.assertEqual(review.get("alerts"), [])

    def test_document_requires_default_booking_process_for_paid_ticket(self):
        document_payload = {"doc_type": "ticket"}
        solf_entities = [
            {
                "entity_id": "e1",
                "class_name": "ticket",
                "attributes": {"fare_amount": "19.90", "currency": "CHF"},
            }
        ]

        self.assertTrue(ingest._document_requires_default_booking_process(document_payload, solf_entities))

    def test_document_requires_default_booking_process_skips_zero_value_ticket(self):
        document_payload = {"doc_type": "ticket"}
        solf_entities = [
            {
                "entity_id": "e1",
                "class_name": "ticket",
                "attributes": {"fare_amount": "0.00", "currency": "CHF"},
            }
        ]

        self.assertFalse(ingest._document_requires_default_booking_process(document_payload, solf_entities))

    def test_document_requires_default_booking_process_for_email_with_paid_ticket_entity(self):
        document_payload = {"doc_type": "email"}
        solf_entities = [
            {
                "entity_id": "e1",
                "class_name": "ticket",
                "attributes": {"gross_amount": "26.50", "currency": "CHF"},
            }
        ]

        self.assertTrue(ingest._document_requires_default_booking_process(document_payload, solf_entities))

    def test_document_requires_default_booking_process_for_email_with_invoice_entity(self):
        document_payload = {"doc_type": "email"}
        solf_entities = [
            {
                "entity_id": "e1",
                "class_name": "invoice",
                "attributes": {"gross_amount": "706.20", "currency": "CHF"},
            }
        ]

        self.assertTrue(ingest._document_requires_default_booking_process(document_payload, solf_entities))

    def test_document_requires_default_booking_process_for_email_with_generic_ticket_entity(self):
        document_payload = {"doc_type": "email"}
        solf_entities = [
            {
                "entity_id": "e1",
                "entity_name": "Ticket 1",
                "class_name": "entity",
                "attributes": {
                    "ticket_type": "Point-to-point Ticket",
                    "amount": "26.50",
                    "currency": "CHF",
                },
            }
        ]

        self.assertTrue(ingest._document_requires_default_booking_process(document_payload, solf_entities))

    def test_extract_company_registration_metadata_prefers_full_eintragung_date(self):
        markdown_text = """Kanton Zug                   Handelsregisteramt des Kantons Zug

# Firmennummer

CH-170.4.023.751-7

CHE-399.737.068      Gesellschaft mit beschränkter Haftung

Eintragung           Löschung   Übertrag

23.09.2025               von:                          1

auf:

# Ei LöBesondere Tatbestände

| Ref | TR-Nr   | TR-Datum | SHAB | SHAB-Dat.          | Seite / Id | Ref | TR-Nr | TR-Datum | SHAB | SHAB-Dat. | Seite / Id |
| --- | ------- | -------- | ---- | ------------------ | ---------- | --- | ----- | -------- | ---- | --------- | ---------- |
| 1   | 1706223 | 09.2025  |      | (Genehmigung EHRA) |            |     |       |          |      |           |            |
"""

        extracted = ingest._extract_company_registration_metadata_from_markdown(markdown_text)

        self.assertEqual(extracted["tr_number"], "1706223")
        self.assertEqual(extracted["tr_date"], "2025-09-23")

    def test_resolve_original_local_path_prefers_absolute_and_ignores_uri(self):
        resolved = ingest._resolve_original_local_path(
            "gs://bucket/aphotonix.pdf",
            r"C:\\Seveco\\Aphotonix\\Aphotonix_registration.pdf",
            "Aphotonix_registration.pdf",
        )
        self.assertEqual(resolved, r"C:\Seveco\Aphotonix\Aphotonix_registration.pdf")

    def test_resolve_original_local_path_resolves_existing_relative_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            current = Path.cwd()
            try:
                os.chdir(temp_dir)
                rel = Path("Aphotonix_registration.pdf")
                rel.write_text("stub", encoding="utf-8")

                resolved = ingest._resolve_original_local_path(str(rel))

                self.assertTrue(Path(resolved).is_absolute())
                self.assertEqual(Path(resolved), rel.resolve())
            finally:
                os.chdir(current)

    def _run_ingest_with_common_patches(
        self,
        *,
        fake_cache_path: Path,
        source_path_or_uri: str = "receipt.pdf",
        routed: dict,
        extracted: dict,
        ingested: dict,
        db_summary: dict,
        reuse_markdown: bool,
        persist_markdown: bool,
        skip_markdown_generation: bool,
        direct_markdown_text: str | None = None,
        document_persistence_mode_override: dict | None = None,
    ) -> tuple[dict, MagicMock]:
        with ExitStack() as stack:
            original_path_exists = ingest.Path.exists

            def _fake_path_exists(path_obj):
                if str(path_obj) == str(Path(source_path_or_uri)):
                    return True
                return original_path_exists(path_obj)

            stack.enter_context(patch.object(ingest, "PROJECT_ID", "test-project"))
            stack.enter_context(patch.object(ingest, "configure_logging", return_value=None))
            stack.enter_context(patch.object(ingest, "parse_solf_classes", return_value={}))
            stack.enter_context(patch.object(ingest, "build_solf_interpreter", return_value=MagicMock()))
            stack.enter_context(patch.object(ingest, "get_openrouter_client", return_value=MagicMock()))
            stack.enter_context(patch.object(ingest, "upload_if_local", return_value="gs://bucket/receipt.pdf"))
            stack.enter_context(patch.object(ingest, "generate_json", side_effect=[routed, extracted]))
            stack.enter_context(patch.object(ingest, "apply_domain_action_policy", return_value=extracted))
            stack.enter_context(patch.object(ingest, "enforce_domain_action_policy", return_value=None))
            stack.enter_context(
                patch.object(
                    ingest.business_rules,
                    "apply_post_extraction_business_rules",
                    return_value={"extracted": extracted, "applied_count": 0, "applied_rules": []},
                )
            )
            stack.enter_context(patch.object(ingest, "to_solf_objects", return_value=ingested))
            stack.enter_context(
                patch.object(
                    ingest,
                    "_md_gen",
                    MagicMock(file_to_markdown_openrouter=MagicMock(return_value="# generated markdown")),
                )
            )
            stack.enter_context(patch.object(ingest.Path, "exists", _fake_path_exists))
            stack.enter_context(patch.object(ingest, "load_approved_solf_attribute_extensions", return_value={}))
            if direct_markdown_text is not None:
                stack.enter_context(patch.object(ingest, "get_direct_markdown_text", return_value=direct_markdown_text))
            stack.enter_context(patch.object(ingest, "_markdown_cache_path", return_value=fake_cache_path))
            markdown_loader = stack.enter_context(
                patch.object(
                    ingest,
                    "load_or_generate_markdown",
                    return_value=("# markdown", fake_cache_path, False),
                )
            )
            stack.enter_context(patch.object(ingest.object_db, "get_connection", return_value=_DummyConn()))
            stack.enter_context(patch.object(ingest.object_db, "create_tables", return_value=None))
            stack.enter_context(patch.object(ingest, "ensure_domain_schema", return_value=None))
            stack.enter_context(patch.object(ingest, "ensure_solf_classes_in_db", return_value=None))
            insert_document_mock = stack.enter_context(
                patch.object(ingest, "insert_document_record", return_value=db_summary["doc_id"])
            )
            stack.enter_context(patch.object(ingest, "persist_solf_objects", return_value=db_summary))
            if document_persistence_mode_override is not None:
                stack.enter_context(
                    patch.object(
                        ingest,
                        "detect_document_persistence_mode",
                        return_value=document_persistence_mode_override,
                    )
                )
            stack.enter_context(patch.object(ingest, "infer_source_mime_type", return_value="application/pdf"))
            stack.enter_context(patch.object(ingest, "should_use_discovery_for_source", return_value=False))
            stack.enter_context(
                patch.object(ingest, "select_indexing_backend", return_value=(False, False, False, "none"))
            )
            stack.enter_context(patch.object(ingest, "build_adk_context_payload", return_value={"ok": True}))

            result = ingest.run_ingest(
                source_path_or_uri=source_path_or_uri,
                reuse_markdown=reuse_markdown,
                persist_markdown=persist_markdown,
                skip_markdown_generation=skip_markdown_generation,
            )

        return result, markdown_loader, insert_document_mock

    def test_load_or_generate_markdown_no_persist_does_not_write_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "receipt-test.md"

            with patch.object(ingest, "_markdown_cache_path", return_value=cache_path), patch.object(
                ingest,
                "generate_markdown_from_document",
                return_value="# generated markdown",
            ):
                markdown_text, returned_cache_path, reused = ingest.load_or_generate_markdown(
                    client=MagicMock(),
                    source_path_or_uri="receipt.pdf",
                    gcs_uri="gs://bucket/receipt.pdf",
                    extracted={"document": {"doc_type": "receipt"}},
                    reuse_markdown=False,
                    persist_markdown=False,
                )

        self.assertEqual(markdown_text, "# generated markdown")
        self.assertIsNone(returned_cache_path)
        self.assertFalse(reused)
        self.assertFalse(cache_path.exists())

    def test_run_ingest_wires_markdown_flags_and_records_workflow_trace(self):
        routed = {
            "document_type": "receipt",
            "language": "en",
            "entity_type_hints": [],
            "semantic_notes": [],
        }
        extracted = {"document": {"doc_type": "receipt"}, "entities": [], "relationships": []}
        ingested = {
            "document": {"doc_key": "r-1", "doc_type": "receipt", "metadata": {}, "keywords": []},
            "document_effective_date": "2026-05-01",
            "document_recorded_date": "2026-05-02",
            "solf_entities": [],
            "solf_relationships": [],
        }
        db_summary = {
            "doc_id": 1,
            "objects_upserted": 0,
            "relationships_upserted": 0,
            "ambiguities_queued": 0,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_cache_path = Path(temp_dir) / "receipt-run.md"
            result, markdown_loader, insert_document_mock = self._run_ingest_with_common_patches(
                fake_cache_path=fake_cache_path,
                source_path_or_uri="receipt.pdf",
                routed=routed,
                extracted=extracted,
                ingested=ingested,
                db_summary=db_summary,
                reuse_markdown=False,
                persist_markdown=False,
                skip_markdown_generation=False,
            )

        self.assertTrue(result["markdown_reused"])
        self.assertEqual(result["markdown"], "# generated markdown")

        markdown_loader.assert_not_called()

        insert_document_mock.assert_called_once_with(
            unittest.mock.ANY,
            "gs://bucket/receipt.pdf",
            ingested["document"],
            "2026-05-01",
            "2026-05-02",
        )

        trace_steps = [entry.get("step") for entry in result.get("workflow_trace", [])]
        self.assertIn("load_solf", trace_steps)
        self.assertIn("upload_or_resolve_source", trace_steps)
        self.assertIn("genai_router", trace_steps)
        self.assertIn("genai_extract", trace_steps)
        self.assertIn("apply_domain_action_policy", trace_steps)
        self.assertIn("markdown_generation", trace_steps)
        self.assertIn("persist_objects", trace_steps)

        markdown_steps = [
            entry for entry in result.get("workflow_trace", []) if entry.get("step") == "markdown_generation"
        ]
        self.assertTrue(markdown_steps)
        self.assertEqual(markdown_steps[-1].get("reason"), "reuse_preextract_markdown")

    def test_run_ingest_skips_person_similarity_review_for_update_existing(self):
        routed = {
            "document_type": "receipt",
            "language": "en",
            "entity_type_hints": [],
            "semantic_notes": [],
        }
        extracted = {"document": {"doc_type": "receipt"}, "entities": [], "relationships": []}
        ingested = {
            "document": {"doc_key": "r-3", "doc_type": "receipt", "metadata": {}, "keywords": []},
            "document_effective_date": "2026-05-01",
            "document_recorded_date": "2026-05-02",
            "solf_entities": [],
            "solf_relationships": [],
        }
        db_summary = {
            "doc_id": 5,
            "objects_upserted": 0,
            "relationships_upserted": 0,
            "ambiguities_queued": 0,
            "document_persistence_action": "update_existing",
            "document_persistence_match": {"matched_by": "source_identity"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_cache_path = Path(temp_dir) / "receipt-update-existing.md"
            with patch.object(ingest, "_build_person_similarity_review") as review_builder:
                result, _markdown_loader, _insert_document_mock = self._run_ingest_with_common_patches(
                    fake_cache_path=fake_cache_path,
                    source_path_or_uri="receipt.pdf",
                    routed=routed,
                    extracted=extracted,
                    ingested=ingested,
                    db_summary=db_summary,
                    reuse_markdown=False,
                    persist_markdown=False,
                    skip_markdown_generation=False,
                    document_persistence_mode_override={
                        "persistence_action": "update_existing",
                        "matched_by": "source_identity",
                        "doc_id": 5,
                        "doc_key": "r-3",
                        "doc_name": "receipt.pdf",
                        "match_value": "receipt.pdf",
                    },
                )

        review_builder.assert_not_called()
        review = result.get("person_similarity_review") if isinstance(result.get("person_similarity_review"), dict) else {}
        self.assertTrue(review.get("skipped"))
        self.assertEqual(review.get("skip_reason"), "update_existing_same_source_identity_or_path")

        similarity_steps = [
            entry for entry in result.get("workflow_trace", []) if entry.get("step") == "person_similarity_review"
        ]
        self.assertTrue(similarity_steps)
        self.assertEqual(similarity_steps[-1].get("status"), "skipped")

    def test_run_ingest_skip_markdown_generation_records_skipped_step(self):
        routed = {
            "document_type": "receipt",
            "language": "en",
            "entity_type_hints": [],
            "semantic_notes": [],
        }
        extracted = {"document": {"doc_type": "receipt"}, "entities": [], "relationships": []}
        ingested = {
            "document": {"doc_key": "r-2", "doc_type": "receipt", "metadata": {}, "keywords": []},
            "solf_entities": [],
            "solf_relationships": [],
        }
        db_summary = {
            "doc_id": 2,
            "objects_upserted": 0,
            "relationships_upserted": 0,
            "ambiguities_queued": 0,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_cache_path = Path(temp_dir) / "missing-cache.md"
            result, markdown_loader, _insert_document_mock = self._run_ingest_with_common_patches(
                fake_cache_path=fake_cache_path,
                source_path_or_uri="receipt.pdf",
                routed=routed,
                extracted=extracted,
                ingested=ingested,
                db_summary=db_summary,
                reuse_markdown=False,
                persist_markdown=False,
                skip_markdown_generation=True,
            )

        markdown_loader.assert_not_called()
        self.assertEqual(result["markdown"], "")
        self.assertFalse(result["markdown_reused"])

        markdown_steps = [
            entry for entry in result.get("workflow_trace", []) if entry.get("step") == "markdown_generation"
        ]
        self.assertTrue(markdown_steps)
        self.assertEqual(markdown_steps[-1].get("status"), "skipped")

    def test_run_ingest_auto_skips_markdown_for_plain_text_sources(self):
        routed = {
            "document_type": "general_information",
            "language": "en",
            "entity_type_hints": [],
            "semantic_notes": [],
        }
        extracted = {"document": {"doc_type": "general_information"}, "entities": [], "relationships": []}
        ingested = {
            "document": {"doc_key": "t-1", "doc_type": "general_information", "metadata": {}, "keywords": []},
            "solf_entities": [],
            "solf_relationships": [],
        }
        db_summary = {
            "doc_id": 3,
            "objects_upserted": 0,
            "relationships_upserted": 0,
            "ambiguities_queued": 0,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            text_path = Path(temp_dir) / "note.txt"
            text_path.write_text("line one\nline two", encoding="utf-8")
            fake_cache_path = Path(temp_dir) / "unused-cache.md"

            result, markdown_loader, _insert_document_mock = self._run_ingest_with_common_patches(
                fake_cache_path=fake_cache_path,
                source_path_or_uri=str(text_path),
                routed=routed,
                extracted=extracted,
                ingested=ingested,
                db_summary=db_summary,
                reuse_markdown=False,
                persist_markdown=False,
                skip_markdown_generation=False,
            )

        markdown_loader.assert_not_called()
        self.assertEqual(result["markdown"], "line one\nline two")

        markdown_steps = [
            entry for entry in result.get("workflow_trace", []) if entry.get("step") == "markdown_generation"
        ]
        self.assertTrue(markdown_steps)
        self.assertEqual(markdown_steps[-1].get("status"), "skipped")
        self.assertEqual(markdown_steps[-1].get("reason"), "auto_skip_plain_text_source")

    def test_run_ingest_extracts_structured_tables_from_spreadsheet_text(self):
        routed = {
            "document_type": "invoice",
            "language": "en",
            "entity_type_hints": [],
            "semantic_notes": [],
        }
        extracted = {"document": {"doc_type": "invoice"}, "entities": [], "relationships": []}
        ingested = {
            "document": {"doc_key": "inv-1", "doc_type": "invoice", "metadata": {}, "keywords": []},
            "solf_entities": [],
            "solf_relationships": [],
        }
        db_summary = {
            "doc_id": 4,
            "objects_upserted": 0,
            "relationships_upserted": 0,
            "ambiguities_queued": 0,
        }

        spreadsheet_text = "[xl/worksheets/sheet1.xml]\nDescription\tAmount\nConsulting\t100.00\nTax\t7.70"

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_cache_path = Path(temp_dir) / "invoice-run.md"
            result, markdown_loader, insert_document_mock = self._run_ingest_with_common_patches(
                fake_cache_path=fake_cache_path,
                source_path_or_uri="invoice.xlsx",
                routed=routed,
                extracted=extracted,
                ingested=ingested,
                db_summary=db_summary,
                reuse_markdown=False,
                persist_markdown=False,
                skip_markdown_generation=False,
                direct_markdown_text=spreadsheet_text,
            )

        markdown_loader.assert_not_called()
        self.assertEqual(result["markdown"], spreadsheet_text)

        insert_document_mock.assert_called_once()
        stored_document = insert_document_mock.call_args.args[2]
        structured_tables = stored_document.get("metadata", {}).get("structured_tables", {})
        self.assertEqual(structured_tables.get("table_count"), 1)
        self.assertEqual(structured_tables.get("source_kind"), "spreadsheet")

        tables = structured_tables.get("tables") or []
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table.get("source"), "xl/worksheets/sheet1.xml")
        self.assertEqual(table.get("table_dimension"), "1d")
        self.assertEqual(table.get("column_count"), 2)
        self.assertEqual(table.get("normalized_headers"), ["description", "amount"])
        self.assertEqual(table.get("records")[0].get("description"), "Consulting")

        markdown_steps = [
            entry for entry in result.get("workflow_trace", []) if entry.get("step") == "extract_structured_tables"
        ]
        self.assertTrue(markdown_steps)
        self.assertEqual(markdown_steps[-1].get("table_count"), 1)

    def test_extract_structured_tables_parses_markdown_and_tabular_blocks(self):
        tables = ingest.extract_structured_tables(
            "| Name | Value |\n| --- | --- |\n| Invoice | 123 |\n\nSection\tNote\nA\tAlpha\nB\tBeta"
        )

        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].get("table_dimension"), "1d")
        self.assertEqual(tables[0].get("normalized_headers"), ["name", "value"])
        self.assertEqual(tables[0].get("records")[0].get("name"), "Invoice")
        self.assertEqual(tables[1].get("source_kind"), "tabular")
        self.assertEqual(tables[1].get("column_count"), 2)

    def test_apply_structured_table_enrichment_maps_key_values_and_line_items(self):
        extracted = {
            "document": {
                "doc_type": "invoice",
                "doc_key": "INV-001",
                "metadata": {
                    "structured_tables": {
                        "table_count": 2,
                        "source_kind": "spreadsheet",
                        "tables": [
                            {
                                "table_index": 1,
                                "table_dimension": "1d",
                                "normalized_headers": ["field", "value"],
                                "records": [
                                    {"field": "invoice_no", "value": "INV-001", "row_index": 1},
                                    {"field": "status", "value": "draft", "row_index": 2},
                                ],
                            },
                            {
                                "table_index": 2,
                                "table_dimension": "2d",
                                "normalized_headers": ["description", "amount"],
                                "records": [
                                    {"description": "Consulting", "amount": "100.00", "row_index": 1},
                                    {"description": "Tax", "amount": "7.70", "row_index": 2},
                                ],
                            },
                        ],
                    }
                },
            },
            "entities": [
                {
                    "entity_id": "invoice_1",
                    "entity_name": "INV-001",
                    "class_name": "invoice",
                    "attributes": {},
                }
            ],
            "relationships": [],
        }

        enriched = ingest._apply_structured_table_enrichment(
            extracted,
            document_type="invoice",
            class_defs={"invoice": object(), "invoice_line": object()},
        )

        invoice_entity = enriched["entities"][0]
        self.assertEqual(invoice_entity["attributes"]["invoice_no"], "INV-001")
        self.assertEqual(invoice_entity["attributes"]["status"], "draft")

        line_items = [entity for entity in enriched["entities"] if entity.get("class_name") == "invoice_line"]
        self.assertEqual(len(line_items), 2)
        self.assertEqual(line_items[0]["attributes"]["description"], "Consulting")
        self.assertEqual(line_items[0]["attributes"]["net_amount"], "100.00")

        relationships = enriched["relationships"]
        self.assertEqual(len(relationships), 2)
        self.assertTrue(all(rel.get("relationship_type") == "has_line_item" for rel in relationships))

    def test_generate_json_logs_response_payload(self):
        response = MagicMock()
        response.text = '{"document_type":"receipt","language":"en"}'
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch.object(ingest, "build_model_contents", return_value=["prompt"]), patch.object(
            ingest,
            "generate_content_with_openrouter_fallback",
            return_value=response,
        ), patch.object(
            ingest.LOGGER, "info"
        ) as log_info:
            parsed = ingest.generate_json(
                client=client,
                model="router-model",
                source_path_or_uri="receipt.pdf",
                gcs_uri="gs://bucket/receipt.pdf",
                prompt="route this",
                call_name="router",
                run_id="run-123",
            )

        self.assertEqual(parsed.get("document_type"), "receipt")
        self.assertTrue(log_info.called)
        self.assertEqual(log_info.call_args[0][1], "run-123")
        self.assertEqual(log_info.call_args[0][2], "router")
        self.assertEqual(log_info.call_args[0][3], "router-model")

    def test_generate_json_parses_fenced_json_response(self):
        response = MagicMock()
        response.text = "```json\n{\"document\": {\"doc_key\": \"x.pdf\", \"doc_type\": \"invoice\"}}\n```"
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch.object(ingest, "build_model_contents", return_value=["prompt"]), patch.object(
            ingest,
            "generate_content_with_openrouter_fallback",
            return_value=response,
        ):
            parsed = ingest.generate_json(
                client=client,
                model="extract-model",
                source_path_or_uri="x.pdf",
                gcs_uri="gs://bucket/x.pdf",
                prompt="extract",
                call_name="extract",
                run_id="run-fenced",
            )

        self.assertEqual(str((parsed.get("document") or {}).get("doc_type") or ""), "invoice")

    def test_generate_json_autocloses_truncated_object(self):
        response = MagicMock()
        response.text = "```json\n{\"document\": {\"doc_key\": \"x.pdf\", \"doc_type\": \"invoice\", \"keywords\": [\"travel\", \"booking\", \"train\",\n"
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch.object(ingest, "build_model_contents", return_value=["prompt"]), patch.object(
            ingest,
            "generate_content_with_openrouter_fallback",
            return_value=response,
        ):
            parsed = ingest.generate_json(
                client=client,
                model="extract-model",
                source_path_or_uri="x.pdf",
                gcs_uri="gs://bucket/x.pdf",
                prompt="extract",
                call_name="extract",
                run_id="run-truncated",
            )

        document = parsed.get("document") if isinstance(parsed.get("document"), dict) else {}
        self.assertEqual(str(document.get("doc_type") or ""), "invoice")
        self.assertEqual(document.get("keywords"), ["travel", "booking", "train"])

    def test_localize_user_description_logs_json_response(self):
        response = MagicMock()
        response.text = '{"localized_description":"Hallo"}'
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch.object(ingest.LOGGER, "info") as log_info:
            localized = ingest.localize_user_description(client, "Hello", "de", run_id="run-xyz")

        self.assertEqual(localized, "Hallo")
        self.assertTrue(log_info.called)
        self.assertEqual(log_info.call_args[0][1], "run-xyz")
        self.assertEqual(log_info.call_args[0][2], "localize_user_description")

    def test_insert_document_record_reuses_existing_row_for_same_source(self):
        connection = _ConnectionStub(fetchone_results=[(7,)])
        document = {
            "doc_key": "APHOTONIX_GMBH_REG_2025",
            "doc_cat": "legal",
            "doc_type": "general_information",
            "doc_date": "2025-09-24",
            "doc_theme": None,
            "keywords": ["registration"],
            "identifiers": {"uid_che": "CHE-399.737.068"},
            "metadata": {
                "user_description": "Aphotonix Unternehmensregistrierungsdokument zur Entitaetsextraktion",
                "user_tags": ["registration", "company", "aphotonix"],
                "user_metadata": {
                    "client_file_name": "Aphotonix_registration.pdf",
                    "source_reference": {
                        "source_path_or_uri": "gs://bucket/aphotonix.pdf",
                        "gcs_uri": "gs://bucket/aphotonix.pdf",
                    },
                },
            },
        }

        doc_id = ingest.insert_document_record(connection, "gs://bucket/aphotonix.pdf", document)

        self.assertEqual(doc_id, 7)
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(len(connection.cursor_stub.executed), 1)

        executed_sql, executed_params = connection.cursor_stub.executed[0]
        self.assertIn("UPDATE document", executed_sql)
        self.assertEqual(executed_params[-1], "gs://bucket/aphotonix.pdf")
        self.assertEqual(executed_params[0], "Aphotonix_registration.pdf")
        self.assertEqual(executed_params[1], "APHOTONIX_GMBH_REG_2025")
        self.assertEqual(executed_params[2], "gs://bucket/aphotonix.pdf")
        self.assertEqual(json.loads(executed_params[9]), {"uid_che": "CHE-399.737.068"})

    def test_insert_document_record_reuses_existing_row_for_same_source_identity(self):
        connection = _ConnectionStub(fetchone_results=[None, (9,)])
        document = {
            "doc_key": "APHOTONIX_GMBH_REG_2025",
            "doc_cat": "legal",
            "doc_type": "general_information",
            "doc_date": "2025-09-24",
            "doc_theme": None,
            "keywords": [],
            "identifiers": {},
            "metadata": {
                "user_metadata": {
                    "source_reference": {
                        "source_path_or_uri": "gs://bucket/aphotonix.pdf",
                    },
                },
            },
        }

        doc_id = ingest.insert_document_record(connection, "gs://bucket/aphotonix.pdf", document)

        self.assertEqual(doc_id, 9)
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(len(connection.cursor_stub.executed), 2)
        self.assertIn("UPDATE document", connection.cursor_stub.executed[0][0])
        self.assertIn("UPDATE document", connection.cursor_stub.executed[1][0])


if __name__ == "__main__":
    unittest.main()