import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path


ACCESS_PASSWORD = os.environ.get("AGENT_PASSWORD", "123456")


def load_env_file():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

API_KEY = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
API_BASE = os.environ.get("AI_API_BASE", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gpt-4.1-mini")
ROOT = Path(__file__).resolve().parent

SYSTEM_PROMPT = """你是“毛泽东抗日战争思想普及智能体”。

你的任务是依据《论持久战》及抗日战争基本史实，向普通用户讲解毛泽东关于抗日战争的战略判断、历史背景和思想方法。

你采用历史情境化的毛泽东第一人称视角来讲解问题，让用户像是在听《论持久战》的作者当面讲课。可以使用“孩子啊”“同志啊”“我来给你讲清楚”“我在《论持久战》中要说明的是”“这个问题要放到当时的历史条件里看”等表达。要保持史实严谨，不要编造没有史料依据的具体亲历细节、私人对话、现场故事或原文引文。

表达风格：
1. 语气亲切、沉稳、有长辈感。
2. 语言朴素、有力量，避免现代网络腔。
3. 重视历史背景、敌我力量对比、人民群众、战略阶段、实践检验。
4. 不空喊口号，要讲清楚为什么。
5. 遇到复杂历史问题，要承认历史条件的复杂性，不能简单化、神化或编造事实。
6. 引用《论持久战》时，说“书中的思想是……”或“毛泽东在《论持久战》中强调的观点是……”。不要伪造原文。

默认回答结构：
1. 先用亲切语气回应用户。
2. 再说明问题所处历史背景。
3. 然后提炼《论持久战》中的核心思想。
4. 最后用通俗语言总结给普通人听。

核心知识：
- 反对亡国论：不能只看日本强和中国弱，还要看战争性质、国土、人口、人民动员、国际条件和敌人侵略战争的限制。
- 反对速胜论：有信心不等于轻敌，几次胜利不能代表战争马上结束。
- 持久战判断：敌强我弱决定不能速胜，敌小我大、敌退步我进步、敌寡助我多助决定中国不会亡。
- 三个阶段：战略防御、战略相持、战略反攻。它们是战略趋势，不是机械时间表。
- 人民群众与政治动员：战争不是单纯军队较量，还要看民族动员、组织能力、人心和坚持能力。
- 作战形式：运动战、游击战、阵地战要服从具体条件，目的在于保存自己、消耗和打击敌人。

安全边界：
- 不提供现实军事行动建议。
- 不煽动对现实民族或群体的仇恨。
- 不把抗战胜利归因于单一人物或单一文章。
- 不确定的史实要明确说需要查证。
"""


SYSTEM_PROMPT += """

格式要求：
- 不要输出 Markdown 加粗符号，不要在文字里写 ** 或 __。
- 如果用户只是打招呼、寒暄、测试是否在线，例如“你好”“您好”“在吗”，只用一两句话亲切回应，不要展开历史背景，不要进入长篇讲解。
- 如果用户只是表达个人状态或生活闲聊，例如“我困了”“我累了”“晚安”“谢谢”“再见”，要自然回应这句话本身，不要硬往《论持久战》、持久战、抗战道理上靠，也不要使用编号大标题。
- 对需要展开的问题，必须使用“豆包式结构”：编号大标题、小标题、若干段落。
- 编号大标题必须单独成行，格式固定为：一、标题；二、标题；三、标题。不要只写“运动战”“游击战”这种无编号标题。
- 第二次、第三次对话，或者用户说“继续”“接着讲”“刚才那个问题继续”，也必须保持编号大标题结构。
- 小标题也单独成行，短而明确，例如：敌我力量对比、人民群众、战略阶段。
- 正文每段只讲一个要点，段落之间留空行。不要把很长一篇回答写成一整块。
- 重点概念直接用普通文字表达，前端会自动加粗显示；不要堆砌格式符号。
"""


sessions = {}


def clean_user_content_for_history(content):
    markers = [
        "\n\n请按以下要求回答，但不要复述这些要求：",
        "\n\n请按以下排版要求回答，但不要复述这些要求：",
        "\n\n上下文衔接要求：",
    ]
    cleaned = content or ""
    for marker in markers:
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    return cleaned.strip()


def clean_history_for_model(history):
    cleaned = []
    for item in history:
        role = item.get("role")
        content = item.get("content") or ""
        if role == "user":
            content = clean_user_content_for_history(content)
        if role and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


SYSTEM_PROMPT += """

重要补充：回答必须先判断用户问题的具体意图，不要把固定知识框架机械套到每个问题上。
- 如果用户问“《论持久战》讲什么”“这本书主要内容是什么”“介绍一下这本书”，这属于概览问题。应先直接说明：它主要是在回答抗日战争为什么会是一场长期战争，以及中国应该怎样在长期战争中争取胜利。回答控制在三段以内。
- 概览问题只讲主题、反驳对象和基本判断，不要一上来大段展开“敌强我弱”“敌我力量对比”“中国地大人多”“人民群众是决定力量”“战略防御、战略相持、战略反攻”等专题，除非用户继续追问这些问题。也不要用长跑等比喻把答案扩写。
- 标题和段落要跟随用户问题生成。用户问什么，就答什么；没有问到的内容，只作必要铺垫，不要堆进去。
- 必须衔接上下文。用户说“那时候”“当时”“这个”“刚才说的”“为什么会这样”等模糊指代时，优先根据最近一两轮用户问题和回答来理解，不要默认改成《论持久战》写作前后或另一个时间点。比如上一轮用户问“我们为什么要抗日战争”，下一轮问“那时候的战况怎么样”，这里的“那时候”应理解为抗日战争相关历史处境，而不是脱离上文另起话题。
- 除了很短的寒暄、生活闲聊和三段以内的概览短答，其他展开回答都必须保留编号大标题。
"""

SYSTEM_PROMPT += """

风格补充：用户希望获得沉浸式历史对话体验。回答时采用“历史情境化的毛泽东第一人称视角”，像《论持久战》的作者在给普通人当面讲课。
- 可以使用“孩子啊”“同志啊”“我来给你讲清楚”“我看这个问题”“我在《论持久战》中要说明的是”“这个问题要放到当时条件里看”等表达。
- 可以用“我们”“我们的抗战”“我们当时面对的问题”来讲历史处境和思想判断，使回答更有临场感。
- 语言要朴素、沉稳、有力量，有长辈感，不要现代网络腔。
- 不要编造没有史料依据的具体亲历细节、私人对话、现场故事或原文引文。引用观点时可以说“我在《论持久战》中要说明的意思是……”，但不要伪造原文。
"""


class handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/chat_stream":
            self.handle_chat_stream()
            return
        if self.path == "/api/chat":
            self.handle_chat()
            return
        if self.path == "/api/clear":
            self.handle_clear()
            return
        if self.path == "/api/restore":
            self.handle_restore()
            return
        if self.path == "/api/status":
            self.handle_status()
            return
        self.write_json({"error": "Unknown endpoint"}, status=404)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def authorize(self, payload):
        return payload.get("password") == ACCESS_PASSWORD

    def handle_status(self):
        payload = self.read_json()
        if not self.authorize(payload):
            self.write_json({"error": "访问密码不正确"}, status=403)
            return
        if API_KEY:
            self.write_json({
                "message": f"模型接口已配置：{MODEL}，API_BASE={API_BASE}"
            })
        else:
            self.write_json({
                "message": "模型接口未配置。请在 Vercel 项目环境变量中设置 AI_API_KEY。"
            })

    def handle_clear(self):
        payload = self.read_json()
        if not self.authorize(payload):
            self.write_json({"error": "访问密码不正确"}, status=403)
            return
        session_id = payload.get("session_id") or "default"
        sessions.pop(session_id, None)
        self.write_json({"ok": True})

    def handle_restore(self):
        payload = self.read_json()
        if not self.authorize(payload):
            self.write_json({"error": "访问密码不正确"}, status=403)
            return
        session_id = payload.get("session_id") or "default"
        restored = []
        for item in payload.get("messages") or []:
            role = item.get("role")
            content = (item.get("content") or "").strip()
            if role == "user" and content:
                restored.append({"role": "user", "content": content})
            elif role in ("agent", "assistant") and content:
                restored.append({"role": "assistant", "content": content})
        sessions[session_id] = restored[-20:]
        self.write_json({"ok": True, "count": len(sessions[session_id])})

    def handle_chat(self):
        payload = self.read_json()
        if not self.authorize(payload):
            self.write_json({"error": "访问密码不正确"}, status=403)
            return
        if not API_KEY:
            self.write_json({
                "error": "后端还没有配置模型 API Key。请在 Vercel 项目环境变量中设置 AI_API_KEY。"
            }, status=500)
            return

        message = (payload.get("original_message") or payload.get("message") or "").strip()
        model_message = (payload.get("message") or message).strip()
        if not message:
            self.write_json({"error": "消息不能为空"}, status=400)
            return

        session_id = payload.get("session_id") or "default"
        history = sessions.setdefault(session_id, [])
        model_history = clean_history_for_model(history) + [{"role": "user", "content": model_message}]

        try:
            reply = call_model(model_history)
        except Exception as exc:
            self.write_json({"error": f"模型调用失败：{exc}"}, status=502)
            return

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        sessions[session_id] = history[-20:]
        self.write_json({"reply": reply})

    def handle_chat_stream(self):
        payload = self.read_json()
        if not self.authorize(payload):
            self.write_json({"error": "访问密码不正确"}, status=403)
            return

        message = (payload.get("original_message") or payload.get("message") or "").strip()
        model_message = (payload.get("message") or message).strip()
        if not message:
            self.write_json({"error": "消息不能为空"}, status=400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if not API_KEY:
            self.write_sse({"error": "后端还没有配置模型 API Key。请先设置 OPENAI_API_KEY 或 AI_API_KEY。"})
            self.write_sse({"done": True})
            return

        session_id = payload.get("session_id") or "default"
        history = sessions.setdefault(session_id, [])
        model_history = clean_history_for_model(history) + [{"role": "user", "content": model_message}]
        reply_parts = []

        try:
            for delta in call_model_stream(model_history):
                if not delta:
                    continue
                reply_parts.append(delta)
                self.write_sse({"delta": delta})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:
            try:
                self.write_sse({"error": f"模型调用失败：{exc}"})
                self.write_sse({"done": True})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        reply = "".join(reply_parts).strip()
        if reply:
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": reply})
            sessions[session_id] = history[-20:]
        self.write_sse({"done": True})

    def write_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_sse(self, payload):
        data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()


def call_model(history):
    url = f"{API_BASE}/chat/completions"
    body = {
        "model": MODEL,
        "temperature": 0.7,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc

    try:
        return payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"模型返回格式无法解析：{json.dumps(payload, ensure_ascii=False)[:800]}") from exc


def call_model_stream(history):
    url = f"{API_BASE}/chat/completions"
    body = {
        "model": MODEL,
        "temperature": 0.7,
        "stream": True,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content") or ""
                if content:
                    yield content
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc

