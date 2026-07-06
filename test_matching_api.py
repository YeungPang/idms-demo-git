import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from idms_api_server.application import app


class TestMatchingAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_workflow_shortcut_query_resolves_aliases(self):
        query_result = {
            "ok": True,
            "collection": "generic_match_collection",
            "purpose": "entity_resolution",
            "workflow_scope": "workflow:vendor_dedupe_v1",
            "tenant_scope": "tenant:acme",
            "query_text": "Supplier duplicate candidate",
            "top_k": 5,
            "count": 1,
            "items": [{"id": "p1", "score": 0.98, "payload": {"alias": True}}],
        }

        with patch(
            "idms_api_server.routers.matching.business_rules.resolve_workflow_resource_alias",
            side_effect=[
                {
                    "canonical_name": "entity_resolution",
                    "alias_name": "vendor_dedupe_v1::matching_purpose",
                    "resource_type": "purpose",
                },
                {
                    "canonical_name": "workflow:vendor_dedupe_v1",
                    "alias_name": "vendor_dedupe_v1::workflow_scope",
                    "resource_type": "scope",
                },
                {
                    "canonical_name": "generic_match_collection",
                    "alias_name": "vendor_dedupe_v1::matching_collection",
                    "resource_type": "collection",
                },
            ],
        ) as alias_mock:
            with patch("idms_api_server.routers.matching.tx_match_index.is_allowed_purpose", return_value=True):
                with patch("idms_api_server.routers.matching.tx_match_index.query_rows", return_value=query_result) as query_mock:
                    response = self.client.post(
                        "/api/matching/workflow/vendor_dedupe_v1/query",
                        json={
                            "query_text": "Supplier duplicate candidate",
                            "tenant_scope": "tenant:acme",
                            "top_k": 5,
                            "purpose_alias": "vendor_dedupe_v1::matching_purpose",
                            "workflow_scope_alias": "vendor_dedupe_v1::workflow_scope",
                            "collection_alias": "vendor_dedupe_v1::matching_collection",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("collection"), "generic_match_collection")
        query_mock.assert_called_once()
        called_kwargs = query_mock.call_args.kwargs
        self.assertEqual(called_kwargs.get("purpose"), "entity_resolution")
        self.assertEqual(called_kwargs.get("workflow_scope"), "workflow:vendor_dedupe_v1")
        self.assertEqual(called_kwargs.get("collection_name"), "generic_match_collection")
        self.assertEqual(alias_mock.call_count, 3)

    def test_workflow_shortcut_candidates_resolves_aliases(self):
        candidate_result = {
            "ok": True,
            "collection": "generic_match_collection",
            "purpose": "entity_resolution",
            "workflow_scope": "workflow:vendor_dedupe_v1",
            "tenant_scope": "tenant:acme",
            "doc_type": "invoice",
            "limit": 10,
            "cursor": "",
            "next_cursor": "",
            "count": 1,
            "items": [{"id": "p1", "payload": {"alias": True}}],
        }

        with patch(
            "idms_api_server.routers.matching.business_rules.resolve_workflow_resource_alias",
            side_effect=[
                {
                    "canonical_name": "entity_resolution",
                    "alias_name": "vendor_dedupe_v1::matching_purpose",
                    "resource_type": "purpose",
                },
                {
                    "canonical_name": "workflow:vendor_dedupe_v1",
                    "alias_name": "vendor_dedupe_v1::workflow_scope",
                    "resource_type": "scope",
                },
                {
                    "canonical_name": "generic_match_collection",
                    "alias_name": "vendor_dedupe_v1::matching_collection",
                    "resource_type": "collection",
                },
            ],
        ):
            with patch("idms_api_server.routers.matching.tx_match_index.is_allowed_purpose", return_value=True):
                with patch("idms_api_server.routers.matching.tx_match_index.list_candidates", return_value=candidate_result) as candidates_mock:
                    response = self.client.post(
                        "/api/matching/workflow/vendor_dedupe_v1/candidates",
                        json={
                            "tenant_scope": "tenant:acme",
                            "doc_type": "invoice",
                            "limit": 10,
                            "purpose_alias": "vendor_dedupe_v1::matching_purpose",
                            "workflow_scope_alias": "vendor_dedupe_v1::workflow_scope",
                            "collection_alias": "vendor_dedupe_v1::matching_collection",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("collection"), "generic_match_collection")
        candidates_mock.assert_called_once()
        called_kwargs = candidates_mock.call_args.kwargs
        self.assertEqual(called_kwargs.get("purpose"), "entity_resolution")
        self.assertEqual(called_kwargs.get("workflow_scope"), "workflow:vendor_dedupe_v1")
        self.assertEqual(called_kwargs.get("collection_name"), "generic_match_collection")

    def test_resolve_workflow_alias_endpoint(self):
        with patch(
            "idms_api_server.routers.business_rules.business_rules.resolve_workflow_resource_alias",
            return_value={
                "alias_id": 1,
                "workflow_id": 10,
                "workflow_version_id": 11,
                "resource_type": "collection",
                "alias_name": "vendor_dedupe_v1::matching_collection",
                "canonical_name": "generic_match_collection",
                "metadata": {"source": "workflow_registry_sync"},
                "workflow_key": "vendor_dedupe_v1",
                "workflow_name": "Vendor Dedupe V1",
            },
        ):
            response = self.client.post(
                "/api/business-rules/workflow-registry/resolve-alias",
                json={
                    "workflow_key": "vendor_dedupe_v1",
                    "alias_name": "vendor_dedupe_v1::matching_collection",
                    "resource_type": "collection",
                    "workflow_version_id": 11,
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        result = data.get("result") or {}
        self.assertEqual(result.get("canonical_name"), "generic_match_collection")
        self.assertEqual(result.get("workflow_key"), "vendor_dedupe_v1")

    def test_list_workflow_aliases_by_key_endpoint(self):
        with patch(
            "idms_api_server.routers.business_rules.business_rules.get_solf_workflow_registry_entry_by_key",
            return_value={"workflow_id": 10, "workflow_key": "vendor_dedupe_v1", "workflow_name": "Vendor Dedupe V1"},
        ):
            with patch(
                "idms_api_server.routers.business_rules.business_rules.list_workflow_resource_aliases",
                return_value=[
                    {
                        "alias_id": 1,
                        "workflow_id": 10,
                        "workflow_version_id": 11,
                        "resource_type": "purpose",
                        "alias_name": "vendor_dedupe_v1::matching_purpose",
                        "canonical_name": "entity_resolution",
                        "metadata": {},
                        "created_by": "test",
                    }
                ],
            ):
                response = self.client.get("/api/business-rules/workflow-registry/by-key/vendor_dedupe_v1/resource-aliases")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("count"), 1)
        self.assertEqual((data.get("result") or [{}])[0].get("canonical_name"), "entity_resolution")

    def test_workflow_shortcut_query_requires_resolvable_aliases(self):
        with patch(
            "idms_api_server.routers.matching.business_rules.resolve_workflow_resource_alias",
            return_value=None,
        ) as alias_mock:
            response = self.client.post(
                "/api/matching/workflow/vendor_dedupe_v1/query",
                json={
                    "query_text": "Supplier duplicate candidate",
                    "tenant_scope": "tenant:acme",
                    "top_k": 5,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("purpose and workflow_scope are required", response.json().get("detail", ""))
        self.assertGreaterEqual(alias_mock.call_count, 2)

    def test_workflow_shortcut_query_with_explicit_scope_and_purpose(self):
        query_result = {
            "ok": True,
            "collection": "generic_match_collection",
            "purpose": "entity_resolution",
            "workflow_scope": "workflow:vendor_dedupe_v1",
            "tenant_scope": "tenant:acme",
            "query_text": "Supplier duplicate candidate",
            "top_k": 5,
            "count": 1,
            "items": [{"id": "p1", "score": 0.97, "payload": {"explicit": True}}],
        }

        with patch("idms_api_server.routers.matching.tx_match_index.is_allowed_purpose", return_value=True):
            with patch("idms_api_server.routers.matching.tx_match_index.query_rows", return_value=query_result) as query_mock:
                response = self.client.post(
                    "/api/matching/workflow/vendor_dedupe_v1/query",
                    json={
                        "query_text": "Supplier duplicate candidate",
                        "tenant_scope": "tenant:acme",
                        "top_k": 5,
                        "purpose": "entity_resolution",
                        "workflow_scope": "workflow:vendor_dedupe_v1",
                        "collection_name": "generic_match_collection",
                    },
                )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("success"))
        self.assertEqual((data.get("result") or {}).get("purpose"), "entity_resolution")
        query_mock.assert_called_once()
        called_kwargs = query_mock.call_args.kwargs
        self.assertEqual(called_kwargs.get("purpose"), "entity_resolution")
        self.assertEqual(called_kwargs.get("workflow_scope"), "workflow:vendor_dedupe_v1")
        self.assertEqual(called_kwargs.get("collection_name"), "generic_match_collection")

    def test_resolve_workflow_alias_endpoint_returns_404_for_missing_alias(self):
        with patch(
            "idms_api_server.routers.business_rules.business_rules.resolve_workflow_resource_alias",
            return_value=None,
        ):
            response = self.client.post(
                "/api/business-rules/workflow-registry/resolve-alias",
                json={
                    "workflow_key": "vendor_dedupe_v1",
                    "alias_name": "vendor_dedupe_v1::missing_alias",
                    "resource_type": "collection",
                    "workflow_version_id": 11,
                },
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
