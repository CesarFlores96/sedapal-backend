import unittest
from unittest.mock import AsyncMock, patch

from app.repositories.customer_supplies import update_supply_location_by_code


class CustomerSupplyLocationTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "app.repositories.customer_supplies.execute_fetch_all_dict",
        new_callable=AsyncMock,
    )
    async def test_update_by_nis_targets_local_customer_supplies(self, execute_query: AsyncMock):
        execute_query.return_value = [
            {
                "id": "supply-id",
                "supply_code": "5521004",
                "latitude": -12.123,
                "longitude": -77.456,
                "location_source": "gps-planilla",
            }
        ]

        result = await update_supply_location_by_code(
            pool=object(),
            supply_code=" 5521004 ",
            latitude=-12.123,
            longitude=-77.456,
            source="gps-planilla",
        )

        self.assertEqual(result["supply_code"], "5521004")
        _, sql, params = execute_query.await_args.args
        self.assertIn("UPDATE public.customer_supplies", sql)
        self.assertIn("WHERE supply_code = %s", sql)
        self.assertNotIn("geolocation_address =", sql)
        self.assertEqual(params, [-12.123, -77.456, "gps-planilla", "5521004"])


if __name__ == "__main__":
    unittest.main()
