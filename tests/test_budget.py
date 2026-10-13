from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from duet.budget import Budget, BudgetError


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "budget.sqlite3"

    def test_reserve_settle_and_restart_preserve_totals(self):
        budget = Budget(self.path, "1", 10)
        reservation = budget.reserve(100_000, 10_000, "2", "10")
        self.assertEqual(Decimal(budget.snapshot()["reserved_usd"]), Decimal("0.3"))
        budget.settle(reservation, 50_000, 5_000)
        snapshot = Budget(self.path, "1", 10).snapshot()
        self.assertEqual(Decimal(snapshot["spent_usd"]), Decimal("0.15"))
        self.assertEqual(Decimal(snapshot["reserved_usd"]), 0)
        self.assertEqual(snapshot["calls"], 1)

    def test_uncertain_reservations_survive_restart(self):
        Budget(self.path, "0.3", 10).reserve(100_000, 10_000, "2", "10")
        restarted = Budget(self.path, "0.3", 10)
        with self.assertRaises(BudgetError):
            restarted.reserve(1, 1, "2", "10")
        self.assertEqual(Decimal(restarted.snapshot()["committed_usd"]), Decimal("0.3"))

    def test_max_calls_is_cumulative_even_when_cost_is_zero(self):
        budget = Budget(self.path, "0", 1)
        reservation = budget.reserve(1, 1, "0", "0")
        budget.settle(reservation, 0, 0)
        with self.assertRaises(BudgetError):
            Budget(self.path, "100", 100).reserve(1, 1, "0", "0")
        self.assertEqual(budget.snapshot()["calls"], 1)

    def test_config_cannot_reset_totals_or_implicitly_raise_caps(self):
        budget = Budget(self.path, "1", 10)
        budget.reserve(100_000, 1, "2", "0")
        reopened = Budget(self.path, "100", 100)
        self.assertEqual(Decimal(reopened.snapshot()["max_usd"]), 1)
        self.assertEqual(reopened.snapshot()["max_calls"], 10)
        Budget(self.path, "0.2", 2)
        self.assertEqual(Decimal(budget.snapshot()["max_usd"]), Decimal("0.2"))
        with self.assertRaises(BudgetError):
            reopened.reserve(1, 1, "1", "1")

    def test_malformed_usage_keeps_full_reservation(self):
        budget = Budget(self.path, "1", 10)
        reservation = budget.reserve(100, 100, "2", "10")
        before = budget.snapshot()
        for invalid in [-1, True, 1.1, "100", float("inf"), None]:
            with self.assertRaises(BudgetError):
                budget.settle(reservation, invalid, 1)
            self.assertEqual(budget.snapshot(), before)

    def test_overrun_is_recorded_and_blocks_future_calls(self):
        budget = Budget(self.path, "1", 10)
        reservation = budget.reserve(10, 10, "1", "1")
        with self.assertRaises(BudgetError):
            budget.settle(reservation, 11, 10)
        restarted = Budget(self.path, "100", 100)
        self.assertTrue(restarted.snapshot()["blocked"])
        self.assertEqual(Decimal(restarted.snapshot()["spent_usd"]), Decimal("0.000021"))
        with self.assertRaises(BudgetError):
            restarted.reserve(1, 1, "0", "0")
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT status,input_tokens,output_tokens FROM reservations").fetchone()
        db.close()
        self.assertEqual(row, ("overflow", 11, 10))

    def test_settlement_is_idempotent_but_cannot_be_rewritten(self):
        budget = Budget(self.path, "1", 10)
        reservation = budget.reserve(100, 100, "2", "10")
        budget.settle(reservation, 20, 30)
        snapshot = budget.snapshot()
        budget.settle(reservation, 20, 30)
        self.assertEqual(budget.snapshot(), snapshot)
        with self.assertRaises(BudgetError):
            budget.settle(reservation, 0, 0)
        with self.assertRaises(BudgetError):
            budget.settle("missing", 0, 0)

    def test_concurrent_connections_cannot_overspend(self):
        instances = [Budget(self.path, "0.1", 100) for _ in range(20)]
        def reserve(budget):
            try:
                return budget.reserve(50_000, 1, "1", "0")
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            reservations = list(pool.map(reserve, instances))
        self.assertEqual(sum(r is not None for r in reservations), 2)
        self.assertEqual(Decimal(instances[0].snapshot()["committed_usd"]), Decimal("0.1"))

    def test_concurrent_connections_respect_call_cap(self):
        instances = [Budget(self.path, "1", 3) for _ in range(10)]
        def reserve(budget):
            try:
                return budget.reserve(1, 1, "0", "0")
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(reserve, instances))
        self.assertEqual(sum(r is not None for r in results), 3)

    def test_decimal_cost_has_no_binary_float_rounding(self):
        budget = Budget(self.path, "0.000000000001", 10)
        reservation = budget.reserve(1, 1, "0.000001", "0")
        self.assertEqual(Decimal(budget.snapshot()["reserved_usd"]), Decimal("0.000000000001"))
        budget.settle(reservation, 1, 0)
        with self.assertRaises(BudgetError):
            budget.reserve(1, 1, "0.000001", "0")

    def test_invalid_amounts_and_counts_are_rejected(self):
        for amount in ["NaN", "Infinity", "-1", "0.0000000000001", "1e100", 1.0]:
            with self.subTest(amount=amount), self.assertRaises(BudgetError):
                Budget(self.path, amount, 10)
        budget = Budget(self.path, "1", 10)
        for count in [-1, True, 1.5, 10**13]:
            with self.assertRaises(BudgetError):
                budget.reserve(count, 10, "1", "1")
        self.assertEqual(budget.snapshot()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
