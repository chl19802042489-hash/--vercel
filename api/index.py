import json
import hmac
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


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

# The owner intentionally requires this public, fixed access password.
# Do not read AGENT_PASSWORD here, so stale Vercel settings cannot override it.
ACCESS_PASSWORD = "123456"
API_KEY = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
API_BASE = os.environ.get("AI_API_BASE", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gpt-4.1-mini")
ROOT = Path(__file__).resolve().parent

MAX_REQUEST_BYTES = env_int("MAX_REQUEST_BYTES", 64 * 1024, 1024, 256 * 1024)
MAX_MESSAGE_CHARS = env_int("MAX_MESSAGE_CHARS", 4000, 200, 16000)
MAX_MODEL_MESSAGE_CHARS = env_int("MAX_MODEL_MESSAGE_CHARS", 16000, 1000, 50000)
MAX_RESTORE_MESSAGES = 20
MAX_SESSIONS = env_int("MAX_SESSIONS", 500, 10, 5000)
GENERAL_RATE_LIMIT = env_int("GENERAL_RATE_LIMIT", 60, 10, 1000)
CHAT_RATE_LIMIT = env_int("CHAT_RATE_LIMIT", 20, 1, 200)
AUTH_FAILURE_LIMIT = env_int("AUTH_FAILURE_LIMIT", 5, 2, 50)
MAX_RATE_LIMIT_KEYS = env_int("MAX_RATE_LIMIT_KEYS", 10000, 100, 50000)
RATE_WINDOW_SECONDS = 60
AUTH_WINDOW_SECONDS = 10 * 60
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class SlidingWindowLimiter:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_seconds = window_seconds
        self.events = {}
        self.lock = threading.Lock()

    def _active_events(self, key, now):
        if key not in self.events and len(self.events) >= MAX_RATE_LIMIT_KEYS:
            self.events.pop(next(iter(self.events)))
        queue = self.events.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if not queue:
            self.events.pop(key, None)
            queue = self.events.setdefault(key, deque())
        return queue

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            queue = self._active_events(key, now)
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            return True

    def blocked(self, key):
        now = time.monotonic()
        with self.lock:
            return len(self._active_events(key, now)) >= self.limit

    def record(self, key):
        now = time.monotonic()
        with self.lock:
            self._active_events(key, now).append(now)

    def clear(self, key):
        with self.lock:
            self.events.pop(key, None)


general_limiter = SlidingWindowLimiter(GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS)
chat_limiter = SlidingWindowLimiter(CHAT_RATE_LIMIT, RATE_WINDOW_SECONDS)
auth_failure_limiter = SlidingWindowLimiter(AUTH_FAILURE_LIMIT, AUTH_WINDOW_SECONDS)
sessions_lock = threading.Lock()

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


sessions = OrderedDict()


def normalize_session_id(value):
    session_id = str(value or "default").strip()
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise RequestError("会话标识格式无效")
    return session_id


def get_session(session_id):
    with sessions_lock:
        history = list(sessions.get(session_id, []))
        if session_id in sessions:
            sessions.move_to_end(session_id)
        return history


def save_session(session_id, history):
    with sessions_lock:
        sessions[session_id] = list(history[-20:])
        sessions.move_to_end(session_id)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)


def clear_session(session_id):
    with sessions_lock:
        sessions.pop(session_id, None)


def validated_messages(payload):
    message = payload.get("original_message") or payload.get("message") or ""
    model_message = payload.get("message") or message
    if not isinstance(message, str) or not isinstance(model_message, str):
        raise RequestError("消息格式无效")
    message = message.strip()
    model_message = model_message.strip()
    if not message:
        raise RequestError("消息不能为空")
    if len(message) > MAX_MESSAGE_CHARS or len(model_message) > MAX_MODEL_MESSAGE_CHARS:
        raise RequestError("消息过长，请缩短后重试", status=413)
    return message, model_message


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
    server_version = "MaoAgent"
    sys_version = ""

    def client_key(self):
        forwarded = self.headers.get("X-Vercel-Forwarded-For") or self.headers.get(
            "X-Forwarded-For", ""
        )
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:128]
        return str(self.client_address[0] if self.client_address else "unknown")[:128]

    def allowed_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None
        normalized = origin.rstrip("/")
        if normalized in ALLOWED_ORIGINS:
            return normalized
        parsed = urlsplit(normalized)
        host = self.headers.get("Host", "").lower()
        if parsed.scheme in ("http", "https") and parsed.netloc.lower() == host:
            return normalized
        return None

    def origin_is_allowed(self):
        return not self.headers.get("Origin") or self.allowed_origin() is not None

    def end_headers(self):
        allowed_origin = self.allowed_origin()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def do_OPTIONS(self):
        if not self.origin_is_allowed():
            self.write_json({"error": "不允许的请求来源"}, status=403)
            return
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if not self.origin_is_allowed():
            self.write_json({"error": "不允许的请求来源"}, status=403)
            return

        client_key = self.client_key()
        if not general_limiter.allow(client_key):
            self.write_json(
                {"error": "请求过于频繁，请稍后重试"},
                status=429,
                extra_headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
            )
            return

        path = urlsplit(self.path).path
        if path in ("/api/chat", "/api/chat_stream") and not chat_limiter.allow(client_key):
            self.write_json(
                {"error": "对话请求过于频繁，请稍后重试"},
                status=429,
                extra_headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
            )
            return

        routes = {
            "/api/chat_stream": self.handle_chat_stream,
            "/api/chat": self.handle_chat,
            "/api/clear": self.handle_clear,
            "/api/restore": self.handle_restore,
            "/api/status": self.handle_status,
        }
        route = routes.get(path)
        if route is None:
            self.write_json({"error": "Unknown endpoint"}, status=404)
            return
        try:
            route()
        except RequestError as exc:
            self.write_json({"error": exc.message}, status=exc.status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise RequestError("请求长度无效") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestError("请求内容过大", status=413)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if length and content_type != "application/json":
            raise RequestError("仅支持 application/json", status=415)
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("JSON 请求格式无效") from exc
        if not isinstance(payload, dict):
            raise RequestError("JSON 顶层必须是对象")
        return payload

    def authorize(self, payload):
        client_key = self.client_key()
        if auth_failure_limiter.blocked(client_key):
            return False, 429
        supplied = payload.get("password")
        if not isinstance(supplied, str) or len(supplied) > 128:
            auth_failure_limiter.record(client_key)
            return False, 403
        if hmac.compare_digest(supplied.encode("utf-8"), ACCESS_PASSWORD.encode("utf-8")):
            auth_failure_limiter.clear(client_key)
            return True, 200
        auth_failure_limiter.record(client_key)
        return False, 403

    def require_authorization(self, payload):
        authorized, status = self.authorize(payload)
        if authorized:
            return True
        if status == 429:
            self.write_json(
                {"error": "密码尝试次数过多，请稍后重试"},
                status=429,
                extra_headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
            )
        else:
            self.write_json({"error": "访问密码不正确"}, status=403)
        return False

    def handle_status(self):
        payload = self.read_json()
        if not self.require_authorization(payload):
            return
        if API_KEY:
            self.write_json({"message": "模型接口已配置"})
        else:
            self.write_json({
                "message": "模型接口未配置。请在 Vercel 项目环境变量中设置 AI_API_KEY。"
            })

    def handle_clear(self):
        payload = self.read_json()
        if not self.require_authorization(payload):
            return
        session_id = normalize_session_id(payload.get("session_id"))
        clear_session(session_id)
        self.write_json({"ok": True})

    def handle_restore(self):
        payload = self.read_json()
        if not self.require_authorization(payload):
            return
        session_id = normalize_session_id(payload.get("session_id"))
        incoming_messages = payload.get("messages") or []
        if not isinstance(incoming_messages, list):
            raise RequestError("历史消息格式无效")
        restored = []
        for item in incoming_messages[-MAX_RESTORE_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content") or ""
            if not isinstance(content, str):
                continue
            content = content.strip()[:MAX_MESSAGE_CHARS]
            if role == "user" and content:
                restored.append({"role": "user", "content": content})
            elif role in ("agent", "assistant") and content:
                restored.append({"role": "assistant", "content": content})
        save_session(session_id, restored)
        self.write_json({"ok": True, "count": len(restored)})

    def handle_chat(self):
        payload = self.read_json()
        if not self.require_authorization(payload):
            return
        if not API_KEY:
            self.write_json({
                "error": "后端还没有配置模型 API Key。请在 Vercel 项目环境变量中设置 AI_API_KEY。"
            }, status=500)
            return

        message, model_message = validated_messages(payload)
        session_id = normalize_session_id(payload.get("session_id"))
        history = get_session(session_id)
        model_history = clean_history_for_model(history) + [{"role": "user", "content": model_message}]

        try:
            reply = call_model(model_history)
        except Exception:
            self.write_json({"error": "模型服务暂时不可用，请稍后重试"}, status=502)
            return

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        save_session(session_id, history)
        self.write_json({"reply": reply})

    def handle_chat_stream(self):
        payload = self.read_json()
        if not self.require_authorization(payload):
            return

        message, model_message = validated_messages(payload)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if not API_KEY:
            self.write_sse({"error": "后端还没有配置模型 API Key。请先设置 OPENAI_API_KEY 或 AI_API_KEY。"})
            self.write_sse({"done": True})
            return

        session_id = normalize_session_id(payload.get("session_id"))
        history = get_session(session_id)
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
        except Exception:
            try:
                self.write_sse({"error": "模型服务暂时不可用，请稍后重试"})
                self.write_sse({"done": True})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        reply = "".join(reply_parts).strip()
        if reply:
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": reply})
            save_session(session_id, history)
        self.write_sse({"done": True})

    def write_json(self, payload, status=200, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
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

