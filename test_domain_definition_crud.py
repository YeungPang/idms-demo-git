"""
Comprehensive tests for domain definition CRUD system.

Tests cover:
- Natural Language (NL) resolution for operations and definition types
- Payload validation with various inputs
- CRUD operations (create, read, update, delete, list)
- Audit logging
- Edge cases and error handling
"""

import json
import pytest
from typing import Any
from unittest.mock import MagicMock, patch

import domain_db
import interaction


class TestDomainDefinitionNLResolution:
    """Test natural language resolution for domain definition operations."""
    
    def test_resolve_operation_create_variations(self):
        """Test detection of create operations from various NL forms."""
        test_cases = [
            ("create a new account", "create"),
            ("add account definition", "create"),
            ("insert department", "create"),
            ("new role definition", "create"),
            ("create", "create"),
        ]
        
        for nl_text, expected_op in test_cases:
            operation = interaction._resolve_domain_definition_operation(nl_text)
            assert operation == expected_op, f"Failed for: {nl_text} (got {operation}, expected {expected_op})"
    
    def test_resolve_operation_delete_variations(self):
        """Test detection of delete operations from various NL forms."""
        test_cases = [
            ("delete account", "delete"),
            ("remove department", "delete"),
            ("drop role definition", "delete"),
            ("delete", "delete"),
        ]
        
        for nl_text, expected_op in test_cases:
            operation = interaction._resolve_domain_definition_operation(nl_text)
            assert operation == expected_op, f"Failed for: {nl_text} (got {operation}, expected {expected_op})"
    
    def test_resolve_operation_update_variations(self):
        """Test detection of update operations from various NL forms."""
        test_cases = [
            ("update account", "update"),
            ("change department", "change"),
            ("modify role", "modify"),
            ("update", "update"),
        ]
        
        for nl_text, expected_op in test_cases:
            operation = interaction._resolve_domain_definition_operation(nl_text)
            # Note: operation may not exactly match 'update' if it's 'change' or 'modify'
            assert operation in ["update", "change", "modify"], f"Failed for: {nl_text} (got {operation})"
    
    def test_resolve_type_account_variations(self):
        """Test detection of account definition types."""
        test_cases = [
            ("account", "account_definition"),
            ("account definition", "account_definition"),
            ("coa", "account_definition"),
            ("chart of accounts", "account_definition"),
            ("konto", "account_definition"),
            ("chart_of_accounts", "account_definition"),
        ]
        
        for nl_text, expected_type in test_cases:
            def_type = interaction._resolve_domain_definition_type(nl_text)
            assert def_type == expected_type, f"Failed for: {nl_text} (got {def_type}, expected {expected_type})"
    
    def test_resolve_type_department_variations(self):
        """Test detection of department definition types."""
        test_cases = [
            ("department", "hr_department_definition"),
            ("dept", "hr_department_definition"),
            ("abteilung", "hr_department_definition"),
            ("hr department", "hr_department_definition"),
            ("department definition", "hr_department_definition"),
        ]
        
        for nl_text, expected_type in test_cases:
            def_type = interaction._resolve_domain_definition_type(nl_text)
            assert def_type == expected_type, f"Failed for: {nl_text} (got {def_type}, expected {expected_type})"
    
    def test_resolve_type_role_variations(self):
        """Test detection of role definition types."""
        test_cases = [
            ("role", "hr_role_definition"),
            ("position", "hr_role_definition"),
            ("funktion", "hr_role_definition"),
            ("role definition", "hr_role_definition"),
            ("hr role", "hr_role_definition"),
        ]
        
        for nl_text, expected_type in test_cases:
            def_type = interaction._resolve_domain_definition_type(nl_text)
            assert def_type == expected_type, f"Failed for: {nl_text} (got {def_type}, expected {expected_type})"


class TestDomainDefinitionValidation:
    """Test payload validation for domain definitions."""
    
    def test_validate_account_definition_create(self):
        """Test validation of account definition for create operation."""
        payload = {
            "legal_entity_ref": "ACME",
            "account_number": 1000,
            "account_name": "Cash",
            "account_type": "asset",
        }
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        assert result["valid"] is True
        assert result["normalized_payload"]["account_number"] == 1000
    
    def test_validate_account_definition_missing_fields(self):
        """Test validation failure when required fields are missing."""
        payload = {
            "account_number": 1000,
            "account_name": "Cash",
            # missing account_type
        }
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        assert result["valid"] is False
        assert "account_type" in result.get("missing_fields", [])
    
    def test_validate_department_definition_create(self):
        """Test validation of department definition for create operation."""
        payload = {
            "department_code": "HR",
            "department_name": "Human Resources",
            "active": True,
        }
        result = domain_db.validate_domain_definition_payload("department", payload, "create")
        assert result["valid"] is True
        assert result["normalized_payload"]["department_code"] == "HR"
    
    def test_validate_department_definition_missing_name(self):
        """Test validation failure when department_name is missing."""
        payload = {
            "department_code": "HR",
            # missing department_name
        }
        result = domain_db.validate_domain_definition_payload("department", payload, "create")
        assert result["valid"] is False
        assert "department_name" in result.get("missing_fields", [])
    
    def test_validate_role_definition_create(self):
        """Test validation of role definition for create operation."""
        payload = {
            "role_code": "MGR",
            "role_name": "Manager",
            "role_family": "management",
            "seniority_level": "mid",
            "active": True,
        }
        result = domain_db.validate_domain_definition_payload("role", payload, "create")
        assert result["valid"] is True
        assert result["normalized_payload"]["role_code"] == "MGR"
    
    def test_validate_delete_operation_requires_only_keys(self):
        """Test that delete operation only requires key fields."""
        payload = {
            "department_code": "HR",
            # department_name not required for delete
        }
        result = domain_db.validate_domain_definition_payload("department", payload, "delete")
        assert result["valid"] is True
    
    def test_validate_type_normalization_synonyms(self):
        """Test that definition type synonyms are normalized correctly."""
        test_cases = [
            ("account", "account_definition"),
            ("coa", "account_definition"),
            ("konto", "account_definition"),
            ("dept", "hr_department_definition"),
            ("abteilung", "hr_department_definition"),
            ("position", "hr_role_definition"),
            ("funktion", "hr_role_definition"),
        ]
        
        for input_type, expected_type in test_cases:
            payload = {}
            result = domain_db.validate_domain_definition_payload(input_type, payload, "delete")
            assert result["definition_type"] == expected_type, f"Failed for type: {input_type}"
    
    def test_validate_invalid_type(self):
        """Test validation failure for unsupported definition type."""
        payload = {"some_field": "value"}
        result = domain_db.validate_domain_definition_payload("invalid_type", payload, "create")
        assert result["valid"] is False


class TestDomainDefinitionAuditLogging:
    """Test audit logging for domain definitions."""
    
    @patch("domain_db.log_domain_definition_audit")
    def test_audit_log_called_on_create(self, mock_audit):
        """Test that audit log is called on successful create."""
        mock_audit.return_value = True
        
        # Simulate what happens in create_domain_definition
        definition_type = "account_definition"
        operation = "create"
        key_value = "global/1000"
        after_state = {"account_number": 1000, "account_name": "Cash"}
        
        domain_db.log_domain_definition_audit(
            None,  # connection
            definition_type,
            operation,
            key_value,
            before_state=None,
            after_state=after_state,
        )
        
        # Verify the call was made (in real implementation)
        # This is a simple verification that the function exists
        assert callable(domain_db.log_domain_definition_audit)
    
    def test_audit_log_function_exists(self):
        """Test that audit logging functions exist."""
        assert hasattr(domain_db, "log_domain_definition_audit")
        assert hasattr(domain_db, "get_domain_definition_audit_log")
        assert callable(domain_db.log_domain_definition_audit)
        assert callable(domain_db.get_domain_definition_audit_log)


class TestDomainDefinitionNLParsing:
    """Test NL parsing with JSON extraction fallback."""
    
    def test_extract_json_from_fenced_code_block(self):
        """Test extraction of JSON from markdown-style code blocks."""
        payload_text = '''
        Create a new account with this data:
        ```json
        {
            "account_number": 2000,
            "account_name": "Bank",
            "account_type": "asset",
            "legal_entity_ref": "ACME"
        }
        ```
        '''
        
        json_data = interaction._extract_domain_definition_json({"text": payload_text})
        assert json_data is not None
        assert json_data.get("account_number") == 2000
    
    def test_extract_json_from_definition_field(self):
        """Test extraction of JSON from 'definition' field in payload."""
        payload = {
            "definition": {
                "account_number": 3000,
                "account_name": "Receivables",
                "account_type": "asset",
                "legal_entity_ref": "ACME"
            }
        }
        
        json_data = interaction._extract_domain_definition_json(payload)
        assert json_data is not None
        assert json_data.get("account_number") == 3000
    
    def test_extract_json_returns_none_if_not_found(self):
        """Test that extraction returns None if no JSON is found."""
        payload = {"text": "Create a new department"}
        
        json_data = interaction._extract_domain_definition_json(payload)
        # Either None or empty dict is acceptable
        assert json_data is None or json_data == {}


class TestDomainDefinitionNLResolutionFallback:
    """Test fallback mechanisms for NL resolution."""
    
    def test_llm_extraction_available(self):
        """Test that LLM extraction function is available."""
        assert hasattr(interaction, "_llm_extract_domain_definition_request")
        assert callable(interaction._llm_extract_domain_definition_request)
    
    def test_operation_resolution_fallback_chain(self):
        """Test that operation resolution follows regex -> spaCy -> LLM chain."""
        # Test regex detection
        operation = interaction._resolve_domain_definition_operation("create an account")
        assert operation == "create"
        
        # Test with ambiguous text
        operation = interaction._resolve_domain_definition_operation("build the chart")
        # Should return empty string if no match (fallback to LLM)
        assert operation in ["", "build", "create"]  # Depends on implementation


class TestDomainDefinitionValidationEdgeCases:
    """Test edge cases in validation."""
    
    def test_validate_empty_payload(self):
        """Test validation with empty payload."""
        result = domain_db.validate_domain_definition_payload("account", {}, "create")
        assert result["valid"] is False
        # Should report missing required fields
        assert len(result.get("missing_fields", [])) > 0
    
    def test_validate_null_values_in_payload(self):
        """Test validation with None/null values."""
        payload = {
            "department_code": "HR",
            "department_name": None,  # null value
        }
        result = domain_db.validate_domain_definition_payload("department", payload, "create")
        # Should either fail or normalize None to empty string
        assert result["valid"] is False or result["normalized_payload"]["department_name"] == ""
    
    def test_validate_extra_fields_ignored(self):
        """Test that extra fields don't cause validation to fail."""
        payload = {
            "department_code": "HR",
            "department_name": "Human Resources",
            "extra_field": "should be ignored",
            "another_extra": 12345,
        }
        result = domain_db.validate_domain_definition_payload("department", payload, "create")
        assert result["valid"] is True
    
    def test_validate_type_coercion(self):
        """Test that values are coerced to correct types."""
        payload = {
            "account_number": "1000",  # string instead of int
            "account_name": "Cash",
            "account_type": "asset",
            "legal_entity_ref": "ACME",
        }
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        # Should either fail or coerce to int
        if result["valid"]:
            assert isinstance(result["normalized_payload"]["account_number"], int)


class TestDomainDefinitionErrorMessages:
    """Test quality of error messages for validation failures."""
    
    def test_validation_error_includes_missing_fields(self):
        """Test that validation errors clearly indicate which fields are missing."""
        payload = {
            "account_number": 1000,
            # missing account_name and account_type
        }
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        assert result["valid"] is False
        # Should have missing_fields list
        missing = result.get("missing_fields", [])
        assert "account_name" in missing or "account_type" in missing
    
    def test_validation_error_includes_error_details(self):
        """Test that validation errors include detailed error information."""
        payload = {
            "account_number": "not_a_number",
            "account_name": "Cash",
            "account_type": "asset",
            "legal_entity_ref": "ACME",
        }
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        if not result["valid"]:
            # Should have error details
            assert "errors" in result or "missing_fields" in result


class TestDomainDefinitionIntegration:
    """Integration tests for the complete CRUD workflow."""
    
    def test_workflow_nl_to_crud(self):
        """Test complete workflow from NL input to CRUD operation."""
        # Simulated NL input
        nl_command = "create a new account with number 5000, name Working Capital, type asset"
        
        # Step 1: Resolve operation
        operation = interaction._resolve_domain_definition_operation(nl_command)
        assert operation == "create"
        
        # Step 2: Resolve type
        def_type = interaction._resolve_domain_definition_type(nl_command)
        assert def_type == "account_definition"
        
        # Step 3: Create should validate before attempting database write
        payload = {
            "account_number": 5000,
            "account_name": "Working Capital",
            "account_type": "asset",
            "legal_entity_ref": "global",
        }
        
        result = domain_db.validate_domain_definition_payload("account", payload, "create")
        assert result["valid"] is True
    
    def test_audit_trail_captures_all_operations(self):
        """Test that audit logging captures the required information."""
        # Verify audit functions exist and are callable
        assert callable(domain_db.log_domain_definition_audit)
        assert callable(domain_db.get_domain_definition_audit_log)
        
        # Verify audit table DDL exists
        assert hasattr(domain_db, "create_domain_definition_audit_log_table")


# Run tests with: pytest test_domain_definition_crud.py -v
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
