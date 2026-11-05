"""Agent 可调用的工具。

设计原则：
- 价格、库存、用量全部来自数据库，模型只负责调用和解释，不允许自己编。
- 底价(min_price)永远不返回给模型，避免被说漏嘴；砍价是否允许由 quote 工具判定。
- 用药说明由程序根据登记数据生成，不让模型自由发挥。
- 农户的俗称（土豆、腻虫、钻心虫……）自动转换成标准名称再匹配。
"""
import math

from store import connect, now, to_json, BASE, spec_text, latest_cost

ORDERS_DIR = BASE / "orders"

# ---------------- 俗称 -> 标准名称（可以继续往里加） ----------------
CROP_ALIASES = {
    "土豆": "马铃薯", "洋芋": "马铃薯", "地蛋": "马铃薯",
    "西红柿": "番茄", "洋柿子": "番茄",
    "包菜": "甘蓝", "圆白菜": "甘蓝", "卷心菜": "甘蓝", "大头菜": "甘蓝", "洋白菜": "甘蓝",
    "苞米": "玉米", "棒子": "玉米", "玉蜀黍": "玉米",
    "胡瓜": "黄瓜",
    "白菜": "大白菜", "黄芽菜": "大白菜",
}
TARGET_ALIASES = {
    "腻虫": "蚜虫", "蜜虫": "蚜虫", "油汗": "蚜虫",
    "小白蛾": "白粉虱", "小白蛾子": "白粉虱", "白飞虱": "白粉虱",
    "钻心虫": "玉米螟",
    "粘虫": "黏虫",
    "吊丝虫": "小菜蛾", "两头尖": "小菜蛾",
    "菜虫": "菜青虫",
    "潜叶蝇": "斑潜蝇", "鬼画符": "斑潜蝇",
    "白地蚕": "蛴螬", "地蚕": "蛴螬", "土蚕": "蛴螬", "地下害虫": "蛴螬",
    "根上长疙瘩": "根结线虫", "根瘤": "根结线虫", "线虫": "根结线虫",
    "灰霉": "灰霉病", "白粉": "白粉病", "霜霉": "霜霉病", "角斑病": "细菌性角斑病",
    "花叶病": "病毒病", "病毒": "病毒病",
    # 除草：统一对应到"杂草"，能匹配到苗前、苗后、阔叶、禾本科所有除草剂，再由模型按情况选
    "草": "杂草", "除草": "杂草", "一年生杂草": "杂草",
    "阔叶草": "阔叶杂草", "禾本科草": "禾本科杂草", "狗尾草": "禾本科杂草", "马唐": "禾本科杂草",
}

# 有机磷类有效成分（与烟嘧磺隆等磺酰脲类除草剂间隔使用会产生药害）
ORGANOPHOSPHATES = {"毒死蜱", "辛硫磷", "敌敌畏", "马拉硫磷", "乐果", "丙溴磷", "三唑磷"}
SULFONYLUREAS = {"烟嘧磺隆", "砜嘧磺隆", "噻吩磺隆"}


def norm_crop(c: str) -> str:
    c = (c or "").strip()
    return CROP_ALIASES.get(c, c)


def norm_target(t: str) -> str:
    t = (t or "").strip()
    return TARGET_ALIASES.get(t, t)


def _get_product(conn, pid):
    return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


def _get_reg(conn, pid, crop, target):
    """精确查找某个药在某作物、某防治对象上的登记。先按原名找，找不到再按俗称转换后的名字找。"""
    crop = norm_crop(crop)
    raw = (target or "").strip()
    for t in dict.fromkeys([raw, norm_target(raw)]):
        r = conn.execute(
            "SELECT * FROM registrations WHERE product_id=? AND crop=? AND target=?",
            (pid, crop, t)).fetchone()
        if r:
            return r
    return None


def _plan(p, reg, mu=None):
    """把一个 商品+登记用途 整理成方案；给了亩数就算好数量和价格。"""
    d = {
        "product_id": p["id"],
        "商品": p["name"],
        "防治对象": reg["target"],
        "每亩用量": f"{reg['dose_min']:g}-{reg['dose_max']:g} {reg['dose_unit']}",
        "施药方法": reg["method"],
        "安全间隔期(天)": reg["phi_days"] or "无/见标签",
        "每季最多次数": reg["max_times"],
        "单价": p["price"],
        "包装": f"{p['spec_amount']:g}{p['spec_unit']}/{p['package']}",
        "库存": p["stock"],
        "毒性": p["toxicity"],
        "注意": p["notes"],
    }
    if mu:
        qty = math.ceil(reg["dose_max"] * mu / p["spec_amount"])  # 按上限备货
        d.update({
            "亩数": mu,
            "总用药量": f"{reg['dose_min'] * mu:g}-{reg['dose_max'] * mu:g} {p['spec_unit']}",
            "建议购买数量": qty,
            "小计": round(qty * p["price"], 2),
            "库存够不够": "够" if p["stock"] >= qty else f"不够，只有{p['stock']}{p['package']}",
        })
    return d


def _phi_conflict(reg, days_to_harvest):
    """离采收的天数小于安全间隔期时返回提示，否则返回 None。"""
    if days_to_harvest is None or not str(reg["phi_days"]).strip():
        return None
    try:
        phi = int(float(reg["phi_days"]))
    except ValueError:
        return None
    if phi > days_to_harvest:
        return f"安全间隔期{phi}天，但离采收只有{days_to_harvest:g}天，现在打会农药残留超标"
    return None


# ---------------- 工具实现 ----------------

def match_products(crop: str, candidates: list, mu: float = None, days_to_harvest: float = None):
    """根据可能的病虫害，自动匹配店里有货且登记过的药，并直接算好数量价格。"""
    crop = norm_crop(crop)
    conn = connect()
    results, unmatched = [], []
    for raw in candidates:
        t = norm_target(raw)
        rows = conn.execute(
            """SELECT r.product_id, r.target FROM registrations r
               JOIN products p ON p.id = r.product_id
               WHERE r.crop = ? AND p.stock > 0 AND p.category = '农药'
                 AND (r.target LIKE ? OR ? LIKE '%' || r.target || '%')
               ORDER BY p.price""",
            (crop, f"%{t}%", t),
        ).fetchall()
        plans, blocked = [], []
        for row in rows:
            p = _get_product(conn, row["product_id"])
            reg = _get_reg(conn, row["product_id"], crop, row["target"])
            conflict = _phi_conflict(reg, days_to_harvest)
            if conflict:
                blocked.append({"商品": p["name"], "不能用的原因": conflict})
            else:
                plans.append(_plan(p, reg, mu))
        if plans or blocked:
            entry = {"可能的问题": raw, "店里能用的药": plans}
            if blocked:
                entry["快采收了不能用的药"] = blocked
            results.append(entry)
        else:
            unmatched.append(raw)

    out = {"作物": crop, "匹配结果": results}
    if unmatched:
        can_treat = conn.execute(
            """SELECT DISTINCT r.target FROM registrations r JOIN products p ON p.id=r.product_id
               WHERE r.crop=? AND p.stock>0""", (crop,)).fetchall()
        out["店里没有对症药的问题"] = unmatched
        out["这个作物店里能治的问题"] = [r[0] for r in can_treat]
        out["提示"] = "没有对症药的问题不能推荐别的药凑数，可以告诉客户店里暂时没货，并给老板一个进货建议。"
    conn.close()
    return out


def _goods_plan(p, u, mu=None):
    """化肥/种子/农具的方案：有亩用量且给了亩数就算数量，否则默认买 1 个。"""
    d = {
        "product_id": p["id"], "商品": p["name"], "类别": p["category"],
        "包装": f"{p['spec_amount']:g}{p['spec_unit']}/{p['package']}",
        "单价": p["price"], "库存": p["stock"], "注意": p["notes"],
    }
    if u:
        d["适用"] = u["crop"]
        d["用途"] = u["purpose"]
        if u["method"]:
            d["用法"] = u["method"]
        if u["note"]:
            d["说明"] = u["note"]
    if u and u["dose_max"]:
        d["每亩用量"] = f"{u['dose_min']:g}-{u['dose_max']:g} {u['dose_unit']}"
        if mu:
            unit = (u["dose_unit"] or "").split("/")[0]
            if unit != p["spec_unit"]:
                d["提示"] = "用量单位和包装单位不一致，没法自动算数量"
            else:
                qty = math.ceil(u["dose_max"] * mu / p["spec_amount"])
                d.update({"亩数": mu, "建议购买数量": qty, "小计": round(qty * p["price"], 2),
                          "库存够不够": "够" if p["stock"] >= qty else f"不够，只有{p['stock']}{p['package']}"})
    elif not (u and u["dose_max"]):
        d.update({"建议购买数量": 1, "小计": p["price"]})
    return d


def _keywords(text):
    for sep in "，,、；; /":
        text = (text or "").replace(sep, " ")
    return [w for w in text.split() if w]


def match_goods(need: str, crop: str = "", category: str = "", mu: float = None):
    """化肥、种子、农具：按作物和需求（底肥、追肥、缺钙、播种、喷雾器……）匹配店里有货的商品。"""
    crop = norm_crop(crop)
    words = _keywords(need)
    conn = connect()
    sql = """SELECT u.*, p.id AS pid FROM usage u JOIN products p ON p.id = u.product_id
             WHERE p.category != '农药' AND p.stock > 0"""
    args = []
    if crop:
        sql += " AND (u.crop = ? OR u.crop = '通用')"
        args.append(crop)
    if category:
        sql += " AND p.category = ?"
        args.append(category)
    rows = conn.execute(sql, args).fetchall()

    results, seen = [], set()
    for u in sorted(rows, key=lambda r: r["crop"] == "通用"):  # 专用的排前面，通用的排后面
        p = _get_product(conn, u["pid"])
        text = f"{u['purpose']} {p['name']} {p['notes']}"
        if words and not any(w in text for w in words):
            continue
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        results.append(_goods_plan(p, u, mu))

    if not results and words:  # 用途表里没有，再按商品名称找
        for p in conn.execute("SELECT * FROM products WHERE category != '农药' AND stock > 0"):
            if any(w in f"{p['name']} {p['notes']}" for w in words) and (not category or p["category"] == category):
                results.append(_goods_plan(p, None, mu))
    conn.close()
    out = {"作物": crop or "不限", "需求": need, "店里能用的商品": results}
    if not results:
        out["提示"] = "店里没有合适的商品，不要拿别的凑数；可以给老板一个进货建议。"
    return out


def calc_dosage(product_id: str, crop: str, target: str, mu: float):
    conn = connect()
    p = _get_product(conn, product_id)
    reg = _get_reg(conn, product_id, crop, target)
    conn.close()
    if not p:
        return {"error": f"没有商品 {product_id}"}
    if not reg:
        return {"error": f"{p['name']} 没有登记用于【{crop}·{target}】，禁止推荐。"}
    if mu <= 0:
        return {"error": "亩数必须大于0"}
    return _plan(p, reg, mu)


def check_mix(product_ids: list):
    conn = connect()
    prods = [_get_product(conn, pid) for pid in product_ids]
    conn.close()
    if any(p is None for p in prods):
        return {"error": "有商品ID不存在"}
    problems = []
    ais = [p["active_ingredient"] for p in prods]
    if len(set(ais)) < len(ais):
        problems.append("有效成分重复，等于加大剂量，容易产生药害。")
    alkaline = [p["name"] for p in prods if p["ph_property"] == "碱性"]
    if alkaline and len(prods) > 1:
        problems.append(f"{'、'.join(alkaline)} 是碱性药剂，不能和其他大多数农药混用。")
    if set(ais) & SULFONYLUREAS and set(ais) & ORGANOPHOSPHATES:
        problems.append("磺酰脲类除草剂（如烟嘧磺隆）不能与有机磷农药混用，前后7天内也不能用。")
    if problems:
        return {"可以混用": False, "问题": problems}
    return {
        "可以混用": "未发现已知禁忌",
        "提醒": "先用少量药液试配（二次稀释法），无沉淀、絮状、分层再大面积使用。",
    }


def quote(items: list):
    """items: [{product_id, qty, unit_price(可选，砍价后的单价)}]"""
    conn = connect()
    lines, total, errors = [], 0.0, []
    for it in items:
        p = _get_product(conn, it["product_id"])
        if not p:
            errors.append(f"没有商品 {it['product_id']}")
            continue
        qty = int(it["qty"])
        unit_price = float(it.get("unit_price") or p["price"])
        if unit_price < p["min_price"]:
            errors.append(f"{p['name']} 报价 {unit_price} 元低于店里允许的最低价，不能卖。"
                          f"可以稍微让一点，但不能到这个价。")
            continue
        if qty > p["stock"]:
            errors.append(f"{p['name']} 库存只有 {p['stock']}{p['package']}，不够 {qty}。")
            continue
        sub = round(unit_price * qty, 2)
        total += sub
        lines.append({"product_id": p["id"], "商品": p["name"], "数量": qty,
                      "单价": unit_price, "小计": sub})
    conn.close()
    return {"ok": not errors, "明细": lines, "合计": round(total, 2), "问题": errors}


def _find_usage(conn, pid, crop, purpose):
    """找化肥/种子/农具在这个作物（或通用）、这个用途上的用法，找不到返回 None。"""
    rows = conn.execute("SELECT * FROM usage WHERE product_id=? AND crop IN (?, '通用') "
                        "ORDER BY crop = '通用'", (pid, norm_crop(crop))).fetchall()
    words = _keywords(purpose)
    for u in rows:
        if not words or any(w in u["purpose"] for w in words):
            return u
    return rows[0] if rows else None


def create_order(items: list, customer: str = "", phone: str = ""):
    """items: [{product_id, qty, unit_price(可选), crop, target, mu}]
    农药的 target 是防治对象（必须有登记）；化肥/种子/农具的 target 是用途（底肥、播种等，可不填）。"""
    q = quote(items)
    if not q["ok"]:
        return {"error": "报价校验没有通过，不能开单", "问题": q["问题"]}
    conn = connect()
    for it in items:
        p = _get_product(conn, it["product_id"])
        if p["category"] != "农药":
            continue
        if p["restricted"] == "是" and not (customer and phone):
            conn.close()
            return {"error": f"{p['name']} 是限制使用农药，法律要求实名购买，请问客户姓名和电话。"}
        if not _get_reg(conn, it["product_id"], it.get("crop", ""), it.get("target", "")):
            conn.close()
            return {"error": f"{p['name']} 没有登记用于【{it.get('crop')}·{it.get('target')}】，不能开单。"}

    guide = _build_guide(conn, items, customer, q["合计"])
    for it in items:
        conn.execute("UPDATE products SET stock = stock - ? WHERE id=?",
                     (int(it["qty"]), it["product_id"]))
    cur = conn.execute(
        "INSERT INTO orders(created_at, customer, phone, items_json, total, guide, status) "
        "VALUES (?,?,?,?,?,?,'正常')",
        (now(), customer, phone, to_json(items), q["合计"], guide),
    )
    order_id = cur.lastrowid
    # 销售台账明细：把商品信息和当时的进价存成快照
    prices = {line["product_id"]: line["单价"] for line in q["明细"]}
    for it in items:
        p = _get_product(conn, it["product_id"])
        if p["category"] == "农药":
            reg = _get_reg(conn, p["id"], it.get("crop", ""), it.get("target", ""))
            crop, target = reg["crop"], reg["target"]
        else:
            crop, target = norm_crop(it.get("crop", "")), (it.get("target") or "").strip()
        qty = int(it["qty"])
        price = prices[p["id"]]
        conn.execute(
            "INSERT INTO order_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, p["id"], p["name"], p["reg_no"], spec_text(p), p["manufacturer"],
             qty, price, round(price * qty, 2), crop, target,
             it.get("mu"), latest_cost(conn, p["id"])))
    conn.commit()
    conn.close()

    ORDERS_DIR.mkdir(exist_ok=True)
    path = ORDERS_DIR / f"订单{order_id}_使用说明.txt"
    path.write_text(guide, encoding="utf-8")
    return {"ok": True, "订单号": order_id, "合计": q["合计"], "用药说明文件": str(path)}


def _build_guide(conn, items, customer, total):
    lines = ["========== 使用说明 ==========", f"日期：{now()}"]
    if customer:
        lines.append(f"客户：{customer}")
    has_pesticide = False
    for i, it in enumerate(items, 1):
        p = _get_product(conn, it["product_id"])
        lines += ["", f"【{i}】{p['name']}  × {it['qty']}{p['package']}"]
        mu = f"（{float(it['mu']):g} 亩）" if it.get("mu") else ""
        if p["category"] == "农药":
            has_pesticide = True
            reg = _get_reg(conn, it["product_id"], it["crop"], it["target"])
            lines += [
                f"  用于：{reg['crop']} · {reg['target']}{mu}",
                f"  每亩用量：{reg['dose_min']:g}-{reg['dose_max']:g} {reg['dose_unit']}，{reg['method']}",
                f"  安全间隔期：{reg['phi_days'] or '见标签'} 天（最后一次用药到采收至少隔这么久）",
                f"  每季最多用：{reg['max_times']} 次",
            ]
        else:
            u = _find_usage(conn, p["id"], it.get("crop", ""), it.get("target", ""))
            what = " · ".join(x for x in (norm_crop(it.get("crop", "")), it.get("target", "")) if x)
            if what:
                lines.append(f"  用于：{what}{mu}")
            if u and u["dose_max"]:
                lines.append(f"  每亩用量：{u['dose_min']:g}-{u['dose_max']:g} {u['dose_unit']}"
                             + (f"，{u['method']}" if u["method"] else ""))
            elif u and u["method"]:
                lines.append(f"  用法：{u['method']}")
            if u and u["note"]:
                lines.append(f"  说明：{u['note']}")
        lines.append(f"  注意：{p['notes']}")
    lines += ["", f"合计：{total} 元"]
    if has_pesticide:
        lines += [
            "",
            "用药安全提示：",
            "  1. 用药前请仔细阅读包装标签，以标签为准；",
            "  2. 施药时穿长衣长裤、戴口罩手套，不要逆风喷药；",
            "  3. 避开中午高温和下雨前后施药；",
            "  4. 空包装不要随意丢弃，不要污染水源。",
        ]
    lines.append("================================")
    return "\n".join(lines)


# ---------------- 给模型的工具定义 ----------------

_item = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string"},
        "qty": {"type": "integer", "description": "数量（袋/瓶）"},
        "unit_price": {"type": "number", "description": "成交单价；不砍价就不填"},
    },
    "required": ["product_id", "qty"],
}

TOOLS = [
    {
        "name": "match_products",
        "description": "【治病虫草害用】根据作物和你判断的1-3个可能病虫草害，自动匹配店里有货、登记过的农药，"
                       "给了亩数会直接算好买几袋/瓶和价格。农户俗称（土豆、腻虫等）可以直接传。",
        "input_schema": {
            "type": "object",
            "properties": {
                "crop": {"type": "string", "description": "作物，如 玉米、土豆、番茄、黄瓜、包菜"},
                "candidates": {"type": "array", "items": {"type": "string"},
                               "description": "可能的病虫草害，最可能的放第一个，如 [\"晚疫病\", \"早疫病\"]"},
                "mu": {"type": "number", "description": "亩数，知道就填"},
                "days_to_harvest": {"type": "number",
                                    "description": "离采收还有几天。蔬菜等快要采收时一定要填，系统会排除安全间隔期不够的药"},
            },
            "required": ["crop", "candidates"],
        },
    },
    {
        "name": "match_goods",
        "description": "【买化肥、种子、农具用】按作物和需求匹配店里有货的商品，给了亩数会算好买几袋和价格。"
                       "缺素症状（缺钙、缺硼、缺钾）也用这个找叶面肥。",
        "input_schema": {
            "type": "object",
            "properties": {
                "need": {"type": "string",
                         "description": "需求关键词，可多个用空格隔开，如 \"底肥\"、\"追肥\"、\"冲施 膨果\"、"
                                        "\"缺钙\"、\"播种 春播\"、\"喷雾器\"、\"地膜\"、\"黄板 蚜虫\""},
                "crop": {"type": "string", "description": "作物；农具等不分作物的可以不填"},
                "category": {"type": "string", "enum": ["化肥", "种子", "农具"],
                             "description": "限定类别，可不填"},
                "mu": {"type": "number", "description": "亩数，知道就填"},
            },
            "required": ["need"],
        },
    },
    {
        "name": "calc_dosage",
        "description": "亩数变了或换药时，重新计算某个药的数量和价格。",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "crop": {"type": "string"},
                "target": {"type": "string"},
                "mu": {"type": "number", "description": "亩数"},
            },
            "required": ["product_id", "crop", "target", "mu"],
        },
    },
    {
        "name": "check_mix",
        "description": "推荐两种及以上药一起用时，检查能不能混用。",
        "input_schema": {
            "type": "object",
            "properties": {"product_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["product_ids"],
        },
    },
    {
        "name": "quote",
        "description": "客户砍价时，把砍后的单价填进 unit_price，由系统判断能不能卖。",
        "input_schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": _item}},
            "required": ["items"],
        },
    },
    {
        "name": "create_order",
        "description": "店员说客户要了/买了，就直接开单、扣库存、生成使用说明。农药、化肥、种子、农具可以开在同一单里。"
                       "客户姓名电话没有可以不填。",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            **_item["properties"],
                            "crop": {"type": "string", "description": "作物（农药必填）"},
                            "target": {"type": "string",
                                       "description": "农药填防治对象（必填）；化肥种子农具填用途，如 底肥、播种"},
                            "mu": {"type": "number"},
                        },
                        "required": ["product_id", "qty"],
                    },
                },
                "customer": {"type": "string"},
                "phone": {"type": "string"},
            },
            "required": ["items"],
        },
    },
]

TOOL_FUNCS = {
    "match_products": match_products,
    "match_goods": match_goods,
    "calc_dosage": calc_dosage,
    "check_mix": check_mix,
    "quote": quote,
    "create_order": create_order,
}


def run_tool(name: str, args: dict):
    if name not in TOOL_FUNCS:
        return {"error": f"没有这个工具：{name}"}
    try:
        return TOOL_FUNCS[name](**args)
    except Exception as e:  # 工具出错也要告诉模型，而不是让程序崩掉
        return {"error": f"{type(e).__name__}: {e}"}
