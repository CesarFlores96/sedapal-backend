import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routers.supervision_table import (
    ImportedFilePayload,
    SupervisionImportRequest,
    post_import,
)


class SupervisionImportAssignmentTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, assigned_user_id: int | None) -> SupervisionImportRequest:
        return SupervisionImportRequest(
            assignedUserId=assigned_user_id,
            files=[ImportedFilePayload(content="NUM_OS\n123", fileName="supervision.txt")],
        )

    async def test_mobile_ignores_manipulated_assignment_and_uses_actor(self) -> None:
        importer = AsyncMock(return_value={"totalImported": 1})
        with (
            patch("app.routers.supervision_table.get_pool", return_value=object()),
            patch("app.routers.supervision_table.get_supervision_pool", return_value=object()),
            patch("app.routers.supervision_table.import_supervision_txt_files", importer),
            patch("app.routers.supervision_table.ensure_path_access", new=AsyncMock()),
        ):
            result = await post_import(
                payload=self.payload(999),
                user_role="supervisor",
                user_id="27",
            )

        self.assertEqual(result, {"totalImported": 1})
        self.assertEqual(importer.await_args.kwargs["assigned_user_id"], 27)
        self.assertFalse(importer.await_args.kwargs["allow_reassignment"])

    async def test_admin_can_assign_batch_and_reassign_existing_rows(self) -> None:
        importer = AsyncMock(return_value={"totalImported": 1})
        with (
            patch("app.routers.supervision_table.get_pool", return_value=object()),
            patch("app.routers.supervision_table.get_supervision_pool", return_value=object()),
            patch("app.routers.supervision_table.import_supervision_txt_files", importer),
            patch("app.routers.supervision_table.ensure_path_access", new=AsyncMock()),
        ):
            await post_import(
                payload=self.payload(45),
                user_role="superadmin",
                user_id="1",
            )

        self.assertEqual(importer.await_args.kwargs["assigned_user_id"], 45)
        self.assertTrue(importer.await_args.kwargs["allow_reassignment"])

    async def test_mobile_requires_operational_user_id(self) -> None:
        with (
            patch("app.routers.supervision_table.get_pool", return_value=object()),
            patch("app.routers.supervision_table.ensure_path_access", new=AsyncMock()),
            self.assertRaises(HTTPException) as context,
        ):
            await post_import(payload=self.payload(999), user_role="consultor", user_id=None)

        self.assertEqual(context.exception.status_code, 401)

    async def test_admin_requires_explicit_assignee(self) -> None:
        with (
            patch("app.routers.supervision_table.get_pool", return_value=object()),
            patch("app.routers.supervision_table.ensure_path_access", new=AsyncMock()),
            self.assertRaises(HTTPException) as context,
        ):
            await post_import(payload=self.payload(None), user_role="admin", user_id="1")

        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
