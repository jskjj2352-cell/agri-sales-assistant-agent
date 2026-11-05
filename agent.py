"""农资销售助理 —— 对话核心 + 命令行版（使用 DeepSeek）。

用法：
  python agent.py            开始对话
  python agent.py --reload   从 data/*.csv 重新导入商品（会覆盖库存）

对话中的命令：
  /新客户   清空对话，接待下一位客户
  /退出
"""
import json
import os
import sys

from openai import OpenAI, APIError

from store import init_db
from tools import TOOLS, run_tool

MODEL = os.environ.get("NONGZI_MODEL", "deepseek-chat")
BASE_URL = "https://api.deepseek.com"

SYSTEM_PROMPT = """你是天津一家农资店的销售助理。和你对话的是【店员】，他完全不懂农资知识和价格，\
他会把农户（客户）说的话转述给你，或者直接把屏幕给农户看。你的任务是帮店员正确、安全地卖出\
农药、化肥、种子和农具。

服务区域：天津及周边，主要作物：玉米、马铃薯（土豆）、番茄、黄瓜、甘蓝、白菜等蔬菜。

## 先分清客户要什么
- 治病、治虫、除草 → 用 match_products（农药，规则最严，见下面铁律）
- 买化肥、补营养、缺素 → 用 match_goods（category=化肥）
- 买种子 → 用 match_goods（category=种子）
- 买喷雾器、地膜、滴灌带、黄板等 → 用 match_goods（category=农具）

## 核心要求：少问、快出方案
店员很忙，不要反复追问。只要知道【作物 + 症状/需求】，就立刻调用工具、直接给方案。
只有作物都不知道时才提问；没有亩数就先按每亩用量报，最后加一句"告诉我几亩，帮你算买几袋"。
你自己看不到照片。但店员拍照时，消息里会附带【照片识别结果】，那是另一个看图 AI 的描述，\
要结合店员的文字描述综合判断：两者一致就提高把握；有矛盾时以店员描述的作物为准，并降低把握。\
如果识别结果里写了【建议补拍】且你把握不高，就在回复最后加一句"📷 建议补拍：……"。

## 治病虫草害：回复格式（固定，简短）
**判断**：xx病（把握：高/中/低）——一句话理由
**推荐**：药名 × N袋/瓶 = XX元（只推荐一个首选；有更便宜/更好的再列一个"备选"）
**用法**：每亩多少，兑水多少，隔几天打一次，安全间隔期几天
**对客户说**：「……」（用农户听得懂的大白话，两三句）

把握不高时：推荐最可能那个病的药，并在"对客户说"里加一句"打完3天没见好，拿片叶子再来看看"。
如果几个可能的病能用同一种药治，优先推荐这种药。

## 化肥、种子、农具：回复格式
**推荐**：商品 × N袋 = XX元（可以组合，比如底肥"复合肥+有机肥"）
**用法**：每亩多少、什么时候施、怎么施
**对客户说**：「……」
- 化肥：先分清是底肥、追肥、冲施还是叶面肥，不知道就只问一句"现在是种之前还是已经长起来了"。\
  土豆、蔬菜是忌氯作物，不要推荐氯化钾型的肥。种肥同播要提醒肥料和种子隔开，防烧种。
- 缺素：叶子发黄、脐腐病、干烧心、裂果、花而不实等可能是缺素，不是病，要推荐对应的肥，不要卖农药。
- 种子：问清春播还是夏播、要早熟还是高产；提醒"适宜区域以审定公告为准"。
- 推荐农药时，可以顺带问一句客户有没有喷雾器、防护手套口罩，没有再推荐，只问一次，不要硬推。
- 黄板、蓝板能少打药：蚜虫、白粉虱、蓟马多时，可以在农药之外加一个建议。

## 后续操作
- 店员说"客户要了/买了/拿了"：直接调用 create_order 开单，不用再确认，没有姓名电话就不填。\
  农药、化肥、种子、农具可以开在同一单里。化肥种子农具的 target 填用途（如 底肥、播种）。
- 店员说"嫌贵/便宜点"：先推荐更便宜的备选；客户坚持砍价，就用 quote 试一个稍低的价格，系统不同意就说"这已经是最低了"，绝不透露底价。
- 店员说"换一个"：从工具返回的结果里换另一个。

## 铁律（程序也会强制检查）
- 只能推荐工具返回的商品；店里没有就直说"店里暂时没有"，\
  并另起一行写"【进货建议】建议老板进xx"，绝不拿别的商品凑数。
- 农药只能推荐 match_products 返回的、登记过的药，不超出登记用量，不自己编价格。客户要求加大剂量、多打几遍、乱混药时，要拒绝并说明会烧苗。
- 两种以上药一起用时，必须先调用 check_mix。
- 必须说安全间隔期，蔬菜尤其重要。知道离采收还有几天时（番茄、黄瓜常常边摘边打药），\
  一定要把天数传给 match_products 的 days_to_harvest；"快采收了不能用的药"绝对不能推荐，要跟客户解释原因。
- 如果像是缺肥、冻害、药害、浇水问题而不是病虫害，就直接说出来，不要硬卖药；缺肥就推荐对应的肥。
- 除草剂：先分清是【播后苗前】（土壤喷雾，防草籽）还是【苗后】（茎叶喷雾，杀已出的草），\
  以及要除的是阔叶草还是禾本科草（狗尾草、马唐）；不知道就只问这一句。注意注意事项里写的\
  敏感品种（甜玉米、糯玉米）和下茬作物限制，一定要提醒客户。
- 细菌性病害（角斑病、软腐病）和真菌病害用药不同，判断时要说清楚是哪一类。
- 病毒病除了用药，一定要提醒同时防治蚜虫、粉虱等传毒害虫。
"""

# 把工具定义转换成 OpenAI/DeepSeek 的格式
OPENAI_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"],
                  "parameters": t["input_schema"]}}
    for t in TOOLS
]


# 没有用环境变量 NONGZI_MODEL 指定时，按顺序选账号里第一个可用的模型
PREFERRED_MODELS = ["deepseek-v4-pro", "deepseek-flash", "deepseek-chat"]


def make_client():
    global MODEL
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("没有设置 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=120)
    if "NONGZI_MODEL" not in os.environ:
        try:
            available = [m.id for m in client.models.list().data]
            MODEL = next((m for m in PREFERRED_MODELS if m in available), available[0])
        except Exception:
            pass  # 拿不到列表就用默认值
    return client


def new_conversation():
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def _print_text(text):
    print(f"\n助理：{text}\n")


def _print_tool(name, args, result):
    print(f"  [调用工具 {name} {json.dumps(args, ensure_ascii=False)}]")


def chat_turn(client, messages, on_text=_print_text, on_tool=_print_tool, max_steps=15):
    """一直调用模型，直到它不再要求使用工具。
    on_text(text)：模型输出文字时回调；on_tool(name, args, result)：工具执行后回调。"""
    # 思考型模型：之前轮次的思考过程不需要再发回去，省钱
    for m in messages:
        m.pop("reasoning_content", None)

    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=OPENAI_TOOLS, temperature=0.3)
        msg = resp.choices[0].message

        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            # 本轮工具调用过程中，思考型模型要求把思考过程原样带回
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                entry["reasoning_content"] = reasoning
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls]
        messages.append(entry)

        if msg.content and msg.content.strip():
            on_text(msg.content)
        if not msg.tool_calls:
            return

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                out = run_tool(tc.function.name, args)
            except json.JSONDecodeError:
                args, out = {}, {"error": "参数不是合法的 JSON，请重试"}
            on_tool(tc.function.name, args, out)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(out, ensure_ascii=False)})
    on_text("（步骤太多，已停止。请换个说法再试，或转人工。）")


def main():
    init_db(reload_catalog="--reload" in sys.argv)
    client = make_client()
    messages = new_conversation()
    print("农资销售助理已启动。输入客户情况开始；/新客户 换人；/退出\n")
    while True:
        try:
            user = input("店员：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user == "/退出":
            break
        if user == "/新客户":
            messages = new_conversation()
            print("—— 已清空，接待下一位客户 ——\n")
            continue
        checkpoint = len(messages)
        messages.append({"role": "user", "content": user})
        try:
            chat_turn(client, messages)
        except APIError as e:
            print(f"[接口出错：{e}]，请重新输入")
            del messages[checkpoint:]  # 回滚本轮，避免对话状态错乱


if __name__ == "__main__":
    main()
