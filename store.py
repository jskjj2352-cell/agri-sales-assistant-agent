"""数据层：SQLite 存商品、登记信息、订单、进货、台账。

商品资料的两种维护方式：
  1. 老板后台网页（推荐）：改价格、新增商品、进货入库
  2. Excel 编辑 data/products.csv 和 data/registrations.csv，再运行 python agent.py --reload
     重新导入只更新商品资料和价格，【不会改动已有商品的库存】；库存只通过进货和销售变化。
"""
import csv
import json
import sqlite3
from datetime import datetime
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
    _add_column(conn, "orders", "status", "TEXT DEFAULT '正常'")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
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
    if qty <= 0:
        raise ValueError("进货数量必须大于0")
    conn = connect()
    p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        conn.close()
        raise ValueError(f"没有商品 {product_id}")
    conn.execute(
        "INSERT INTO purchases(created_at, product_id, name, reg_no, spec, manufacturer, qty, "
        "unit_cost, supplier, batch_no, prod_date, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), p["id"], p["name"], p["reg_no"], spec_text(p), p["manufacturer"], int(qty),
         float(unit_cost), supplier, batch_no, prod_date, note))
    conn.execute("UPDATE products SET stock = stock + ? WHERE id=?", (int(qty), product_id))
    conn.commit()
    conn.close()


def void_order(order_id):
    """作废订单：库存加回去，台账里保留记录但标记作废。"""
    conn = connect()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["status"] == "作废":
        conn.close()
        return False
    for it in conn.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,)):
        conn.execute("UPDATE products SET stock = stock + ? WHERE id=?", (it["qty"], it["product_id"]))
    conn.execute("UPDATE orders SET status='作废' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    return True


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
