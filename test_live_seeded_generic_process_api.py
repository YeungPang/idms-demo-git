from __future__ import annotations

import os
import unittest
from uuid import uuid4

import requests

import object_db


def _cleanup_object(connection, object_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM attribute WHERE src_id = %s AND src_type = 'object'", (int(object_id),))
        cursor.execute(
            "DELETE FROM object_relationship WHERE src_object_id = %s OR tar_object_id = %s",
            (int(object_id), int(object_id)),
        )
        cursor.execute("DELETE FROM object_instance WHERE object_id = %s", (int(object_id),))
    connection.commit()


def _seed_process_family(connection, process_name: str, label: str, seed: str) -> dict:
    client_company_name = f"Seeded {label} Client {seed} GmbH"
    contact_person_name = f"{label} Contact {seed}"
    responsible_company_name = f"Seeded {label} Advisory {seed} AG"
    contact_email = f"{process_name}.contact.{seed}@seeded-brokerage.example"

    client_company = object_db.upsert_object_instance(
        connection,
        client_company_name,
        "company",
        metadata={"seeded_test": True, "seed_kind": "generic_process_api", "process": process_name},
    )
    contact_person = object_db.upsert_object_instance(
        connection,
        contact_person_name,
        "person",
        metadata={"seeded_test": True, "seed_kind": "generic_process_api", "process": process_name},
    )
    responsible_company = object_db.upsert_object_instance(
        connection,
        responsible_company_name,
        "company",
        metadata={"seeded_test": True, "seed_kind": "generic_process_api", "process": process_name},
    )

    object_db.upsert_object_relationship(
        connection,
        relationship_name=f"has_{process_name}_contact_person",
        relationship_cat="process",
        src_object_id=int(client_company["object_id"]),
        tar_object_id=int(contact_person["object_id"]),
        confidence=0.99,
        metadata={"seeded_test": True, "process": process_name},
    )
    object_db.upsert_object_relationship(
        connection,
        relationship_name="employed_by",
        relationship_cat="employment",
        src_object_id=int(contact_person["object_id"]),
        tar_object_id=int(responsible_company["object_id"]),
        confidence=0.99,
        metadata={"seeded_test": True, "process": process_name},
    )
    object_db.upsert_temporal_attribute(
        connection,
        src_id=int(contact_person["object_id"]),
        src_type="object",
        attr_type="email",
        attr_json={"value": contact_email},
        close_previous=False,
    )
    connection.commit()

    return {
        "client_company_name": client_company_name,
        "contact_person_name": contact_person_name,
        "responsible_company_name": responsible_company_name,
        "contact_email": contact_email,
        "object_ids": [
            int(client_company["object_id"]),
            int(contact_person["object_id"]),
            int(responsible_company["object_id"]),
        ],
    }


class TestLiveSeededGenericProcessAPI(unittest.TestCase):
    """Live end-to-end API test using a real seeded non-EORI process dataset."""

    @classmethod
    def setUpClass(cls):
        cls.base_url = os.getenv("IDMS_API_BASE_URL", "http://127.0.0.1:8000")
        cls.connection = object_db.get_connection()
        seed = uuid4().hex[:8]
        cls.customs_family = _seed_process_family(cls.connection, "customs", "Customs", seed)
        cls.vat_family = _seed_process_family(cls.connection, "vat", "VAT", seed)
        cls._seeded_object_ids = [
            *cls.customs_family["object_ids"],
            *cls.vat_family["object_ids"],
        ]

    @classmethod
    def tearDownClass(cls):
        try:
            for object_id in reversed(getattr(cls, "_seeded_object_ids", [])):
                _cleanup_object(cls.connection, object_id)
        finally:
            if getattr(cls, "connection", None) is not None:
                cls.connection.close()

    def _query(self, question: str) -> dict:
        response = requests.post(
            f"{self.base_url.rstrip('/')}/api/query",
            json={"question": question, "max_steps": 4},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("result", {}) if isinstance(payload, dict) else {}

    def test_live_customs_contact_name_and_email_query(self):
        question = (
            "What are the name and email address of the customs contact person for "
            f"{self.customs_family['client_company_name']}?"
        )
        result = self._query(question)

        self.assertEqual(result.get("answer_source"), "sql_exact_composite")
        answer = str(result.get("answer") or "")
        self.assertIn(self.customs_family["contact_person_name"], answer)
        self.assertIn(self.customs_family["contact_email"], answer)

    def test_live_customs_responsible_company_query(self):
        question = (
            "What company is responsible for the customs application for "
            f"{self.customs_family['client_company_name']}?"
        )
        result = self._query(question)

        self.assertEqual(result.get("answer_source"), "sql_exact_composite")
        answer = str(result.get("answer") or "")
        self.assertIn(self.customs_family["responsible_company_name"], answer)
        self.assertIn(self.customs_family["contact_person_name"], answer)
        self.assertIn(self.customs_family["contact_email"], answer)

    def test_live_vat_contact_name_and_email_query(self):
        question = (
            "What are the name and email address of the VAT contact person for "
            f"{self.vat_family['client_company_name']}?"
        )
        result = self._query(question)

        self.assertEqual(result.get("answer_source"), "sql_exact_composite")
        answer = str(result.get("answer") or "")
        self.assertIn(self.vat_family["contact_person_name"], answer)
        self.assertIn(self.vat_family["contact_email"], answer)

    def test_live_vat_responsible_company_query(self):
        question = f"Which company handled the VAT registration for {self.vat_family['client_company_name']}?"
        result = self._query(question)

        self.assertEqual(result.get("answer_source"), "sql_exact_composite")
        answer = str(result.get("answer") or "")
        self.assertIn(self.vat_family["responsible_company_name"], answer)
        self.assertIn(self.vat_family["contact_person_name"], answer)
        self.assertIn(self.vat_family["contact_email"], answer)


if __name__ == "__main__":
    unittest.main()
