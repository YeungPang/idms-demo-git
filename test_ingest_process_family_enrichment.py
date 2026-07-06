import unittest

from ingest import enrich_process_family_relationships_from_markdown


class TestIngestProcessFamilyEnrichment(unittest.TestCase):
    def _base_ingested(self, company_name: str, person_name: str) -> dict:
        return {
            "document_effective_date": "2026-07-04",
            "solf_entities": [
                {
                    "entity_id": "e1",
                    "name": company_name,
                    "class_name": "company",
                    "operation": "ingest",
                    "attributes": {},
                },
                {
                    "entity_id": "e2",
                    "name": person_name,
                    "class_name": "person",
                    "operation": "ingest",
                    "attributes": {},
                },
            ],
            "solf_relationships": [],
        }

    def test_adds_vat_process_contact_relationship_and_email(self):
        ingested = self._base_ingested("Acme GmbH", "Maria Keller")
        markdown = "VAT contact person for Acme GmbH: Maria Keller, email maria.keller@example.com"

        summary = enrich_process_family_relationships_from_markdown(ingested, markdown)

        self.assertEqual(summary.get("relationships_added"), 1)
        self.assertEqual(summary.get("person_emails_added"), 1)
        relationships = ingested.get("solf_relationships") or []
        self.assertEqual(len(relationships), 1)
        self.assertEqual(relationships[0].get("relationship_type"), "has_vat_contact_person")
        self.assertEqual(
            ((ingested.get("solf_entities") or [])[1].get("attributes") or {}).get("email"),
            "maria.keller@example.com",
        )

    def test_adds_license_process_contact_relationship_from_german_text(self):
        ingested = self._base_ingested("Beta AG", "Lukas Meier")
        markdown = "Lizenz Ansprechpartner fuer Beta AG: Lukas Meier, contact lukas.meier@beta.example"

        summary = enrich_process_family_relationships_from_markdown(ingested, markdown)

        self.assertEqual(summary.get("relationships_added"), 1)
        relationships = ingested.get("solf_relationships") or []
        self.assertEqual(relationships[0].get("relationship_type"), "has_license_contact_person")

    def test_does_not_duplicate_existing_process_relationship(self):
        ingested = self._base_ingested("Acme GmbH", "Maria Keller")
        ingested["solf_relationships"] = [
            {
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "source_name": "Acme GmbH",
                "target_name": "Maria Keller",
                "relationship_type": "has_vat_contact_person",
                "relationship_cat": "process",
                "operation": "upsert",
            }
        ]
        markdown = "VAT contact person for Acme GmbH: Maria Keller, email maria.keller@example.com"

        summary = enrich_process_family_relationships_from_markdown(ingested, markdown)

        self.assertEqual(summary.get("relationships_added"), 0)
        relationships = ingested.get("solf_relationships") or []
        self.assertEqual(len(relationships), 1)


if __name__ == "__main__":
    unittest.main()
