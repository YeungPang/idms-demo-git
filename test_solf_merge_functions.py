import unittest
from unittest.mock import MagicMock, patch

import solf_function
import solf_parser
from solf_interpreter import SOLFInterpreter


class TestSolfMergeFunctions(unittest.TestCase):
    def _build_interpreter(self) -> SOLFInterpreter:
        interpreter = SOLFInterpreter()
        parser_adapter = type("Parser", (object,), {"parse": staticmethod(solf_parser.parse_script)})()
        interpreter.set_parser(parser_adapter)
        interpreter.set_debug(False)
        with open("solf_script.txt", "r", encoding="utf-8") as handle:
            interpreter.load_program_script(handle.read(), clear_existing=True)
        return interpreter

    def test_get_entity_id_accepts_valid_numeric_id(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "get_object_instance_by_id",
            return_value={"object_id": 123, "object_name": "Chung Yeung Pang"},
        ):
            self.assertEqual(solf_function.get_entity_id("123"), 123)

    def test_get_entity_id_resolves_name(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "get_object_instance",
            return_value={"object_id": 456, "object_name": "Chung Yeung Pang"},
        ):
            self.assertEqual(solf_function.get_entity_id("Chung Yeung Pang"), 456)

    def test_get_entity_id_returns_false_for_unknown_entity(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "get_object_instance",
            return_value=None,
        ):
            self.assertFalse(solf_function.get_entity_id("Unknown Person"))

    def test_get_entity_id_resolves_unique_fuzzy_match(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "get_object_instance",
            return_value=None,
        ), patch.object(
            solf_function.object_db,
            "search_objects",
            return_value=[
                {"object_id": 77, "object_name": "Chung Yeung Pang", "similarity": 0.81},
                {"object_id": 12, "object_name": "Kaethi Pang", "similarity": 0.19},
            ],
        ):
            self.assertEqual(solf_function.get_entity_id("Yeung Pang"), 77)

    def test_get_entity_id_returns_false_for_ambiguous_fuzzy_matches(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "get_object_instance",
            return_value=None,
        ), patch.object(
            solf_function.object_db,
            "search_objects",
            return_value=[
                {"object_id": 77, "object_name": "Chung Yeung Pang", "similarity": 0.66},
                {"object_id": 78, "object_name": "Yeung Pang", "similarity": 0.64},
            ],
        ):
            self.assertFalse(solf_function.get_entity_id("Yeung Pang"))

    def test_merge_returns_summary_payload(self):
        fake_conn = MagicMock()
        with patch.object(solf_function.object_db, "get_connection", return_value=fake_conn), patch.object(
            solf_function.object_db,
            "merge_object_instances",
            return_value={"canonical_object_id": 1, "duplicate_object_id": 2},
        ):
            result = solf_function.merge(1, 2)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("status"), "merged")
        self.assertEqual(result.get("canonical_object_id"), 1)
        self.assertEqual(result.get("duplicate_object_id"), 2)

    def test_merge_entities_clause_resolves_and_merges(self):
        interpreter = self._build_interpreter()
        with patch.object(solf_function, "get_entity_id", side_effect=[11, 22]) as mock_get_id, patch.object(
            solf_function,
            "merge",
            return_value={"status": "merged", "canonical_object_id": 11, "duplicate_object_id": 22},
        ) as mock_merge:
            result = interpreter._invoke_clause("merge_entities", ["Yeung Pang", "Chung Yeung Pang"])

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("status"), "merged")
        self.assertEqual(mock_get_id.call_count, 2)
        mock_merge.assert_called_once_with(11, 22)


if __name__ == "__main__":
    unittest.main()
