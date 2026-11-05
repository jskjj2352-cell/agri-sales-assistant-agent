"""后台页面交互测试：临时商品目录与数据库，不读取真实密码或调用 API。"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import store
import tools


class InventoryPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name in store.CATALOG_FILES:
            shutil.copy2(store.DATA / name, root / name)
        for item in (patch.object(store, "DATA", root), patch.object(store, "DB_PATH", root / "test.db"),
                     patch.object(tools, "ORDERS_DIR", root / "orders"),
                     patch.dict(os.environ, {"ADMIN_PASSWORD": "test-only-password"})):
            item.start()
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(str(store.BASE / "pages" / "老板后台.py"), default_timeout=15)

    def assert_page_ok(self):
        self.assertFalse(self.app.exception, [e.message for e in self.app.exception])

    def widget(self, kind, label):
        return next(w for w in getattr(self.app, kind) if w.label == label)

    def login(self):
        self.app.run()
        self.assert_page_ok()
        self.widget("text_input", "后台密码").set_value("test-only-password")
        self.widget("button", "进入").click().run()
        self.assert_page_ok()

    def test_login_empty_state_purchase_and_statistics(self):
        self.login()
        self.assertEqual(len(self.app.tabs), 8)
        self.widget("selectbox", "商品").set_value("P052")
        self.widget("number_input", "进货数量（袋/瓶）").set_value(4)
        self.widget("number_input", "进价（元/袋或瓶）").set_value(100.0)
        self.widget("button", "✅ 确认入库").click().run()
        self.assert_page_ok()
        self.assertTrue(any("采购单 #" in s.value for s in self.app.success))
        tools.create_order([{"product_id": "P052", "qty": 1}])
        self.app.run()
        self.assert_page_ok()
        self.assertEqual(self.widget("metric", "采购金额").value, "400.00 元")
        self.assertEqual(self.widget("metric", "有效销售额").value, "180.00 元")
        self.assertEqual(self.widget("metric", "已知成本毛利").value, "80.00 元")
        self.widget("selectbox", "统计商品").set_value("P001").run()
        self.assert_page_ok()
        self.assertEqual(self.widget("metric", "采购金额").value, "0.00 元")

    def test_count_rejects_stale_inventory_then_accepts_refresh(self):
        self.login()
        self.widget("selectbox", "盘点商品").set_value("P052").run()
        store.add_purchase("P052", 1, 100)
        self.widget("number_input", "实盘数量").set_value(2)
        self.widget("text_input", "调整原因").set_value("测试盘点")
        self.widget("checkbox", "已核对实物数量和调整原因").check()
        self.widget("button", "保存盘点结果").click().run()
        self.assert_page_ok()
        self.assertTrue(any("库存已变化" in e.value for e in self.app.error))
        self.widget("button", "刷新盘点库存").click().run()
        self.assert_page_ok()
        self.widget("number_input", "实盘数量").set_value(2)
        self.widget("button", "保存盘点结果").click().run()
        self.assert_page_ok()
        self.assertTrue(any("盘点已保存" in s.value for s in self.app.success))

    def test_sales_page_uses_product_warning_threshold(self):
        import agent
        import vision

        store.init_db()
        store.save_stock_settings([{"id": "P052", "stock_warning": 20, "stock_target": 30}])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-test"}), \
                patch.object(agent, "make_client", return_value=object()), \
                patch.object(vision, "make_vision_client", return_value=None):
            app = AppTest.from_file(str(store.BASE / "app.py"), default_timeout=15).run()
        self.assertFalse(app.exception, [e.message for e in app.exception])
        stock_table = next(d.value for d in app.dataframe if "提醒" in d.value.columns)
        row = stock_table[stock_table["商品"] == "电动喷雾器 16升（锂电池）"].iloc[0]
        self.assertEqual(row["提醒"], "⚠️ 库存偏低")


if __name__ == "__main__":
    unittest.main()
