"""农资销售助理 —— 网页版（平板/手机可用）
文字对话用 DeepSeek，拍照看图用通义千问 Qwen-VL（可选）。

启动：双击 启动.bat
平板连同一个 WiFi，浏览器打开 http://电脑IP:8501
"""
import os
from datetime import date
from pathlib import Path

import streamlit as st
from openai import APIError

import agent
import vision
from store import connect, init_db

st.set_page_config(page_title="农资销售助理", page_icon="🌱", layout="centered")

TOOL_LABELS = {
    "match_products": "🔍 匹配店里的药",
    "match_goods": "🔍 匹配店里的商品",
    "calc_dosage": "🧮 计算用量",
    "check_mix": "⚗️ 检查混用",
    "quote": "💰 核价",
    "create_order": "🧾 开单",
}

CROPS = ["玉米", "土豆", "番茄", "黄瓜", "包菜", "白菜", "其他"]
SYMPTOMS = {
    "玉米": ["叶子上有长条形大斑", "心叶被吃烂、有虫粪", "茎秆有虫眼、棒子被咬",
             "叶子被吃得只剩叶脉（黏虫）", "苗被咬断、根被吃（地下害虫）",
             "要除草：还没出苗", "要除草：已经出苗"],
    "土豆": ["叶子有黑褐色斑，背面有白霉", "叶子有一圈圈轮纹的褐斑", "叶子背面有小绿虫",
             "块茎被咬出洞（地下害虫）", "茎基部发黑腐烂", "要除草"],
    "番茄": ["叶子有一圈圈轮纹的褐斑", "叶子、果实上有水浸状大斑，有白霉",
             "花、果实上长灰毛（灰霉）", "叶子背面有褐色或灰绿色霉层", "叶子背面有小白蛾子",
             "叶子皱缩、花叶、植株矮小", "根上长疙瘩"],
    "黄瓜": ["叶子有多角形黄斑，背面有黑霉", "叶子有多角形斑，背面有白色菌脓",
             "叶子上有一层白粉", "叶子背面有小绿虫", "叶子上有弯弯曲曲的白道",
             "瓜、花上长灰毛（灰霉）", "根上长疙瘩"],
    "包菜": ["叶子被吃出小洞，有小青虫", "叶子被吃得只剩网", "有绿色的大青虫",
             "有小绿虫（腻虫）", "烂心"],
    "白菜": ["有绿色的大青虫", "菜帮子腐烂发臭", "叶子背面有白霉", "有小绿虫（腻虫）"],
    "其他": [],
}
MODES = {"🐛 治病虫草": "治病、治虫、除草", "🌾 化肥": "买化肥", "🌱 种子": "买种子",
         "🔧 农具": "买农具"}
NEEDS = {
    "🌾 化肥": ["底肥（还没种）", "追肥（已经长起来了）", "冲施（随水浇）", "叶面肥",
               "叶子发黄、长得慢", "果子烂屁股（脐腐）、白菜干烧心", "裂果、花而不实",
               "改良土壤（连作、板结）"],
    "🌱 种子": ["春播", "夏播", "早熟", "高产", "抗病", "大棚种", "露地种"],
    "🔧 农具": ["电动喷雾器", "手动喷雾器", "打药防护（口罩手套）", "量杯", "地膜", "滴灌带",
               "黄板（诱蚜虫、白粉虱）", "蓝板（诱蓟马）"],
}
PLACEHOLDERS = {
    "🐛 治病虫草": "比如：最近老下雨 / 已经打过多菌灵 / 种的是甜玉米",
    "🌾 化肥": "比如：沙土地 / 去年种的也是土豆 / 想少花钱",
    "🌱 种子": "比如：要种在麦茬地 / 想要穗大的 / 要卖鲜食",
    "🔧 农具": "比如：要打除草剂用 / 大棚里用",
}
MAX_PHOTOS = 3


@st.cache_resource
def get_client():
    init_db()
    return agent.make_client()


@st.cache_resource
def get_vision_client():
    return vision.make_vision_client()


def show_guide(guide: str, key: str):
    with st.expander("🧾 使用说明（给客户）", expanded=True):
        st.code(guide, language=None)
        st.download_button("下载/打印使用说明", guide.encode("utf-8"),
                           file_name="使用说明.txt", key=key)


def show_images(images):
    if images:
        cols = st.columns(len(images))
        for c, img in zip(cols, images):
            c.image(img)


# ---------------- 状态 ----------------
if not os.environ.get("DEEPSEEK_API_KEY"):
    st.error("没有设置 DEEPSEEK_API_KEY。请把 Key 放进 api_key.txt，再用 启动.bat 启动。")
    st.stop()

client = get_client()
vclient = get_vision_client()
ss = st.session_state
if "messages" not in ss:
    ss.messages = agent.new_conversation()  # 发给模型的完整对话
    ss.display = []                         # 页面上显示的内容
ss.setdefault("form_id", 0)                 # 换客户时用新的 key 清空上传的照片
ss.setdefault("current_crop", "")           # 记住作物，补拍照片时也能用


def new_customer():
    ss.messages = agent.new_conversation()
    ss.display = []
    ss.form_id += 1
    ss.current_crop = ""
    for k in ["mode", "crop", "mu", "harvest", "extra"] + [f"sym_{c}" for c in CROPS] \
            + [f"need_{m}" for m in MODES]:
        ss.pop(k, None)


def send(text, images=None):
    ss.pending = text
    ss.pending_images = images or []


def submit_form():
    mode = ss.get("mode")
    crop = ss.get("crop") if mode != "🔧 农具" else ""
    if mode != "🔧 农具" and not crop:
        ss.form_error = "请先选作物"
        return
    ss.current_crop = crop
    photos = ss.get(f"photos_{ss.form_id}") or []
    images = [vision.compress(f.getvalue()) for f in photos[:MAX_PHOTOS]]
    key = f"sym_{crop}" if mode == "🐛 治病虫草" else f"need_{mode}"
    picks = list(ss.get(key) or [])
    extra = (ss.get("extra") or "").strip()
    if not picks and not extra and not images:
        ss.form_error = "请点选一项、写几句，或者拍张照片"
        return
    mu = ss.get("mu") or 0
    parts = [f"客户要：{MODES[mode]}"]
    if crop:
        parts.append(f"作物：{crop}")
    parts.append(f"面积：{mu:g}亩" if mu else "面积：不知道")
    if mode == "🐛 治病虫草" and ss.get("harvest"):
        parts.append(f"离采收还有{ss.harvest}天")
    if picks:
        parts.append(("症状：" if mode in ("🐛 治病虫草",) else "需求：") + "；".join(picks))
    if extra:
        parts.append("补充：" + extra)
    if images:
        parts.append(f"拍了{len(images)}张照片")
    send("，".join(parts), images)


# ---------------- 侧边栏 ----------------
with st.sidebar:
    st.button("🆕 下一位客户", on_click=new_customer, width="stretch", type="primary")
    conn = connect()
    today = date.today().isoformat()
    cnt, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders "
        "WHERE created_at LIKE ? AND status != '作废'",
        (f"{today}%",)).fetchone()
    st.metric("今日销售", f"{total:g} 元", f"{cnt} 单", delta_color="off")
    with st.expander("🧾 今日订单"):
        for o in conn.execute(
                "SELECT * FROM orders WHERE created_at LIKE ? ORDER BY id DESC", (f"{today}%",)):
            void = "　~~已作废~~" if o["status"] == "作废" else ""
            st.markdown(f"#{o['id']}　{o['created_at'][11:16]}　{o['customer'] or '散客'}　**{o['total']:g} 元**{void}")
    with st.expander("📦 库存"):
        rows = conn.execute("SELECT name, price, stock, package FROM products ORDER BY stock").fetchall()
        st.dataframe(
            [{"商品": r["name"], "售价": r["price"], "库存": f"{r['stock']}{r['package']}",
              "提醒": "⚠️ 快没了" if r["stock"] < 10 else ""} for r in rows],
            hide_index=True, width="stretch")
    conn.close()
    st.caption(f"对话模型：{agent.MODEL}")
    st.caption(f"看图模型：{vision.MODEL}" if vclient else "看图：未开启（缺 qwen_key.txt）")

# ---------------- 主界面 ----------------
st.title("🌱 农资销售助理")

if not ss.display:
    # 第一步：点选，不用打字
    st.subheader("客户要干什么？")
    mode = st.pills("类型", list(MODES), key="mode", label_visibility="collapsed")
    crop = None
    if mode and mode != "🔧 农具":
        st.subheader("① 什么作物？")
        crop = st.pills("作物", CROPS, key="crop", label_visibility="collapsed")
    if mode == "🔧 农具" or crop:
        step = iter("②③④⑤⑥")
        if vclient and mode in ("🐛 治病虫草", "🌾 化肥"):
            st.subheader(f"{next(step)} 拍照（推荐）" if mode == "🐛 治病虫草"
                         else f"{next(step)} 拍照（叶子发黄、长得不好时推荐）")
            st.file_uploader(
                f"拍病叶正面、背面特写，最多{MAX_PHOTOS}张", type=["jpg", "jpeg", "png", "webp"],
                accept_multiple_files=True, key=f"photos_{ss.form_id}")
        if mode == "🐛 治病虫草":
            st.subheader(f"{next(step)} 什么症状？（可多选" + ("，拍了照可不选）" if vclient else "）"))
            if SYMPTOMS.get(crop):
                st.pills("症状", SYMPTOMS[crop], key=f"sym_{crop}", selection_mode="multi",
                         label_visibility="collapsed")
        else:
            st.subheader(f"{next(step)} 要什么？（可多选）")
            st.pills("需求", NEEDS[mode], key=f"need_{mode}", selection_mode="multi",
                     label_visibility="collapsed")
        st.text_input("其他情况（可不填）", key="extra", placeholder=PLACEHOLDERS[mode])
        st.subheader(f"{next(step)} 种了几亩？" + ("（买地膜、滴灌带要填）" if mode == "🔧 农具" else ""))
        st.number_input("亩数", min_value=0.0, step=1.0, key="mu",
                        label_visibility="collapsed", help="不知道就填0")
        if mode == "🐛 治病虫草":
            st.number_input("离采收还有几天？（已经在采收填 1，不知道填 0）", min_value=0, step=1,
                            key="harvest")
        st.button("🔍 出方案", on_click=submit_form, type="primary", width="stretch")
        if ss.get("form_error"):
            st.warning(ss.pop("form_error"))
else:
    for i, m in enumerate(ss.display):
        with st.chat_message(m["role"]):
            show_images(m.get("images"))
            if m.get("vision"):
                with st.expander("📷 看图结果"):
                    st.markdown(m["vision"])
            if m.get("tools"):
                st.caption("　".join(m["tools"]))
            if m.get("text"):
                st.markdown(m["text"])
            if m.get("guide"):
                show_guide(m["guide"], key=f"guide{i}")

    # 快捷按钮：点一下就行
    last = ss.display[-1]
    if last.get("guide"):
        st.success("开单完成！把使用说明打印或念给客户听。")
        st.button("🆕 下一位客户", on_click=new_customer, type="primary",
                  width="stretch", key="next_main")
    else:
        c1, c2, c3 = st.columns(3)
        c1.button("✅ 客户要了", on_click=send, args=("客户要了，开单",),
                  type="primary", width="stretch")
        c2.button("💸 嫌贵", on_click=send, args=("客户嫌贵，有没有便宜点的，或者能不能便宜点",),
                  width="stretch")
        c3.button("🔄 换一个", on_click=send, args=("换一个",), width="stretch")

# 底部输入框：开了看图功能就可以附带照片（比如补拍）
placeholder = "也可以直接打字，比如：土豆叶子有黑斑，20亩"
if vclient:
    typed = st.chat_input(placeholder + "（📎 可补拍照片）", accept_file="multiple",
                          file_type=["jpg", "jpeg", "png", "webp"])
    if typed:
        send(typed.text or f"补拍了{len(typed.files)}张照片",
             [vision.compress(f.getvalue()) for f in typed.files[:MAX_PHOTOS]])
else:
    typed = st.chat_input(placeholder)
    if typed:
        send(typed)

user_text = ss.pop("pending", None)
images = ss.pop("pending_images", [])

if user_text:
    with st.chat_message("user"):
        show_images(images)
        st.markdown(user_text)

    reply = {"role": "assistant", "text": "", "tools": [], "guide": None, "vision": None}
    with st.chat_message("assistant"):
        status = st.status("正在分析…")
        content = user_text

        # 1. 先看图（看图失败不影响按文字继续）
        if images and vclient:
            status.write("📷 正在看照片…")
            try:
                reply["vision"] = vision.describe_images(
                    vclient, images, crop=ss.current_crop, note=user_text)
                with st.expander("📷 看图结果"):
                    st.markdown(reply["vision"])
                content += f"\n\n【照片识别结果（看图AI给出，仅供参考）】\n{reply['vision']}"
            except Exception as e:
                st.warning(f"照片识别失败，先按文字描述判断：{e}")

        # 2. 再判断、匹配店里的药
        text_area = st.empty()
        texts = []

        def on_text(t):
            texts.append(t)
            text_area.markdown("\n\n".join(texts))

        def on_tool(name, args, result):
            label = TOOL_LABELS.get(name, name)
            reply["tools"].append(label)
            status.write(label)
            if name == "create_order" and result.get("ok"):
                reply["guide"] = Path(result["用药说明文件"]).read_text(encoding="utf-8")

        checkpoint = len(ss.messages)
        ss.messages.append({"role": "user", "content": content})
        try:
            agent.chat_turn(client, ss.messages, on_text=on_text, on_tool=on_tool)
            status.update(label="完成", state="complete")
            reply["text"] = "\n\n".join(texts)
            ss.display += [{"role": "user", "text": user_text, "images": images}, reply]
            st.rerun()  # 刷新页面：显示快捷按钮、更新今日销售和库存
        except APIError as e:
            del ss.messages[checkpoint:]
            status.update(label="出错了", state="error")
            st.error(f"网络或接口出错，请重新发送：{e}")
