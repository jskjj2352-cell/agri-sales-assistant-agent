"""库存、报表和并发回归测试；仅使用临时数据库，不调用模型 API。"""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import store
import tools


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(store, "DB_PATH", Path(self.tmp.name) / "store.db")
        self.orders_patch = patch.object(tools, "ORDERS_DIR", Path(self.tmp.name) / "orders")
        self.db_patch.start()
        self.orders_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.orders_patch.stop)
        store.init_db()
        self.pid = "P052"  # 农具，不涉及农药登记校验
        self.initial = self.stock()

    def rows(self, sql, params=()):
        with closing(store.connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def stock(self):
        return self.rows("SELECT stock FROM products WHERE id=?", (self.pid,))[0]["stock"]

    def order(self, qty=1, price=180):
        return tools.create_order([{"product_id": self.pid, "qty": qty, "unit_price": price}])

    def report(self, day=None):
        day = day or date.today()
        with closing(store.connect()) as conn:
            return next(dict(r) for r in store.inventory_report(conn, day, day) if r["id"] == self.pid)

    def test_purchase_sale_void_and_ledger_balance(self):
        purchase_id = store.add_purchase(self.pid, 5, 100, supplier="测试供应商")
        order = self.order(3)
        self.assertTrue(order["ok"])
        self.assertEqual(self.stock(), self.initial + 2)
        self.assertTrue(store.void_order(order["订单号"]))
        self.assertFalse(store.void_order(order["订单号"]))
        moves = self.rows("SELECT * FROM stock_movements WHERE product_id=? ORDER BY id", (self.pid,))
        self.assertEqual([r["kind"] for r in moves], ["期初库存", "采购入库", "销售出库", "作废退回"])
        self.assertEqual(moves[1]["ref_id"], str(purchase_id))
        balance = 0
        for move in moves:
            balance += move["qty"]
            self.assertEqual(balance, move["balance"])
        self.assertEqual(balance, self.stock())

    def test_report_no_join_multiplication_and_void_exclusion(self):
        store.add_purchase(self.pid, 2, 100)
        store.add_purchase(self.pid, 3, 110)
        self.order(2)
        voided = self.order(1)
        self.assertTrue(store.void_order(voided["订单号"]))
        r = self.report()
        self.assertEqual((r["bought"], r["purchase_amount"], r["sold"], r["sales_amount"], r["profit"]),
                         (5, 530, 2, 360, 140))
        tomorrow = self.report(date.today() + timedelta(days=1))
        self.assertEqual(tomorrow["sold"], 0)
        self.assertEqual(tomorrow["bought"], 0)

    def test_missing_cost_remains_unknown_after_later_purchase(self):
        self.order(1)
        store.add_purchase(self.pid, 1, 100)
        self.assertEqual(self.report()["unknown_cost_qty"], 1)
        self.assertEqual(self.report()["profit"], 0)

    def test_duplicate_lines_cannot_oversell_or_mix_prices(self):
        self.assertIn("error", tools.create_order([
            {"product_id": self.pid, "qty": self.initial}, {"product_id": self.pid, "qty": 1}]))
        self.assertEqual(self.stock(), self.initial)
        result = tools.create_order([
            {"product_id": self.pid, "qty": 1, "unit_price": 170},
            {"product_id": self.pid, "qty": 1, "unit_price": 180}])
        self.assertEqual(result["合计"], 350)
        rows = self.rows("SELECT amount FROM order_items WHERE order_id=?", (result["订单号"],))
        self.assertEqual([r["amount"] for r in rows], [170, 180])

    def test_invalid_quantities_and_costs_leave_stock_unchanged(self):
        for qty in (0, -1, 1.5, float("nan"), float("inf"), True):
            with self.subTest(qty=qty):
                with self.assertRaises(ValueError):
                    store.add_purchase(self.pid, qty, 100)
                self.assertIn("error", self.order(qty))
        for cost in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                store.add_purchase(self.pid, 1, cost)
        for price in (0, -1, float("nan"), float("inf")):
            self.assertIn("error", self.order(1, price))
        self.assertFalse(tools.quote([])["ok"])
        self.assertEqual(self.stock(), self.initial)

    def test_threshold_boundary_and_settings_rollback(self):
        store.save_stock_settings([{"id": self.pid, "stock_warning": self.initial,
                                    "stock_target": self.initial + 5}])
        with closing(store.connect()) as conn:
            alert = next(r for r in store.inventory_alerts(conn) if r["id"] == self.pid)
            self.assertEqual(alert["suggested_qty"], 5)
        with self.assertRaises(ValueError):
            store.save_stock_settings([
                {"id": self.pid, "stock_warning": 0, "stock_target": 0},
                {"id": "P001", "stock_warning": 20, "stock_target": 10}])
        self.assertEqual(self.rows("SELECT stock_warning FROM products WHERE id=?", (self.pid,))[0]["stock_warning"], self.initial)

    def test_stock_count_rejects_stale_snapshot_and_requires_reason(self):
        store.add_purchase(self.pid, 1, 100)
        with self.assertRaisesRegex(ValueError, "库存已变化"):
            store.adjust_stock(self.pid, 0, "报损", self.initial)
        with self.assertRaises(ValueError):
            store.adjust_stock(self.pid, 0, " ", self.stock())
        self.assertEqual(store.adjust_stock(self.pid, 0, "测试报损", self.stock()), -(self.initial + 1))
        self.assertEqual(store.adjust_stock(self.pid, 2, "盘点补记", 0), 2)
        with closing(store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT SUM(qty) FROM stock_movements WHERE product_id=?", (self.pid,)).fetchone()[0], 2)

    def test_concurrent_sales_do_not_oversell(self):
        store.adjust_stock(self.pid, 1, "测试盘点", self.initial)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.order(1), range(2)))
        self.assertEqual(sum(bool(r.get("ok")) for r in results), 1)
        self.assertEqual(self.stock(), 0)
        order_id = next(r["订单号"] for r in results if r.get("ok"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            voids = list(pool.map(store.void_order, [order_id, order_id]))
        self.assertEqual(sum(voids), 1)
        self.assertEqual(self.stock(), 1)

    def test_transaction_rolls_back_if_ledger_write_fails(self):
        with closing(store.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_movement BEFORE INSERT ON stock_movements
                WHEN NEW.kind != '期初库存' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            store.add_purchase(self.pid, 2, 100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.order(1)
        self.assertEqual(self.stock(), self.initial)
        self.assertEqual(self.rows("SELECT * FROM purchases"), [])
        self.assertEqual(self.rows("SELECT * FROM orders"), [])

    def test_old_database_upgrade_preserves_stock_orders_and_catalog_reload(self):
        store.add_purchase(self.pid, 7, 100)
        self.order(2)
        with closing(store.connect()) as conn, conn:
            conn.execute("DROP TRIGGER product_opening_stock")
            conn.execute("DROP TABLE stock_movements")
            conn.execute("ALTER TABLE products DROP COLUMN stock_warning")
            conn.execute("ALTER TABLE products DROP COLUMN stock_target")
        store.init_db()
        store.init_db(reload_catalog=True)
        self.assertEqual(self.stock(), self.initial + 5)
        self.assertEqual(self.report()["sold"], 2)
        moves = self.rows("SELECT * FROM stock_movements WHERE product_id=?", (self.pid,))
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["balance"], self.stock())

    def test_new_product_has_opening_entry(self):
        with closing(store.connect()) as conn, conn:
            conn.execute("INSERT INTO products(id,name,package,stock) VALUES ('NEW','测试新品','袋',0)")
        self.assertEqual(self.rows("SELECT balance FROM stock_movements WHERE product_id='NEW'"), [{"balance": 0}])

    def test_guide_file_failure_does_not_report_order_failure(self):
        with patch.object(Path, "write_text", side_effect=OSError("test failure")):
            result = self.order(1)
        self.assertTrue(result["ok"])
        self.assertIn("请勿重复开单", result["提示"])
        self.assertEqual(self.stock(), self.initial - 1)


if __name__ == "__main__":
    unittest.main()
