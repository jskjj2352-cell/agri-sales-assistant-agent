"""老板后台的库存预警、出入库流水和统计页面。"""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from store import (adjust_stock, inventory_alerts, inventory_report,
                   save_stock_settings)


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


def date_range(label, key):
    selected = st.date_input(label, (date.today().replace(day=1), date.today()), key=key)
    if not isinstance(selected, (tuple, list)) or len(selected) != 2:
        st.info("请选择开始和结束日期。")
        return None
    if selected[0] > selected[1]:
        st.error("开始日期不能晚于结束日期。")
        return None
    return selected


def render_inventory(conn, products, done):
    st.subheader("库存预警与补货")
    st.caption("库存小于或等于预警线时提醒；建议补货量 = 补货目标 − 当前库存。数量按商品包装计。")
    alerts = pd.DataFrame([dict(r) for r in inventory_alerts(conn)])
    a, b, c = st.columns(3)
    a.metric("缺货商品", sum(p["stock"] <= 0 for p in products))
    b.metric("预警商品（含缺货）", len(alerts))
    c.metric("商品总数", len(products))
    if alerts.empty:
        st.success("当前没有库存预警。")
    else:
        display = alerts.rename(columns={"id": "编号", "name": "商品", "category": "类别",
            "package": "包装", "stock": "当前库存", "stock_warning": "预警线",
            "stock_target": "补货目标", "alert": "状态", "suggested_qty": "建议补货"})
        st.dataframe(display, hide_index=True, width="stretch")
        st.download_button("导出补货清单", csv_bytes(display), "补货清单.csv", "text/csv")

    with st.expander("设置每件商品的预警线和补货目标"):
        settings = pd.read_sql_query("""SELECT id, name, package, stock, stock_warning, stock_target
            FROM products ORDER BY id""", conn)
        edited = st.data_editor(settings, hide_index=True, width="stretch", key="stock_settings",
            disabled=["id", "name", "package", "stock"], column_config={
                "id": "编号", "name": "商品", "package": "包装", "stock": "当前库存",
                "stock_warning": st.column_config.NumberColumn("预警线", min_value=0, step=1, required=True),
                "stock_target": st.column_config.NumberColumn("补货目标", min_value=0, step=1, required=True)})
        if st.button("保存预警设置"):
            try:
                save_stock_settings(edited.to_dict("records"))
            except ValueError as exc:
                st.error(str(exc))
            else:
                done("库存预警设置已保存")

    st.subheader("盘点 / 报损")
    st.caption("进货请使用【进货入库】；这里用于核对实物库存、破损或其他差异，必须填写原因。")
    pmap = {p["id"]: p for p in products}
    if not pmap:
        st.info("请先新增商品。")
        return
    pid = st.selectbox("盘点商品", list(pmap), key="count_product",
                       format_func=lambda i: f"{i} · {pmap[i]['name']}")
    p = pmap[pid]
    # 首次显示时保留库存快照；提交后若库存已变动，要求重新核对。
    snapshot_key = f"count_snapshot_{pid}"
    expected = st.session_state.setdefault(snapshot_key, p["stock"])
    if expected != p["stock"]:
        st.warning("该商品库存已变化，请点击“刷新盘点库存”，重新核对实物数量。")
    if st.button("刷新盘点库存"):
        st.session_state[snapshot_key] = p["stock"]
        st.session_state.pop(f"actual_stock_{pid}", None)
        conn.close()
        st.rerun()
    st.write(f"盘点前账面库存：{expected}{p['package']}")
    with st.form("stock_count"):
        actual = st.number_input("实盘数量", min_value=0, value=max(0, int(expected)), step=1,
                                 key=f"actual_stock_{pid}")
        reason = st.text_input("调整原因", placeholder="例如：破损2袋 / 月末盘点差异")
        confirmed = st.checkbox("已核对实物数量和调整原因")
        if st.form_submit_button("保存盘点结果"):
            if not confirmed:
                st.error("请先确认已核对实物数量和调整原因。")
            else:
                try:
                    delta = adjust_stock(pid, actual, reason, expected_stock=expected)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop(snapshot_key, None)
                    done(f"盘点已保存，库存调整 {delta:+d}{p['package']}")


def render_movements(conn, products):
    st.subheader("出入库流水")
    st.caption("期初库存是建档或升级时的余额，不计为采购。升级前的采购与销售仍可在台账和统计中查询。")
    selected = date_range("流水日期范围", "movement_dates")
    if not selected:
        return
    start, end = selected
    pmap = {p["id"]: p for p in products}
    a, b = st.columns(2)
    pid = a.selectbox("筛选商品", [""] + list(pmap), key="movement_product",
                      format_func=lambda i: "全部商品" if not i else f"{i} · {pmap[i]['name']}")
    kind = b.selectbox("流水类型", ["全部", "采购入库", "销售出库", "作废退回", "盘点调增", "盘点调减", "期初库存"])
    sql = """SELECT id 流水号, created_at 时间, product_id 商品编号, name 商品, package 包装,
        kind 类型, qty 库存变动, balance 变动后库存, ref_id 关联单号, note 备注
        FROM stock_movements WHERE created_at >= ? AND created_at < ?"""
    params = [start.isoformat(), (end + timedelta(days=1)).isoformat()]
    if pid:
        sql += " AND product_id=?"
        params.append(pid)
    if kind != "全部":
        sql += " AND kind=?"
        params.append(kind)
    df = pd.read_sql_query(sql + " ORDER BY id DESC", conn, params=params)
    activity = df[df["类型"] != "期初库存"]
    a, b, c = st.columns(3)
    a.metric("流水笔数", len(df))
    b.metric("入库包装件数（不含期初）", int(activity.loc[activity["库存变动"] > 0, "库存变动"].sum()))
    c.metric("出库包装件数", int(-activity.loc[activity["库存变动"] < 0, "库存变动"].sum()))
    st.caption("以上数量按当前筛选汇总；不同商品的袋、瓶、台等相加仅表示包装件数。关联单号：采购入库对应采购号，销售出库和作废退回对应订单号。")
    if df.empty:
        st.info("所选条件下没有流水记录。")
    st.dataframe(df, hide_index=True, width="stretch")
    st.download_button("导出出入库流水", csv_bytes(df), f"出入库流水_{start}_{end}.csv", "text/csv")


def render_statistics(conn, products):
    st.subheader("进出货统计")
    selected = date_range("统计日期范围", "statistics_dates")
    if not selected:
        return
    start, end = selected
    pmap = {p["id"]: p for p in products}
    pid = st.selectbox("统计商品", [""] + list(pmap), key="statistics_product",
                       format_func=lambda i: "全部商品" if not i else f"{i} · {pmap[i]['name']}")
    df = pd.DataFrame([dict(r) for r in inventory_report(conn, start, end)])
    if df.empty:
        st.info("请先新增商品。")
        return
    if pid:
        df = df[df["id"] == pid]
    a, b, c = st.columns(3)
    a.metric("采购金额", f"{df['purchase_amount'].sum():,.2f} 元")
    b.metric("有效销售额", f"{df['sales_amount'].sum():,.2f} 元")
    c.metric("已知成本毛利", f"{df['profit'].sum():,.2f} 元")
    a, b = st.columns(2)
    a.metric("采购包装件数", int(df["bought"].sum()))
    b.metric("有效销售包装件数", int(df["sold"].sum()))
    st.caption("作废订单不计销售；毛利按开单时记录的最近进价估算，不含运营费用。采购金额不是销售成本。当前库存为实时值，不是所选日期的期末值。")
    unknown = int(df["unknown_cost_qty"].sum())
    if unknown:
        st.warning(f"有 {unknown} 件销售缺少开单时的进价，未计入毛利；事后补录进货不会改变历史成本。")

    bounds = [start.isoformat(), (end + timedelta(days=1)).isoformat()]
    sales = pd.read_sql_query("""SELECT substr(o.created_at,1,10) 日期, SUM(oi.amount) 销售额
        FROM order_items oi JOIN orders o ON o.id=oi.order_id
        WHERE o.status != '作废' AND o.created_at >= ? AND o.created_at < ?"""
        + (" AND oi.product_id=?" if pid else "") + " GROUP BY 日期", conn,
        params=bounds + ([pid] if pid else []))
    buys = pd.read_sql_query("""SELECT substr(created_at,1,10) 日期, SUM(qty*unit_cost) 采购金额
        FROM purchases WHERE created_at >= ? AND created_at < ?"""
        + (" AND product_id=?" if pid else "") + " GROUP BY 日期", conn,
        params=bounds + ([pid] if pid else []))
    trend = sales.merge(buys, on="日期", how="outer").fillna(0).sort_values("日期")
    if not trend.empty:
        st.subheader("每日采购与销售金额")
        st.bar_chart(trend.set_index("日期"), stack=False)
    display = df.rename(columns={"id": "编号", "name": "商品", "category": "类别", "package": "包装",
        "stock": "当前库存", "bought": "采购数量", "purchase_amount": "采购金额",
        "sold": "有效销售数量", "sales_amount": "销售额", "profit": "已知成本毛利",
        "unknown_cost_qty": "缺进价销售数量"})
    st.dataframe(display, hide_index=True, width="stretch")
    st.download_button("导出进出货统计", csv_bytes(display), f"进出货统计_{start}_{end}.csv", "text/csv")
