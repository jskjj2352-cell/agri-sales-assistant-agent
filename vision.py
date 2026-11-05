"""看图诊断：用通义千问视觉模型（Qwen-VL）把照片转成症状描述和可能病因，
再交给 DeepSeek 结合文字信息做最终判断、匹配店里的药。

没有 DASHSCOPE_API_KEY 时拍照功能自动隐藏，其它功能照常使用。
"""
import base64
import io
import os

from openai import OpenAI
from PIL import Image, ImageOps

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PREFERRED_MODELS = ["qwen3-vl-plus", "qwen-vl-max", "qwen-vl-plus"]
MODEL = os.environ.get("VISION_MODEL", "auto")
if MODEL == "auto":
    MODEL = PREFERRED_MODELS[0]

VISION_PROMPT = """你是植保专家，正在帮农资店看农户拍的作物照片。请按下面格式输出，简洁、具体：

【作物】照片里是什么作物（看不出就写"看不出"）{crop_hint}
【看到的症状】发病部位（叶正面/叶背面/茎/果实/根）、病斑形状（圆形/多角形/长条/不规则/有无轮纹）、
颜色、有无霉层或粉状物及颜色、病斑边缘有无黄晕、是否看到虫子或虫粪及虫子样子、受害面积大概比例
【可能原因】1-3个，最可能的排第一，每个写把握（高/中/低）和一句理由。
  也要考虑非病虫原因：缺素、药害（除草剂飘移等）、冻害、晒伤、涝害。
【建议补拍】照片看不清或无法区分时，写出需要补拍什么（如"叶片背面特写""整株照片"）；看得清就写"无"

只描述你从照片里真正看到的，看不清的不要猜。"""


def make_vision_client():
    """没有配置 Key 就返回 None。"""
    global MODEL
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        return None
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=90)
    if os.environ.get("VISION_MODEL", "auto") == "auto":
        try:
            available = sorted((m.id for m in client.models.list().data), key=len)
            # 首选 3.8 flash（多模态、速度快、便宜），没有再按列表顺序选
            MODEL = (next((m for m in available if "3.8" in m and "flash" in m), None)
                     or next((m for m in PREFERRED_MODELS if m in available), PREFERRED_MODELS[0]))
        except Exception:
            MODEL = PREFERRED_MODELS[0]
    return client


def compress(data: bytes) -> bytes:
    """手机原图常有 5-10MB，压到长边 1280 像素，上传快也省钱。"""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def describe_images(client, images: list, crop: str = "", note: str = "") -> str:
    """images: 压缩后的 JPEG 字节列表。返回看图结果文本。"""
    hint = f"（店员说是：{crop}）" if crop else ""
    text = VISION_PROMPT.format(crop_hint=hint)
    if note:
        text += f"\n\n店员补充的情况：{note}"
    content = [
        {"type": "image_url",
         "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
        for b in images
    ]
    content.append({"type": "text", "text": text})
    resp = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": content}], temperature=0.2)
    return resp.choices[0].message.content or "（没有识别结果）"
