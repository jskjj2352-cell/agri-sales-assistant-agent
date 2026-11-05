"""数据层：SQLite 存商品、登记信息、订单、进货、台账。

商品资料的两种维护方式：
  1. 老板后台网页（推荐）：改价格、新增商品、进货入库
  2. Excel 编辑 data/products.csv 和 data/registrations.csv，再运行 python agent.py --reload
     重新导入只更新商品资料和价格，【不会改动已有商品的库存】；库存只通过进货和销售变化。
"""
import csv
import json
import math
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
DB_PATH = DATA / "store.db"

# 商品资料字段（不含库存）
CATALOG_FIELDS = ["name", "active_ingredient", "formulation", "spec_amount", "spec_unit",
                  "package", "price", "min_price", "toxicity", "ph_property", "restricted",
                  "notes", "manufacturer", "reg_no", "category"]
CATEGORIES = ["农药", "化肥", "种子", "农具"]
USAGE_FIELDS = ["product_id", "crop", "purpose", "dose_min", "dose_max", "dose_unit",
                "method", "note"]
CATALOG_FILES = ("products.csv", "registrations.csv", "usage.csv")


def _read_csv(path: Path):
    # Excel 另存为 CSV 时可能是 GBK 编码，两种都兼容
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码: {path}")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn, table, col, decl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(reload_catalog: bool = False):
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT, active_ingredient TEXT, formulation TEXT,
            spec_amount REAL, spec_unit TEXT, package TEXT,
            price REAL, min_price REAL, stock INTEGER,
            toxicity TEXT, ph_property TEXT, restricted TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS registrations (
            product_id TEXT, crop TEXT, target TEXT,
            dose_min REAL, dose_max REAL, dose_unit TEXT,
            method TEXT, phi_days TEXT, max_times TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
            customer TEXT, phone TEXT, items_json TEXT, total REAL, guide TEXT
        );
        -- 销售明细：开单时把商品信息"拍快照"存下来，以后改了商品资料也不影响台账
        CREATE TABLE IF NOT EXISTS order_items (
            order_id INTEGER, product_id TEXT, name TEXT, reg_no TEXT, spec TEXT,
            manufacturer TEXT, qty INTEGER, unit_price REAL, amount REAL,
            crop TEXT, target TEXT, mu REAL, unit_cost REAL
        );
        -- 进货记录（采购台账）
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, product_id TEXT,
            name TEXT, reg_no TEXT, spec TEXT, manufacturer TEXT,
            qty INTEGER, unit_cost REAL, supplier TEXT, batch_no TEXT, prod_date TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
            reason TEXT, summary TEXT, status TEXT DEFAULT '待处理'
        );
        -- 化肥、种子、农具的用途和用量（农药用 registrations 表，规则更严）
        CREATE TABLE IF NOT EXISTS usage (
            product_id TEXT, crop TEXT, purpose TEXT,
            dose_min REAL, dose_max REAL, dose_unit TEXT, method TEXT, note TEXT
        );
        """
    )
    # 老版本数据库升级
    _add_column(conn, "products", "manufacturer", "TEXT DEFAULT ''")
    _add_column(conn, "products", "reg_no", "TEXT DEFAULT ''")
    _add_column(conn, "products", "category", "TEXT DEFAULT '农药'")
    _add_column(conn, "products", "stock_warning", "INTEGER NOT NULL DEFAULT 10")
    _add_column(conn, "products", "stock_target", "INTEGER NOT NULL DEFAULT 30")
    _add_column(conn, "orders", "status", "TEXT DEFAULT '正常'")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            product_id TEXT NOT NULL, name TEXT NOT NULL, package TEXT,
            kind TEXT NOT NULL, qty INTEGER NOT NULL, balance INTEGER NOT NULL,
            ref_id TEXT DEFAULT '', note TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_movements_date ON stock_movements(created_at);
        CREATE INDEX IF NOT EXISTS idx_movements_product ON stock_movements(product_id, id);
        CREATE TRIGGER IF NOT EXISTS product_opening_stock AFTER INSERT ON products
        BEGIN
            INSERT INTO stock_movements(created_at, product_id, name, package, kind, qty, balance, note)
            VALUES (datetime('now', 'localtime'), NEW.id, NEW.name, NEW.package,
                    '期初库存', NEW.stock, NEW.stock, '商品建档库存');
        END;
    """)
    conn.execute("BEGIN IMMEDIATE")
    # 旧库存作为升级时的期初余额，不伪造历史流水，也不重复记账。
    conn.execute("""INSERT INTO stock_movements
        (created_at, product_id, name, package, kind, qty, balance, note)
        SELECT ?, p.id, p.name, p.package, '期初库存', p.stock, p.stock, '升级时的库存余额'
        FROM products p WHERE NOT EXISTS
        (SELECT 1 FROM stock_movements m WHERE m.product_id=p.id)""", (now(),))
    _backfill_order_items(conn)

    # CSV 文件改过（Excel 编辑或后台保存）就自动重新导入，不用手动 --reload
    mtime = str(max((DATA / f).stat().st_mtime for f in CATALOG_FILES if (DATA / f).exists()))
    last = conn.execute("SELECT v FROM meta WHERE k='catalog_mtime'").fetchone()
    empty = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
    if reload_catalog or empty or not last or last[0] != mtime:
        import_catalog(conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('catalog_mtime', ?)", (mtime,))
    conn.commit()
    conn.close()


def import_catalog(conn):
    """从 CSV 导入商品：已有商品只更新资料和价格，不动库存；新商品按 CSV 里的库存建档。"""
    for r in _read_csv(DATA / "products.csv"):
        values = {f: (r.get(f) or "").strip() for f in CATALOG_FIELDS}
        for f in ("spec_amount", "price", "min_price"):
            values[f] = float(values[f])
        values["category"] = values["category"] or "农药"
        exists = conn.execute("SELECT 1 FROM products WHERE id=?", (r["id"],)).fetchone()
        if exists:
            conn.execute(
                f"UPDATE products SET {', '.join(f + '=?' for f in CATALOG_FIELDS)} WHERE id=?",
                [values[f] for f in CATALOG_FIELDS] + [r["id"]])
        else:
            conn.execute(
                f"INSERT INTO products (id, stock, {', '.join(CATALOG_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(CATALOG_FIELDS))})",
                [r["id"], int(float(r.get("stock") or 0))] + [values[f] for f in CATALOG_FIELDS])
    # 登记信息属于商品资料，整体替换
    conn.execute("DELETE FROM registrations")
    for r in _read_csv(DATA / "registrations.csv"):
        conn.execute(
            "INSERT INTO registrations VALUES (?,?,?,?,?,?,?,?,?)",
            (r["product_id"], r["crop"], r["target"],
             float(r["dose_min"]), float(r["dose_max"]), r["dose_unit"],
             r["method"], r["phi_days"], r["max_times"]),
        )
    conn.execute("DELETE FROM usage")
    if (DATA / "usage.csv").exists():
        for r in _read_csv(DATA / "usage.csv"):
            num = lambda v: float(v) if str(v or "").strip() else None
            conn.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
                (r["product_id"], (r.get("crop") or "通用").strip(), r["purpose"].strip(),
                 num(r.get("dose_min")), num(r.get("dose_max")), (r.get("dose_unit") or "").strip(),
                 (r.get("method") or "").strip(), (r.get("note") or "").strip()))


def _backfill_order_items(conn):
    """老版本只把订单明细存在 items_json 里，这里补进 order_items 表。"""
    done = {r[0] for r in conn.execute("SELECT DISTINCT order_id FROM order_items")}
    for o in conn.execute("SELECT id, items_json FROM orders").fetchall():
        if o["id"] in done:
            continue
        for it in json.loads(o["items_json"] or "[]"):
            p = conn.execute("SELECT * FROM products WHERE id=?", (it["product_id"],)).fetchone()
            if not p:
                continue
            price = float(it.get("unit_price") or p["price"])
            conn.execute(
                "INSERT INTO order_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["id"], p["id"], p["name"], "", spec_text(p), "",
                 int(it["qty"]), price, round(price * int(it["qty"]), 2),
                 it.get("crop", ""), it.get("target", ""), it.get("mu"), None))


def spec_text(p):
    return f"{p['spec_amount']:g}{p['spec_unit']}/{p['package']}"


def latest_cost(conn, product_id):
    """最近一次进价，没有进货记录返回 None。"""
    r = conn.execute("SELECT unit_cost FROM purchases WHERE product_id=? ORDER BY id DESC LIMIT 1",
                     (product_id,)).fetchone()
    return r[0] if r else None


def add_purchase(product_id, qty, unit_cost, supplier="", batch_no="", prod_date="", note=""):
    """进货入库：记采购台账 + 加库存。"""
    qty = stock_integer(qty, "进货数量", minimum=1)
    unit_cost = float(unit_cost)
    if not math.isfinite(unit_cost) or unit_cost < 0:
        raise ValueError("进价必须是大于等于0的有效数字")
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise ValueError(f"没有商品 {product_id}")
        cur = conn.execute(
            "INSERT INTO purchases(created_at, product_id, name, reg_no, spec, manufacturer, qty, "
            "unit_cost, supplier, batch_no, prod_date, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), p["id"], p["name"], p["reg_no"], spec_text(p), p["manufacturer"], qty,
             unit_cost, supplier, batch_no, prod_date, note))
        purchase_id = cur.lastrowid
        change_stock(conn, product_id, qty, "采购入库", purchase_id, note)
        return purchase_id


def void_order(order_id):
    """作废订单：库存加回去，台账里保留记录但标记作废。"""
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or o["status"] == "作废":
            return False
        for it in conn.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,)):
            change_stock(conn, it["product_id"], it["qty"], "作废退回", order_id)
        conn.execute("UPDATE orders SET status='作废' WHERE id=?", (order_id,))
        return True


def stock_integer(value, label="数量", minimum=0):
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
            raise ValueError
        if number < minimum:
            raise ValueError
        return int(number)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label}必须是大于等于{minimum}的整数") from None


def change_stock(conn, product_id, delta, kind, ref_id="", note=""):
    """调用方须持有写事务：库存与流水一起提交或回滚。"""
    delta = stock_integer(abs(delta), minimum=0) * (-1 if delta < 0 else 1)
    p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise ValueError(f"没有商品 {product_id}")
    balance = p["stock"] + delta
    if balance < 0:
        raise ValueError(f"{p['name']} 库存不足，现有 {p['stock']}{p['package']}")
    conn.execute("UPDATE products SET stock=? WHERE id=?", (balance, product_id))
    conn.execute("""INSERT INTO stock_movements
        (created_at, product_id, name, package, kind, qty, balance, ref_id, note)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (now(), product_id, p["name"], p["package"], kind, delta, balance, str(ref_id), note))
    return balance


def adjust_stock(product_id, actual_stock, reason, expected_stock):
    """盘点时校验页面上的原库存，避免覆盖盘点期间的新销售或入库。"""
    actual_stock = stock_integer(actual_stock, "实盘库存")
    if not str(reason).strip():
        raise ValueError("请填写盘点或报损原因")
    with closing(connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        p = conn.execute("SELECT stock FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise ValueError(f"没有商品 {product_id}")
        if p[0] != expected_stock:
            raise ValueError("库存已变化，请刷新页面并重新核对后再盘点")
        delta = actual_stock - p[0]
        if delta:
            change_stock(conn, product_id, delta, "盘点调增" if delta > 0 else "盘点调减",
                         note=str(reason).strip())
        return delta


def save_stock_settings(rows):
    with closing(connect()) as conn, conn:
        for row in rows:
            warning = stock_integer(row["stock_warning"], "预警线")
            target = stock_integer(row["stock_target"], "补货目标")
            if target < warning:
                raise ValueError("补货目标不能低于预警线")
            if conn.execute("UPDATE products SET stock_warning=?, stock_target=? WHERE id=?",
                            (warning, target, row["id"])).rowcount != 1:
                raise ValueError(f"没有商品 {row['id']}")


def inventory_alerts(conn):
    return conn.execute("""SELECT id, name, category, package, stock, stock_warning, stock_target,
        CASE WHEN stock <= 0 THEN '缺货' ELSE '库存偏低' END AS alert,
        MAX(stock_target-stock, 0) AS suggested_qty FROM products
        WHERE stock <= stock_warning ORDER BY stock <= 0 DESC, stock-stock_warning, id""").fetchall()


def inventory_report(conn, start, end):
    """日期含首尾；采购和有效销售分别汇总后关联，避免明细多对多重复计数。"""
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    bounds = (start.isoformat(), (end + timedelta(days=1)).isoformat())
    return conn.execute("""
        WITH buys AS (
            SELECT product_id, SUM(qty) bought, SUM(qty*unit_cost) purchase_amount
            FROM purchases WHERE created_at >= ? AND created_at < ? GROUP BY product_id
        ), sales AS (
            SELECT oi.product_id, SUM(oi.qty) sold, SUM(oi.amount) sales_amount,
                SUM(CASE WHEN oi.unit_cost IS NOT NULL THEN oi.amount-oi.qty*oi.unit_cost ELSE 0 END) profit,
                SUM(CASE WHEN oi.unit_cost IS NULL THEN oi.qty ELSE 0 END) unknown_cost_qty
            FROM order_items oi JOIN orders o ON o.id=oi.order_id
            WHERE o.status != '作废' AND o.created_at >= ? AND o.created_at < ? GROUP BY oi.product_id
        ) SELECT p.id, p.name, p.category, p.package, p.stock,
            COALESCE(b.bought,0) bought, ROUND(COALESCE(b.purchase_amount,0),2) purchase_amount,
            COALESCE(s.sold,0) sold, ROUND(COALESCE(s.sales_amount,0),2) sales_amount,
            ROUND(COALESCE(s.profit,0),2) profit, COALESCE(s.unknown_cost_qty,0) unknown_cost_qty
        FROM products p LEFT JOIN buys b ON b.product_id=p.id LEFT JOIN sales s ON s.product_id=p.id
        ORDER BY sales_amount DESC, p.id
        """, bounds + bounds).fetchall()


def export_catalog_csv():
    """网页后台改了商品资料后，同步写回 CSV，保证 Excel 和网页两边一致。旧文件备份为 .bak。"""
    conn = connect()
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    regs = conn.execute("SELECT * FROM registrations ORDER BY product_id").fetchall()
    usage = conn.execute("SELECT * FROM usage ORDER BY product_id").fetchall()
    conn.close()
    for name, rows, fields in [
        ("products.csv", products, ["id"] + CATALOG_FIELDS + ["stock"]),
        ("registrations.csv", regs, ["product_id", "crop", "target", "dose_min", "dose_max",
                                     "dose_unit", "method", "phi_days", "max_times"]),
        ("usage.csv", usage, USAGE_FIELDS),
    ]:
        path = DATA / name
        if path.exists():
            path.replace(path.with_suffix(".csv.bak"))
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(fields)
            for r in rows:
                w.writerow([r[k] if r[k] is not None else "" for k in fields])


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False)
