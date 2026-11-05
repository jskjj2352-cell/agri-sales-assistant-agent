"""老板后台：经营概况、进货入库、商品与价格、台账导出、订单管理。
密码在 settings.txt 的 ADMIN_PASSWORD 里设置。"""
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from store import (CATEGORIES, add_purchase, connect, export_catalog_csv, init_db,
                   inventory_alerts, void_order)
from inventory_ui import render_inventory, render_movements, render_statistics

st.set_page_config(page_title="老板后台", page_icon="🔐", layout="wide")
init_db()
ss = st.session_state

# ---------------- 登录 ----------------
PWD = os.environ.get("ADMIN_PASSWORD", "")
if not PWD or PWD == "请改成你的密码":
    st.error("还没设置后台密码。请打开 settings.txt，把 ADMIN_PASSWORD 改成你自己的密码，再重启助理。")
    st.stop()

if not ss.get("admin_ok"):
    st.title("🔐 老板后台")
    with st.form("login"):
        p = st.text_input("后台密码", type="password")
        if st.form_submit_button("进入", type="primary"):
            if p == PWD:
                ss.admin_ok = True
                st.rerun()
            st.error("密码不对")
    st.stop()

with st.sidebar:
    if st.button("退出后台"):
        ss.admin_ok = False
        st.rerun()

st.title("🔐 老板后台")
if ss.get("flash"):
    st.success(ss.pop("flash"))
conn = connect()
products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
pmap = {p["id"]: p for p in products}


def clean(v):
    """表格里的空值（None / NaN）统一转成空字符串。"""
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def done(msg):
    ss.flash = msg
    conn.close()
    st.rerun()


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")  # 带 BOM，Excel 直接打开不乱码


alerts = inventory_alerts(conn)
if alerts:
    st.warning(f"有 {len(alerts)} 件商品达到库存预警线，其中 {sum(p['stock'] <= 0 for p in alerts)} 件缺货。请查看【库存预警】。")

tab_sum, tab_stock, tab_in, tab_movements, tab_stats, tab_prod, tab_ledger, tab_order = st.tabs(
    ["📊 经营概况", "⚠️ 库存预警", "🚚 进货入库", "📦 出入库流水", "📈 进出货统计",
     "💰 商品与价格", "📒 台账导出", "🧾 订单管理"])

with tab_stock:
    render_inventory(conn, products, done)
with tab_movements:
    render_movements(conn, products)
with tab_stats:
    render_statistics(conn, products)

# ---------------- 经营概况 ----------------
with tab_sum:
    period = st.radio("时间", ["今天", "近7天", "本月", "全部"], horizontal=True)
    today = date.today()
    start = {"今天": today, "近7天": today - timedelta(days=6),
             "本月": today.replace(day=1), "全部": date(2000, 1, 1)}[period].isoformat()
    items = pd.read_sql_query(
        """SELECT oi.*, o.created_at FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.status != '作废' AND o.created_at >= ?""", conn, params=(start,))
    n_orders = items["order_id"].nunique() if len(items) else 0
    revenue = items["amount"].sum() if len(items) else 0
    known = items.dropna(subset=["unit_cost"]) if len(items) else items
    profit = (known["amount"] - known["qty"] * known["unit_cost"]).sum() if len(known) else 0

    c1, c2, c3 = st.columns(3)
    c1.metric("销售额", f"{revenue:,.2f} 元")
    c2.metric("订单数", n_orders)
    c3.metric("毛利", f"{profit:,.2f} 元")
    if len(items) and len(known) < len(items):
        st.caption("⚠️ 部分销售缺少开单时的进价，未计入毛利；事后补录进货不会改变历史订单成本。")

    left, right = st.columns(2)
    with left:
        st.subheader("卖得最好的")
        if len(items):
            top = (items.groupby("name").agg(数量=("qty", "sum"), 金额=("amount", "sum"))
                   .sort_values("金额", ascending=False).reset_index().rename(columns={"name": "商品"}))
            st.dataframe(top, hide_index=True, width="stretch")
        else:
            st.caption("这段时间没有销售")
    with right:
        st.subheader("⚠️ 库存预警")
        low = [{"商品": p["name"], "库存": f"{p['stock']}{p['package']}",
                "预警线": p["stock_warning"], "建议补货": p["suggested_qty"]}
               for p in alerts]
        if low:
            st.dataframe(low, hide_index=True, width="stretch")
        else:
            st.caption("库存都够")

# ---------------- 进货入库 ----------------
with tab_in:
    st.caption("每次进货都在这里登记：自动加库存，同时生成采购台账。")
    with st.form("purchase", clear_on_submit=True):
        pid = st.selectbox("商品", list(pmap),
                           format_func=lambda i: f"{pmap[i]['name']}（现有 {pmap[i]['stock']}{pmap[i]['package']}）")
        c1, c2 = st.columns(2)
        qty = c1.number_input("进货数量（袋/瓶）", min_value=1, step=1)
        cost = c2.number_input("进价（元/袋或瓶）", min_value=0.0, step=0.5)
        c3, c4, c5 = st.columns(3)
        supplier = c3.text_input("供货商")
        batch = c4.text_input("生产批号")
        prod_date = c5.text_input("生产日期", placeholder="如 2026-09-01")
        note = st.text_input("备注")
        if st.form_submit_button("✅ 确认入库", type="primary"):
            if cost <= 0:
                st.error("请填进价，不填没法算毛利")
            else:
                try:
                    purchase_id = add_purchase(pid, qty, cost, supplier, batch, prod_date, note)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    done(f"采购单 #{purchase_id} 已入库：{pmap[pid]['name']} × {qty}")
    st.subheader("最近进货")
    st.dataframe(pd.read_sql_query(
        """SELECT created_at 时间, name 商品, qty 数量, unit_cost 进价, supplier 供货商,
                  batch_no 批号 FROM purchases ORDER BY id DESC LIMIT 20""", conn),
        hide_index=True, width="stretch")

# ---------------- 商品与价格 ----------------
with tab_prod:
    st.subheader("改价格 / 商品资料")
    st.caption("直接在表格里改，改完点保存。库存请用【进货入库】、作废订单或【库存预警】中的盘点功能调整。")
    pdf = pd.read_sql_query(
        """SELECT id, category, name, price, min_price, stock, manufacturer, reg_no, toxicity,
                  restricted, notes FROM products ORDER BY id""", conn)
    cat_filter = st.pills("只看", CATEGORIES, selection_mode="multi", key="cat_filter")
    if cat_filter:
        pdf = pdf[pdf["category"].isin(cat_filter)]
    edited = st.data_editor(
        pdf, hide_index=True, width="stretch", disabled=["id", "stock"], key="prod_editor",
        column_config={
            "id": "编号", "category": st.column_config.SelectboxColumn("类别", options=CATEGORIES),
            "name": "名称", "price": st.column_config.NumberColumn("售价", format="%.2f"),
            "min_price": st.column_config.NumberColumn("底价", format="%.2f"), "stock": "库存",
            "manufacturer": "生产企业", "reg_no": "登记证号/审定编号", "toxicity": "毒性",
            "restricted": st.column_config.SelectboxColumn("限用农药", options=["否", "是"]),
            "notes": "注意事项"})
    if st.button("💾 保存商品修改", type="primary"):
        bad = edited[edited["min_price"] > edited["price"]]
        if len(bad):
            st.error("底价不能高于售价：" + "、".join(bad["name"]))
        else:
            for r in edited.to_dict("records"):
                conn.execute(
                    """UPDATE products SET category=?, name=?, price=?, min_price=?, manufacturer=?,
                       reg_no=?, toxicity=?, restricted=?, notes=? WHERE id=?""",
                    (clean(r["category"]) or "农药", clean(r["name"]), float(r["price"]),
                     float(r["min_price"]), clean(r["manufacturer"]), clean(r["reg_no"]),
                     clean(r["toxicity"]), clean(r["restricted"]) or "否", clean(r["notes"]), r["id"]))
            conn.commit()
            export_catalog_csv()
            done("商品修改已保存")

    pesticides = {i: p for i, p in pmap.items() if p["category"] == "农药"}
    goods = {i: p for i, p in pmap.items() if p["category"] != "农药"}

    st.subheader("登记用途（照抄农药标签）")
    st.caption("作物和防治对象请写标准名称（马铃薯、番茄、甘蓝，不要写土豆、西红柿、包菜）。"
               "可以在最下面加行；选中行按 Delete 删除。")
    rdf = pd.read_sql_query("SELECT * FROM registrations ORDER BY product_id", conn)
    redited = st.data_editor(
        rdf, num_rows="dynamic", hide_index=True, width="stretch", key="reg_editor",
        column_config={
            "product_id": st.column_config.SelectboxColumn(
                "商品", options=list(pesticides), required=True),
            "crop": "作物", "target": "防治对象",
            "dose_min": st.column_config.NumberColumn("每亩用量下限", min_value=0),
            "dose_max": st.column_config.NumberColumn("每亩用量上限", min_value=0),
            "dose_unit": st.column_config.SelectboxColumn("单位", options=["g/亩", "ml/亩"]),
            "method": "施药方法", "phi_days": "安全间隔期(天)", "max_times": "每季最多次数"})
    st.caption("农药编号对照：" + "　".join(f"{i}={p['name']}" for i, p in pesticides.items()))
    if st.button("💾 保存登记用途", type="primary"):
        rows, errors = [], []
        for n, r in enumerate(redited.to_dict("records"), 1):
            if all(clean(v) == "" for v in r.values()):
                continue  # 空行跳过
            if r["product_id"] not in pesticides:
                errors.append(f"第{n}行：商品编号不对（这里只能填农药）")
            elif not clean(r["crop"]) or not clean(r["target"]):
                errors.append(f"第{n}行：作物和防治对象不能空")
            elif pd.isna(r["dose_min"]) or pd.isna(r["dose_max"]) or r["dose_min"] > r["dose_max"] \
                    or r["dose_max"] <= 0:
                errors.append(f"第{n}行：用量不对（下限不能大于上限，也不能为0）")
            elif not clean(r["dose_unit"]):
                errors.append(f"第{n}行：请选用量单位")
            else:
                rows.append(r)
        if errors:
            st.error("\n".join(errors))
        else:
            conn.execute("DELETE FROM registrations")
            for r in rows:
                conn.execute("INSERT INTO registrations VALUES (?,?,?,?,?,?,?,?,?)",
                             (r["product_id"], clean(r["crop"]), clean(r["target"]),
                              float(r["dose_min"]), float(r["dose_max"]), clean(r["dose_unit"]),
                              clean(r["method"]) or "喷雾", clean(r["phi_days"]),
                              clean(r["max_times"])))
            conn.commit()
            export_catalog_csv()
            done(f"已保存 {len(rows)} 条登记用途")

    st.subheader("化肥、种子、农具的用途和用量")
    st.caption("作物填标准名称，不分作物的填“通用”；用途可以写多个关键词，用空格隔开（如：追肥 补钾 缺钾），"
               "助理靠这些关键词匹配。农具没有亩用量的，用量留空即可。")
    udf = pd.read_sql_query("SELECT * FROM usage ORDER BY product_id", conn)
    uedited = st.data_editor(
        udf, num_rows="dynamic", hide_index=True, width="stretch", key="usage_editor",
        column_config={
            "product_id": st.column_config.SelectboxColumn("商品", options=list(goods), required=True),
            "crop": "作物", "purpose": "用途关键词",
            "dose_min": st.column_config.NumberColumn("每亩用量下限", min_value=0),
            "dose_max": st.column_config.NumberColumn("每亩用量上限", min_value=0),
            "dose_unit": st.column_config.SelectboxColumn(
                "单位", options=["kg/亩", "g/亩", "ml/亩", "粒/亩", "m/亩", "张/亩"]),
            "method": "用法", "note": "说明"})
    st.caption("编号对照：" + "　".join(f"{i}={p['name']}" for i, p in goods.items()))
    if st.button("💾 保存用途和用量", type="primary"):
        rows, errors = [], []
        for n, r in enumerate(uedited.to_dict("records"), 1):
            if all(clean(v) == "" for v in r.values()):
                continue
            has_dose = not pd.isna(r["dose_max"])
            if r["product_id"] not in goods:
                errors.append(f"第{n}行：商品编号不对（这里不能填农药）")
            elif not clean(r["purpose"]):
                errors.append(f"第{n}行：用途不能空")
            elif has_dose and (pd.isna(r["dose_min"]) or r["dose_min"] > r["dose_max"]):
                errors.append(f"第{n}行：用量不对（下限不能大于上限）")
            elif has_dose and clean(r["dose_unit"]).split("/")[0] != goods[r["product_id"]]["spec_unit"]:
                errors.append(f"第{n}行：用量单位要和包装单位一致"
                              f"（这个商品的包装单位是 {goods[r['product_id']]['spec_unit']}）")
            else:
                rows.append(r)
        if errors:
            st.error("\n".join(errors))
        else:
            conn.execute("DELETE FROM usage")
            for r in rows:
                has_dose = not pd.isna(r["dose_max"])
                conn.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
                             (r["product_id"], clean(r["crop"]) or "通用", clean(r["purpose"]),
                              float(r["dose_min"]) if has_dose else None,
                              float(r["dose_max"]) if has_dose else None,
                              clean(r["dose_unit"]) if has_dose else "",
                              clean(r["method"]), clean(r["note"])))
            conn.commit()
            export_catalog_csv()
            done(f"已保存 {len(rows)} 条用途")

    with st.expander("➕ 新增商品"):
        with st.form("new_product", clear_on_submit=True):
            c1, c2, c3 = st.columns([1, 2, 2])
            category = c1.selectbox("类别", CATEGORIES)
            name = c2.text_input("商品名称", placeholder="如：苯醚甲环唑 10%水分散粒剂 / 尿素 46%")
            ai = c3.text_input("有效成分/养分/品种", placeholder="如：苯醚甲环唑 / 氮46 / 玉米杂交种")
            c1, c2, c3, c4 = st.columns(4)
            formulation = c1.text_input("剂型/形态", placeholder="如：水分散粒剂 / 颗粒")
            spec_amount = c2.number_input("每袋/瓶装多少", min_value=0.0, step=1.0)
            spec_unit = c3.selectbox("单位", ["g", "ml", "kg", "粒", "m", "张", "个", "台", "套"])
            package = c4.selectbox("包装", ["袋", "瓶", "桶", "盒", "包", "卷", "个", "台", "套"])
            c1, c2, c3, c4 = st.columns(4)
            price = c1.number_input("售价", min_value=0.0, step=1.0)
            min_price = c2.number_input("底价", min_value=0.0, step=1.0)
            toxicity = c3.selectbox("毒性（农药填）", ["", "低毒", "微毒", "中等毒", "高毒", "剧毒"])
            ph = c4.selectbox("酸碱性（农药填）", ["", "中性", "碱性", "酸性"])
            c1, c2, c3 = st.columns(3)
            manufacturer = c1.text_input("生产企业")
            reg_no = c2.text_input("登记证号/审定编号", placeholder="农药 PD…，种子填品种审定编号")
            restricted = c3.selectbox("限用农药", ["否", "是"])
            notes = st.text_input("注意事项", placeholder="如：对蜜蜂有毒，花期禁用 / 忌氯作物别用")
            if st.form_submit_button("新增", type="primary"):
                if not name or spec_amount <= 0 or price <= 0:
                    st.error("名称、包装规格、售价都要填")
                elif min_price > price:
                    st.error("底价不能高于售价")
                elif category == "农药" and not (ai and toxicity and ph):
                    st.error("农药要填有效成分、毒性和酸碱性")
                elif toxicity in ("高毒", "剧毒"):
                    st.error("高毒、剧毒农药不建议通过助理销售，请人工处理")
                else:
                    nums = [int(i[1:]) for i in pmap if i[1:].isdigit()]
                    new_id = f"P{(max(nums) + 1) if nums else 1:03d}"
                    conn.execute(
                        """INSERT INTO products (id, name, active_ingredient, formulation, spec_amount,
                           spec_unit, package, price, min_price, stock, toxicity, ph_property,
                           restricted, notes, manufacturer, reg_no, category)
                           VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                        (new_id, name, ai, formulation, spec_amount, spec_unit, package, price,
                         min_price or price, toxicity, ph, restricted if category == "农药" else "",
                         notes, manufacturer, reg_no, category))
                    conn.commit()
                    export_catalog_csv()
                    where = "【登记用途】里加它能治什么" if category == "农药" else "【用途和用量】里加它用在哪"
                    done(f"已新增 {new_id}。下一步：在{where}，再去【进货入库】加库存。")

# ---------------- 台账导出 ----------------
with tab_ledger:
    st.caption("《农药管理条例》要求农药经营者建立采购台账和销售台账，至少保存2年。执法检查时可直接导出打印。")
    rng = st.date_input("日期范围", value=(date.today().replace(day=1), date.today()))
    if isinstance(rng, (tuple, list)):
        d1 = rng[0]
        d2 = rng[1] if len(rng) > 1 else rng[0]
    else:
        d1 = d2 = rng
    a, b = d1.isoformat(), (d2 + timedelta(days=1)).isoformat()

    st.subheader("销售台账")
    sales = pd.read_sql_query(
        """SELECT o.created_at 销售日期, o.id 订单号, oi.name 农药名称, oi.reg_no 登记证号,
                  oi.spec 规格, oi.manufacturer 生产企业, oi.qty 数量, oi.unit_price 单价,
                  oi.amount 金额, o.customer 购买人, o.phone 联系电话,
                  oi.crop 用于作物, oi.target 防治对象, o.status 状态
           FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.created_at >= ? AND o.created_at < ? ORDER BY o.id""", conn, params=(a, b))
    st.dataframe(sales, hide_index=True, width="stretch")
    st.download_button("⬇️ 导出销售台账（Excel 可打开）", csv_bytes(sales),
                       f"销售台账_{d1}_{d2}.csv", "text/csv")

    st.subheader("采购台账")
    buys = pd.read_sql_query(
        """SELECT created_at 进货日期, name 农药名称, reg_no 登记证号, spec 规格,
                  manufacturer 生产企业, supplier 供货商, batch_no 生产批号, prod_date 生产日期,
                  qty 数量, unit_cost 进价, ROUND(qty * unit_cost, 2) 金额
           FROM purchases WHERE created_at >= ? AND created_at < ? ORDER BY id""",
        conn, params=(a, b))
    st.dataframe(buys, hide_index=True, width="stretch")
    st.download_button("⬇️ 导出采购台账（Excel 可打开）", csv_bytes(buys),
                       f"采购台账_{d1}_{d2}.csv", "text/csv")

    missing = [p["name"] for p in products if not p["reg_no"] or not p["manufacturer"]]
    if missing:
        st.warning("这些商品还没填登记证号或生产企业，台账不完整，请到【商品与价格】补上：\n\n"
                   + "、".join(missing))

# ---------------- 订单管理 ----------------
with tab_order:
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall()
    if not orders:
        st.caption("还没有订单")
    else:
        st.dataframe(
            [{"订单号": o["id"], "时间": o["created_at"], "客户": o["customer"] or "散客",
              "金额": o["total"], "状态": o["status"]} for o in orders],
            hide_index=True, width="stretch")
        oid = st.selectbox("查看/作废订单", [o["id"] for o in orders],
                           format_func=lambda i: f"#{i}")
        o = next(x for x in orders if x["id"] == oid)
        with st.expander("用药说明", expanded=False):
            st.code(o["guide"] or "", language=None)
        if o["status"] == "作废":
            st.info("这个订单已经作废")
        else:
            sure = st.checkbox("确认作废（库存会加回去，台账里保留记录并标记作废）")
            if st.button("❌ 作废订单", disabled=not sure):
                void_order(oid)
                done(f"订单 #{oid} 已作废，库存已加回")

conn.close()
