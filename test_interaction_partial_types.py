"""Regression tests for previously partial interaction types.

Covers:
- ingest_document action branch
- ingest_information action branch
- generate_document action branch
- natural-key SQL identifier normalization and graceful SQL failures
"""

import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

import business_rules
import domain_function
from interaction import IDMSInteractionTools


class _DummyEventBus:
    def __init__(self):
        self.events = []

    def emit(self, event_name, source_entity, entity_id, payload, tags):
        self.events.append(
            {
                "event_name": event_name,
                "source_entity": source_entity,
                "entity_id": entity_id,
                "payload": payload,
                "tags": tags,
            }
        )


class TestInteractionPartialTypes(unittest.TestCase):
    def _build_tools_for_perform_action(self) -> IDMSInteractionTools:
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        tools._log_action_execution = lambda *_args, **_kwargs: None
        tools._compose_action_plan = lambda *_args, **_kwargs: None
        tools.event_bus = _DummyEventBus()
        return tools

    def test_ingest_document_action(self):
        tools = self._build_tools_for_perform_action()
        tools.ingest_document = MagicMock(return_value={"ingested": True, "source": "doc.txt"})

        result = tools.perform_action(
            "ingest_document",
            {
                "source": "doc.txt",
                "description": "source document",
                "tags": ["ops"],
                "metadata": {"kind": "note"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "ingest_document")
        self.assertEqual(result["data"]["ingested"], True)
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "document.ingested")

    def test_ingest_information_action(self):
        tools = self._build_tools_for_perform_action()
        tools.ingest_information = MagicMock(return_value={"ingested": True, "kind": "text"})

        result = tools.perform_action(
            "ingest_information",
            {
                "information_text": "budget reduced",
                "description": "update",
                "tags": ["finance"],
                "metadata": {"source": "meeting"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "ingest_information")
        self.assertEqual(result["data"]["ingested"], True)
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "information.ingested")

    def test_generate_document_action(self):
        tools = self._build_tools_for_perform_action()

        result = tools.perform_action(
            "generate_document",
            {
                "document_type": "status_report",
                "subject": "Weekly Ops",
                "context": {"owner": "ops"},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "generate_document")
        self.assertIn("content", result["data"])
        self.assertIn("Weekly Ops", result["data"]["content"])
        self.assertEqual(len(tools.event_bus.events), 1)
        self.assertEqual(tools.event_bus.events[0]["event_name"], "document.generated")

    def test_normalize_solf_identifier(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)
        self.assertEqual(tools._normalize_solf_identifier("['vat_no']"), "vat_no")
        self.assertEqual(tools._normalize_solf_identifier('["status"]'), "status")
        self.assertEqual(tools._normalize_solf_identifier("name"), "name")

    def test_execute_sql_plan_returns_structured_error(self):
        tools = IDMSInteractionTools.__new__(IDMSInteractionTools)

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return _Cursor()

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _sql, _params):
                raise RuntimeError("forced SQL failure")

        tools.get_connection = lambda: _Conn()

        result = tools._execute_sql_plan(
            {
                "operation": "update",
                "table": "project_tasks",
                "where": {"['id']": 1},
                "values": {"['status']": "done"},
                "returning": ["id"],
            }
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["operation"], "update")
        self.assertIn("SQL update failed", result["message"])

    def test_resolve_accounting_booking_company_prefers_explicit_company(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "legal_entity_ref": "Acme AG",
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        resolved = domain_function.resolve_accounting_booking_company(payload)
        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")
        self.assertEqual(attrs.get("employer_ref"), "Acme AG")

    def test_resolve_accounting_booking_company_from_person_ref(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_company_for_person", return_value="Acme AG"):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")
        self.assertEqual(attrs.get("employee_ref"), "Alice Example")

    def test_resolve_accounting_booking_company_ignores_entity_id_placeholder_ref(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "legal_entity_ref": "e1",
                "payer_ref": "Alice Example",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 1000,
                        "direction": "credit",
                        "amount_source_currency": "10.00",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "10.00",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_company_for_person", return_value="Aphotonix GmbH"):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")

    def test_resolve_accounting_booking_company_resolves_doc_local_entity_tokens(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "doc_id": 14,
                "legal_entity_ref": "e1",
                "payer_ref": "e2",
                "ledger_lines": [
                    {
                        "account_number": 6400,
                        "direction": "debit",
                        "amount_source_currency": "253.61",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "253.61",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "274.15",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "274.15",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        with patch.object(domain_function, "_resolve_doc_local_entity_name", return_value="Yeung Pang"), patch.object(
            domain_function,
            "_resolve_company_for_person",
            return_value="Seveco AG",
        ):
            resolved = domain_function.resolve_accounting_booking_company(payload)

        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")
        self.assertEqual(attrs.get("payer_ref"), "e2")
        self.assertEqual(attrs.get("person_ref"), "Yeung Pang")

    def test_resolve_accounting_booking_company_from_invoice_recipient_field(self):
        payload = {
            "class_name": "accounting_transaction",
            "attributes": {
                "bill_to": "APHOTONIX GmbH",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        resolved = domain_function.resolve_accounting_booking_company(payload)
        attrs = resolved["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "APHOTONIX GmbH")
        self.assertEqual(attrs.get("company_ref"), "APHOTONIX GmbH")

    def test_infer_invoice_party_role_distinguishes_vendor_and_customer_flow(self):
        vendor_bill = {
            "bill_to": "Aphotonix GmbH",
            "issuer_name": "Northwind Supplies AG",
            "invoice_total": "100.00",
            "vat_amount": "7.70",
        }
        sales_invoice = {
            "bill_to": "Client AG",
            "issuer_name": "Aphotonix GmbH",
            "invoice_total": "100.00",
            "vat_amount": "7.70",
        }

        self.assertEqual(
            domain_function._infer_invoice_party_role(vendor_bill, company_hint="Aphotonix GmbH"),
            "vendor_invoice",
        )
        self.assertEqual(
            domain_function._infer_invoice_party_role(sales_invoice, company_hint="Aphotonix GmbH"),
            "sales_invoice",
        )

    def test_derive_summary_ledger_lines_uses_role_specific_posting_shape(self):
        vendor_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "107.70",
                "vat_amount": "7.70",
                "currency": "CHF",
            },
            "invoice",
            invoice_party_role="vendor_invoice",
        )
        sales_lines = domain_function._derive_summary_ledger_lines_from_amounts(
            {
                "invoice_total": "107.70",
                "vat_amount": "7.70",
                "currency": "CHF",
            },
            "invoice",
            invoice_party_role="sales_invoice",
        )

        self.assertEqual(vendor_lines[0]["account_number"], 4200)
        self.assertEqual(vendor_lines[-1]["account_number"], 2000)
        self.assertEqual(sales_lines[0]["account_number"], 1100)
        self.assertEqual(sales_lines[1]["account_number"], 3000)
        self.assertEqual(sales_lines[-1]["account_number"], 2200)

    def test_db_accounting_ingest_enriches_company_before_upsert(self):
        payload = {
            "class_name": "accounting_transaction",
            "entity_name": "posting:invoice-112479923",
            "attributes": {
                "bill_to": "APHOTONIX GmbH",
                "ledger_lines": [
                    {
                        "account_number": 6500,
                        "direction": "debit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                    {
                        "account_number": 2000,
                        "direction": "credit",
                        "amount_source_currency": "706.20",
                        "source_currency": "CHF",
                        "exchange_rate_to_chf": "1",
                        "amount_chf": "706.20",
                        "swiss_vat_code": "NONE",
                    },
                ],
            },
        }

        object_row = {"object_id": 123, "object_name": "posting:invoice-112479923", "class_name": "accounting_transaction"}
        domain_result = {"ok": True, "validation": {"valid": True}, "transaction_id": "tx-1", "ledger_lines_written": 2}

        with patch.object(domain_function.solf_function, "db_ingest", return_value=object_row), patch.object(
            domain_function.domain_db,
            "upsert_transaction_and_lines",
            return_value=domain_result,
        ) as upsert_mock:
            result = domain_function.db_accounting_ingest(payload)

        self.assertTrue(result["accounting_written"])
        upsert_payload = upsert_mock.call_args.kwargs["payload"]
        upsert_attrs = upsert_payload.get("attributes") or {}
        self.assertEqual(upsert_attrs.get("legal_entity_ref"), "APHOTONIX GmbH")

    def test_apply_post_extraction_business_rules_enforces_selected_company_rule(self):
        extracted = {
            "document": {"doc_type": "invoice", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice:apho-1",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "706.20",
                        "currency": "CHF",
                    },
                }
            ],
        }
        selected_rules = [
            {
                "rule_id": 101,
                "rule_name": "aphotonix_booking",
                "rule_text": "Invoices should be booked for the company Aphotonix GmbH.",
                "structured_rule": {},
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        entity_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(entity_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(entity_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_matches_receipt_scope_within_email_container(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "aphotonix_booking",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": [],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "Aphotonix GmbH",
                                "company_ref": "Aphotonix GmbH",
                            },
                            "overwrite_existing": False,
                            "scope": {
                                "document_types": ["invoice", "bill", "receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(source_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertGreaterEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_matches_receipt_scope_for_generic_ticket_entity(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "fare_type": "Reduced fare 1/2",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "aphotonix_booking",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": [],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "Aphotonix GmbH",
                                "company_ref": "Aphotonix GmbH",
                            },
                            "overwrite_existing": False,
                            "scope": {
                                "document_types": ["invoice", "bill", "receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        self.assertEqual(source_attrs.get("company_ref"), "Aphotonix GmbH")
        self.assertGreaterEqual(int(applied.get("applied_count") or 0), 1)

    def test_apply_post_extraction_business_rules_resolves_reference_tokens_to_entity_names(self):
        extracted = {
            "document": {
                "doc_type": "email",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket 1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                    },
                },
                {
                    "entity_id": "e6",
                    "entity_name": "Seveco AG",
                    "class_name": "company",
                    "attributes": {},
                },
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 1,
                "rule_name": "generic_booking_rule",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": ["receipt"],
                            "conditions": [],
                            "set_attributes": {
                                "legal_entity_ref": "e6",
                                "company_ref": "e6",
                            },
                            "overwrite_existing": True,
                            "scope": {
                                "document_types": ["receipt"],
                                "countries": [],
                                "entity_classes": [],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "email", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")
        integrity = applied.get("rule_integrity") if isinstance(applied.get("rule_integrity"), dict) else {}
        self.assertEqual(int(integrity.get("violation_count") or 0), 0)

    def test_apply_post_extraction_business_rules_flags_selected_rule_not_applied(self):
        extracted = {
            "document": {
                "doc_type": "invoice",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Invoice A",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "100.00",
                        "currency": "CHF",
                    },
                }
            ],
            "relationships": [],
        }
        selected_rules = [
            {
                "rule_id": 77,
                "rule_name": "employment_only_rule",
                "structured_rule": {
                    "application_mode": "selected_only",
                    "post_extraction_directives": [
                        {
                            "target_scope": "entity",
                            "target_entity_classes": ["employment_contract"],
                            "conditions": [],
                            "set_attributes": {
                                "department_ref": "FIN",
                            },
                            "overwrite_existing": True,
                            "scope": {
                                "document_types": [],
                                "countries": [],
                                "entity_classes": ["employment_contract"],
                                "operations": [],
                            },
                        }
                    ],
                },
            }
        ]

        applied = business_rules.apply_post_extraction_business_rules(
            extracted,
            context={"country": "switzerland", "document_type": "invoice", "operation": "ingest"},
            selected_rules=selected_rules,
            include_inferred=False,
        )

        integrity = applied.get("rule_integrity") if isinstance(applied.get("rule_integrity"), dict) else {}
        self.assertEqual(int(integrity.get("violation_count") or 0), 1)
        violations = integrity.get("violations") if isinstance(integrity.get("violations"), list) else []
        self.assertEqual(str((violations[0] if violations else {}).get("type") or ""), "selected_rule_not_applied")

    def test_inferred_receipt_rule_sets_booking_account_and_particulars(self):
        extracted = {
            "document": {"doc_type": "receipt", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "receipt:sbb-1",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "42.50",
                        "currency": "CHF",
                        "issuer_name": "SBB CFF FFS",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        inferred_rule = {
            "rule_id": 202,
            "rule_name": "sbb_travel_booking",
            "rule_text": "In general, all receipts from SBB should be booked to travelling expenses account and particulars are train or bus.",
            "structured_rule": {},
        }

        with patch.object(business_rules, "list_business_rules", return_value=[inferred_rule]):
            applied = business_rules.apply_post_extraction_business_rules(
                extracted,
                context={"country": "switzerland", "document_type": "receipt", "operation": "ingest"},
                selected_rules=[],
                include_inferred=True,
            )

        transformed = applied["extracted"]
        source_attrs = transformed["entities"][0]["attributes"]
        self.assertEqual(source_attrs.get("booking_debit_account_name"), "travelling expenses")
        self.assertEqual(source_attrs.get("booking_particulars"), "train or bus")

        class_defs = {"accounting_transaction": {"class_name": "accounting_transaction"}}
        enriched = domain_function.apply_document_action_policy({"document_type": "receipt"}, transformed, class_defs)
        accounting_entities = [
            item for item in (enriched.get("entities") or [])
            if isinstance(item, dict) and str(item.get("class_name") or "") == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        accounting_attrs = accounting_entities[0].get("attributes") if isinstance(accounting_entities[0].get("attributes"), dict) else {}
        ledger_lines = accounting_attrs.get("ledger_lines") if isinstance(accounting_attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)
        self.assertEqual(ledger_lines[0].get("line_description"), "train or bus")

    def test_inferred_rule_without_general_wording_is_skipped(self):
        extracted = {
            "document": {"doc_type": "receipt", "country": "switzerland", "metadata": {}},
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "receipt:sbb-2",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "42.50",
                        "currency": "CHF",
                        "issuer_name": "SBB CFF FFS",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }
        inferred_rule = {
            "rule_id": 203,
            "rule_name": "sbb_travel_booking",
            "rule_text": "All receipts from SBB should be booked to travelling expenses account and particulars are train or bus.",
            "structured_rule": {},
        }

        with patch.object(business_rules, "list_business_rules", return_value=[inferred_rule]):
            applied = business_rules.apply_post_extraction_business_rules(
                extracted,
                context={"country": "switzerland", "document_type": "receipt", "operation": "ingest"},
                selected_rules=[],
                include_inferred=True,
            )

        source_attrs = applied["extracted"]["entities"][0]["attributes"]
        self.assertNotIn("booking_debit_account_name", source_attrs)
        self.assertNotIn("booking_particulars", source_attrs)
        self.assertEqual(int(applied.get("applied_count") or 0), 0)

    def test_derive_accounting_transaction_for_email_with_paid_ticket_entity(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-1",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "SBB ticket confirmation",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "ticket:sbb-1",
                    "class_name": "ticket",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "train ticket",
                        "legal_entity_ref": "Aphotonix GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        self.assertEqual(attrs.get("legal_entity_ref"), "Aphotonix GmbH")
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_with_invoice_entity(self):
        payload = {
            "document": {
                "doc_key": "invoice-mail-1",
                "doc_path": "invoice-email.pdf",
                "doc_theme": "Supplier invoice by email",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "invoice:mail-1",
                    "class_name": "invoice",
                    "attributes": {
                        "gross_amount": "706.20",
                        "currency": "CHF",
                        "bill_to": "APHOTONIX GmbH",
                    },
                    "confidence": 0.98,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertEqual(ledger_lines[0].get("account_number"), 4200)

    def test_derive_accounting_transaction_for_email_prefers_receipt_with_amount(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-2",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "SBB ticket confirmation",
                "doc_date": "2025-12-18",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket Header",
                    "class_name": "receipt",
                    "attributes": {
                        "receipt_no": "952462418416",
                        "payment_status": "Paid",
                        "currency": "CHF",
                    },
                    "confidence": 0.92,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Ticket Amount",
                    "class_name": "receipt",
                    "attributes": {
                        "gross_amount": "26.50",
                        "currency": "CHF",
                        "booking_debit_account_name": "travelling expenses",
                        "booking_particulars": "train ticket",
                    },
                    "confidence": 0.97,
                },
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_with_generic_ticket_entity_class(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-3",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {},
            },
            "entities": [
                {
                    "entity_id": "e5",
                    "entity_name": "Ticket 1",
                    "class_name": "entity",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "fare_type": "Reduced fare 1/2",
                        "amount": "26.50",
                        "currency": "CHF",
                        "payment_status": "Paid",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_for_email_uses_structured_table_chf_fallback(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-4",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {
                    "processing_rule_names": ["aphotonix_booking"],
                    "user_metadata": {
                        "structured_tables": {
                            "tables": [
                                {
                                    "rows": [
                                        ["**CHF 26.50**", "Article no.:"]
                                    ]
                                }
                            ]
                        }
                    }
                },
            },
            "entities": [
                {
                    "entity_id": "e5",
                    "entity_name": "Ticket 1",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "payment_status": "Paid",
                    },
                    "confidence": 0.95,
                }
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        ledger_lines = attrs.get("ledger_lines") if isinstance(attrs.get("ledger_lines"), list) else []
        self.assertTrue(ledger_lines)
        self.assertEqual(ledger_lines[0].get("account_number"), 6400)

    def test_derive_accounting_transaction_propagates_reference_hints_from_other_entities(self):
        payload = {
            "document": {
                "doc_key": "ticket-mail-5",
                "doc_path": "ticket-email.pdf",
                "doc_theme": "ticket_purchase_confirmation",
                "doc_date": "2025-12-31",
                "metadata": {
                    "post_extraction_rule_applications": [
                        {
                            "rule_name": "generic_company_rule",
                            "entity_name": "Ticket Context",
                            "entity_class": "receipt",
                            "changed_keys": ["legal_entity_ref", "company_ref"],
                            "selection_mode": "explicit",
                            "target_scope": "entity",
                        }
                    ]
                },
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Ticket Main",
                    "class_name": "document",
                    "attributes": {
                        "ticket_type": "Point-to-point Ticket",
                        "payment_status": "Paid",
                        "amount": "26.50",
                        "currency": "CHF",
                    },
                    "confidence": 0.95,
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Ticket Context",
                    "class_name": "receipt",
                    "attributes": {
                        "legal_entity_ref": "Seveco AG",
                        "company_ref": "Seveco AG",
                    },
                    "confidence": 0.9,
                },
            ],
            "relationships": [],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "email"},
            payload,
            {"accounting_transaction": object()},
        )

        accounting_entities = [
            entity for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        ]
        self.assertEqual(len(accounting_entities), 1)
        attrs = accounting_entities[0].get("attributes") or {}
        self.assertEqual(attrs.get("legal_entity_ref"), "Seveco AG")
        self.assertEqual(attrs.get("company_ref"), "Seveco AG")

    def test_derive_accounting_transaction_copies_person_company_context(self):
        payload = {
            "document": {
                "doc_key": "rcpt-1",
                "doc_path": "receipt.pdf",
                "doc_theme": "Meal receipt",
                "doc_date": "2026-06-05",
            },
            "entities": [
                {
                    "entity_id": "e1",
                    "entity_name": "Lunch Receipt",
                    "class_name": "receipt",
                    "attributes": {
                        "receipt_no": "R-1",
                        "payer_ref": "Alice Example",
                        "currency": "CHF",
                        "gross_amount": "42.00",
                        "ledger_lines": [
                            {
                                "account_number": 6500,
                                "direction": "debit",
                                "amount_source_currency": "42.00",
                                "source_currency": "CHF",
                                "exchange_rate_to_chf": "1",
                                "amount_chf": "42.00",
                                "swiss_vat_code": "NONE",
                            },
                            {
                                "account_number": 1000,
                                "direction": "credit",
                                "amount_source_currency": "42.00",
                                "source_currency": "CHF",
                                "exchange_rate_to_chf": "1",
                                "amount_chf": "42.00",
                                "swiss_vat_code": "NONE",
                            },
                        ],
                    },
                },
                {
                    "entity_id": "e2",
                    "entity_name": "Alice Example",
                    "class_name": "person",
                    "attributes": {"current_employer_ref": "Acme AG"},
                },
                {
                    "entity_id": "e3",
                    "entity_name": "Acme AG",
                    "class_name": "company",
                    "attributes": {},
                },
            ],
            "relationships": [
                {
                    "source_entity_id": "e2",
                    "target_entity_id": "e3",
                    "source_name": "Alice Example",
                    "target_name": "Acme AG",
                    "relationship_type": "works_for",
                }
            ],
        }

        domain_function._derive_accounting_transaction_entity(
            {"document_type": "receipt"},
            payload,
            {"accounting_transaction": object()},
        )

        derived = next(
            entity
            for entity in payload["entities"]
            if str(entity.get("class_name") or "").strip().lower() == "accounting_transaction"
        )
        attrs = derived.get("attributes") or {}
        self.assertEqual(attrs.get("payer_ref"), "Alice Example")
        self.assertEqual(attrs.get("legal_entity_ref"), "Acme AG")
        self.assertEqual(attrs.get("company_ref"), "Acme AG")


def run_partial_interaction_regression_suite() -> dict:
    """Run partial-interaction regression tests and return suite summary."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestInteractionPartialTypes)
    runner = unittest.TextTestRunner(stream=StringIO(), verbosity=0)
    result = runner.run(suite)

    failures = []
    for case, err in list(result.failures) + list(result.errors):
        failures.append({"name": str(case), "error": str(err)})

    total = result.testsRun
    failed = len(failures)
    passed = total - failed
    return {
        "result": "ok" if failed == 0 else "error",
        "total": total,
        "passed": passed,
        "failed": failed,
        "failures": failures,
        "tests": [{"name": "partial_interaction_types", "success": failed == 0}],
    }


if __name__ == "__main__":
    outcome = run_partial_interaction_regression_suite()
    print(outcome)
    raise SystemExit(0 if outcome["failed"] == 0 else 1)
