import os
import tempfile
import unittest
from pathlib import Path

import backfill_document_source_paths as backfill


class TestBackfillDocumentSourcePaths(unittest.TestCase):
    def test_resolve_original_local_path_prefers_absolute(self):
        resolved = backfill._resolve_original_local_path(
            "gs://bucket/aphotonix.pdf",
            r"C:\\Seveco\\Aphotonix\\Aphotonix_registration.pdf",
            "Aphotonix_registration.pdf",
        )
        self.assertEqual(resolved, r"C:\Seveco\Aphotonix\Aphotonix_registration.pdf")

    def test_resolve_original_local_path_resolves_existing_relative(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            current = Path.cwd()
            try:
                os.chdir(temp_dir)
                rel = Path("Aphotonix_registration.pdf")
                rel.write_text("stub", encoding="utf-8")
                resolved = backfill._resolve_original_local_path(str(rel))
                self.assertTrue(Path(resolved).is_absolute())
                self.assertEqual(Path(resolved), rel.resolve())
            finally:
                os.chdir(current)

    def test_resolve_original_local_path_ignores_uri_only(self):
        resolved = backfill._resolve_original_local_path(
            "gs://bucket/a.pdf",
            "https://example.com/file.pdf",
        )
        self.assertEqual(resolved, "")

    def test_resolve_original_local_path_ignores_markdown_artifacts(self):
        resolved = backfill._resolve_original_local_path(
            r"C:\\Project\\WebTech\\python\\IDMS-Demo\\generated\\markdown\\document-6.md",
            "generated/markdown/preextract-abc.md",
        )
        self.assertEqual(resolved, "")

    def test_resolve_original_local_path_ignores_temp_upload_paths(self):
        resolved = backfill._resolve_original_local_path(
            r"C:\\Users\\pang\\AppData\\Local\\Temp\\idms-upload-zjbg490o.pdf",
            "Aphotonix_registration.pdf",
        )
        self.assertEqual(resolved, "")


if __name__ == "__main__":
    unittest.main()
