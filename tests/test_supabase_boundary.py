import unittest

from app.database import SUPABASE_DATA_TABLES, get_supabase_pool
from app.routers.private_files import _safe_path


class SupabaseBoundaryTests(unittest.TestCase):
    def test_remote_data_allowlist_is_exact(self) -> None:
        self.assertEqual(SUPABASE_DATA_TABLES, frozenset({"supervision", "planillas"}))

    def test_forbidden_remote_table_is_rejected_before_pool_access(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "allowlist"):
            get_supabase_pool("profiles")

    def test_private_file_path_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(Exception, "Ruta de archivo invalida"):
            _safe_path("profile-signature-vault", "../secreto.txt")


if __name__ == "__main__":
    unittest.main()
