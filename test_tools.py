"""离线测试工具层（不调用 API、不花钱）：python test_tools.py"""
import shutil
import tempfile
from pathlib import Path

import store
import tools

# 用临时数据库，不影响真实库存
tmp = Path(tempfile.mkdtemp())
store.DB_PATH = tmp / "test.db"
tools.ORDERS_DIR = tmp / "orders"
store.init_db(reload_catalog=True)

def ids(match_result, i=0):
    return [p["product_id"] for p in match_result["匹配结果"][i]["店里能用的药"]]


r = tools.match_products("土豆", ["晚疫病"], mu=20)
plans = r["匹配结果"][0]["店里能用的药"]
assert r["作物"] == "马铃薯" and set(ids(r)) == {"P002", "P003", "P004"}, r
print("✓ 俗称自动匹配：土豆→马铃薯，晚疫病找到", [(p["商品"], p["建议购买数量"], p["小计"]) for p in plans])

r = tools.match_products("黄瓜", ["腻虫"])
assert {"P007", "P016"} <= set(ids(r)), r
print("✓ 俗称自动匹配：腻虫→蚜虫")

r = tools.match_products("玉米", ["锈病", "玉米螟"])
assert r["店里没有对症药的问题"] == ["锈病"] and len(r["匹配结果"]) == 1, r
print("✓ 没有对症药不会乱推荐：", r["店里没有对症药的问题"])

r = tools.match_products("玉米", ["除草"])
assert {"P008", "P011", "P012", "P013"} <= set(ids(r)), r
print("✓ 除草：苗前、苗后、阔叶、禾本科除草剂都能匹配到")

r = tools.calc_dosage("P008", "玉米", "一年生杂草", 10)
assert r["建议购买数量"] == 10, r
print("✓ 标准名称“一年生杂草”精确查找正常")

r = tools.match_products("白菜", ["软腐病"])
assert {"P025", "P028"} <= set(ids(r)), r
r = tools.match_products("黄瓜", ["根上长疙瘩"])
assert ids(r) == ["P030"], r
print("✓ 新增俗称：白菜→大白菜，根上长疙瘩→根结线虫")

assert tools.check_mix(["P008", "P031"])["可以混用"] is False
print("✓ 混配检查：烟嘧磺隆 + 辛硫磷（有机磷）被拦截")

assert "min_price" not in str(tools.match_products("玉米", ["玉米螟"], 10))
print("✓ 底价不会泄露给模型")

r = tools.calc_dosage("P003", "马铃薯", "晚疫病", 20)
assert r["建议购买数量"] == 8, r  # 40g x 20亩 = 800g / 100g = 8袋
print("✓ 用量计算：20亩", r["建议购买数量"], "小计", r["小计"])

r = tools.calc_dosage("P005", "番茄", "早疫病", 5)
assert "error" in r
print("✓ 超范围用药被拒绝：", r["error"])

assert tools.check_mix(["P009", "P003"])["可以混用"] is False
assert tools.check_mix(["P003", "P004"])["可以混用"] == "未发现已知禁忌"
print("✓ 混配检查：碱性药剂拦截")

r = tools.quote([{"product_id": "P003", "qty": 8, "unit_price": 10}])
assert not r["ok"]
print("✓ 低于底价被拒绝：", r["问题"][0])

r = tools.quote([{"product_id": "P003", "qty": 8, "unit_price": 13}])
assert r["ok"] and r["合计"] == 104
print("✓ 底价以上可以砍价，合计", r["合计"])

r = tools.match_products("西红柿", ["早疫病"], mu=2, days_to_harvest=10)
e = r["匹配结果"][0]
assert [b["商品"] for b in e["快采收了不能用的药"]] == ["代森锰锌 80%可湿性粉剂"], e
assert all(p["product_id"] != "P004" for p in e["店里能用的药"])
print("✓ 离采收10天：安全间隔期15天的代森锰锌被排除")

store.add_purchase("P003", 10, 9.0, supplier="测试供货商", batch_no="B001")
print("✓ 进货入库：P003 +10，进价 9 元")

r = tools.create_order(
    [{"product_id": "P003", "qty": 8, "crop": "土豆", "target": "晚疫病", "mu": 20}],
    customer="张大爷", phone="13800000000")
assert r["ok"], r
order_id = r["订单号"]
print("✓ 开单成功，订单号", order_id)
print(Path(r["用药说明文件"]).read_text(encoding="utf-8"))


def stock_of(pid):
    return store.connect().execute("SELECT stock FROM products WHERE id=?", (pid,)).fetchone()[0]


assert stock_of("P003") == 62, stock_of("P003")  # 60 + 10 - 8
print("✓ 库存正确：60 + 进货10 - 卖出8 = 62")

it = store.connect().execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchone()
assert it["crop"] == "马铃薯" and it["unit_cost"] == 9.0 and it["amount"] == 120, dict(it)
print("✓ 销售台账明细：作物已标准化，记录了进价，毛利 =", it["amount"] - it["qty"] * it["unit_cost"])

assert store.void_order(order_id) and stock_of("P003") == 70
assert not store.void_order(order_id)  # 不能重复作废
print("✓ 作废订单：库存加回到 70，不能重复作废")

store.init_db(reload_catalog=True)
assert stock_of("P003") == 70, stock_of("P003")
print("✓ 重新导入 CSV 不会覆盖库存")

shutil.rmtree(tmp, ignore_errors=True)
print("\n全部通过")
