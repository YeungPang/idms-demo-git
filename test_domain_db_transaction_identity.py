import unittest

import domain_db


class TestDomainDbTransactionIdentity(unittest.TestCase):
    def test_build_transaction_id_namespaces_existing_by_doc(self):
        tx_id = domain_db._build_transaction_id(
            {"transaction_id": "7466735"},
            {"doc_id": 14, "entity_id": "e7"},
        )
        self.assertEqual(tx_id, "doc14-7466735")

    def test_build_transaction_id_without_doc_keeps_existing(self):
        tx_id = domain_db._build_transaction_id(
            {"transaction_id": "7466735"},
            {"entity_id": "e7"},
        )
        self.assertEqual(tx_id, "7466735")

    def test_build_transaction_id_fallback_uses_doc_and_entity(self):
        tx_id = domain_db._build_transaction_id(
            {},
            {"doc_id": 14, "entity_id": "e7"},
        )
        self.assertEqual(tx_id, "doc14-e7")


if __name__ == "__main__":
    unittest.main()
