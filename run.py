"""启动器：检查环境 -> 安装依赖 -> 验证 Key -> 启动网页。
每一步都写入 run_log.txt，出问题时把这个文件发给开发者即可。"""
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "run_log.txt"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def log(msg):
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def fail(msg):
    log("[错误] " + msg)
    log("请把本文件夹里的 run_log.txt 发给开发者。")
    input("按回车键退出...")
    sys.exit(1)


def main():
    LOG.write_text("", encoding="utf-8")
    os.chdir(HERE)
    log(f"Python: {sys.version}")
    log(f"路径: {sys.executable}")

    # 0. 读取模型设置（auto 表示自动选择，不设置环境变量）
    settings = HERE / "settings.txt"
    if settings.exists():
        for line in settings.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = (s.strip() for s in line.split("=", 1))
            if v and v != "auto":
                os.environ[k] = v
                log(f"设置：{k} = {'******' if 'PASSWORD' in k else v}")

    # 1. 读取 Key
    key_file = HERE / "api_key.txt"
    if not key_file.exists():
        fail("没有找到 api_key.txt")
    raw = key_file.read_bytes()
    key = None
    for enc in ("utf-8-sig", "utf-16", "gbk"):
        try:
            key = raw.decode(enc).strip().strip('"').strip()
            if key.startswith("sk-"):
                break
        except UnicodeDecodeError:
            continue
    if not key or not key.startswith("sk-"):
        fail("api_key.txt 里的内容不像 DeepSeek 的 Key（应该以 sk- 开头，且只有这一行）")
    os.environ["DEEPSEEK_API_KEY"] = key
    log(f"Key 读取成功: {key[:5]}...{key[-4:]}")

    # 1b. 看图功能的 Key（可选）
    qwen_file = HERE / "qwen_key.txt"
    qwen_key = None
    if qwen_file.exists():
        raw = qwen_file.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "gbk"):
            try:
                qwen_key = raw.decode(enc).strip().strip('"').strip()
                break
            except UnicodeDecodeError:
                continue
    if qwen_key and qwen_key.startswith("sk-"):
        os.environ["DASHSCOPE_API_KEY"] = qwen_key
        log(f"看图 Key 读取成功: {qwen_key[:5]}...{qwen_key[-4:]}")
    else:
        qwen_key = None
        log("没有 qwen_key.txt（或内容不对），拍照功能关闭，其它功能正常")

    # 2. 安装/更新依赖（已经装好时几秒钟就过）
    log("正在检查依赖（第一次需要1-3分钟）...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", "-i", MIRROR],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(r.stdout[-3000:] + "\n" + r.stderr[-3000:] + "\n")
        fail("依赖安装失败，详情见 run_log.txt")
    import streamlit
    log(f"依赖就绪（streamlit {streamlit.__version__}）")

    # 3. 验证 Key 和网络（列出模型不扣费）
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=20)
        models = [m.id for m in client.models.list().data]
        log(f"DeepSeek 连接成功，可用模型: {models}")
    except Exception as e:
        log(traceback.format_exc())
        fail(f"连接 DeepSeek 失败：{e}\n  常见原因：Key 填错 / 账户没充值 / 网络不通")

    if qwen_key:
        try:
            qc = OpenAI(api_key=qwen_key,
                        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", timeout=20)
            ids = [m.id for m in qc.models.list().data]
            vl = [m for m in ids if any(k in m for k in ("vl", "flash", "omni", "max", "plus"))]
            log(f"通义千问连接成功，可用模型: {vl}")
            import vision
            vision.make_vision_client()
            if vision.MODEL not in ids:
                log(f"[警告] 账号里没有找到 {vision.MODEL}，请检查 settings.txt 里的名字")
            log(f"本次使用的看图模型: {vision.MODEL}")
        except Exception as e:
            log(f"[警告] 通义千问连接失败，拍照功能可能用不了：{e}")

    # 4. 启动网页（headless 跳过 Streamlit 首次运行时要求输入邮箱的提示）
    port = next((p for p in range(8501, 8511) if port_free(p)), None)
    if port is None:
        fail("8501-8510 端口都被占用了，可能已经开了好几个助理。请重启电脑后再试。")
    if port != 8501:
        log(f"8501 端口被占用（可能上次的助理没关干净），改用 {port} 端口")
    url = f"http://localhost:{port}"
    log(f"正在启动，浏览器将打开 {url}")
    log(f"平板访问：http://本机IP:{port}（本机IP 用 ipconfig 查看）")
    log("关闭这个黑窗口 = 关闭助理\n")
    threading.Thread(target=lambda: (time.sleep(4), webbrowser.open(url)), daemon=True).start()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.address", "0.0.0.0", "--server.port", str(port),
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:  # 网页程序的输出同时显示在窗口和写进日志
        log(line.rstrip())
    code = proc.wait()
    if code != 0:
        fail(f"网页程序异常退出（代码 {code}），报错见上方 / run_log.txt")


def port_free(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log(traceback.format_exc())
        fail("启动器出错")
