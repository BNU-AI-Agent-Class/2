# ═══════════════════════════════════════════════════════════════
# 高考志愿底线澄清助手 — HTTP API 服务器
# 纯 Python 内置模块，无需 pip install
# ═══════════════════════════════════════════════════════════════
import http.server
import json
import os
import sys
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gaokao_assistant import GaokaoAssistant

# Windows UTF-8
if sys.platform == "win32":
    import io
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

assistant = GaokaoAssistant()
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class APIHandler(http.server.SimpleHTTPRequestHandler):
    """合并 API 路由 + 静态文件服务。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    # ── CORS 工具 ──────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Id")

    def _json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    # ── OPTIONS（预检）─────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET：前端页面 + API ────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # API: 新建会话
        if parsed.path == "/api/session/new":
            sid = uuid.uuid4().hex[:12]
            self._json(200, {"session_id": sid})

        # API: 健康检查
        elif parsed.path == "/api/health":
            has_key = bool(os.getenv("DEEPSEEK_API_KEY"))
            self._json(200, {"status": "ok", "has_key": has_key})

        # API: 获取记忆
        elif parsed.path == "/api/memory":
            qs = urllib.parse.parse_qs(parsed.query)
            sid = qs.get("session_id", [None])[0]
            if not sid:
                self._json(400, {"error": "缺少 session_id"})
                return
            from gaokao_assistant import memory_read
            mem = memory_read(sid)
            self._json(200, mem)

        # API: 获取澄清问题列表（前端展示用）
        elif parsed.path == "/api/questions":
            from gaokao_assistant import CLARIFY_QUESTIONS
            self._json(200, {"questions": CLARIFY_QUESTIONS})

        # 根路径
        elif parsed.path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()

        # 其他静态文件
        else:
            super().do_GET()

    # ── POST：对话 ─────────────────────────────────────────
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/chat":
            sid = self.headers.get("X-Session-Id", "")
            body = self._read_body()
            user_input = body.get("message", "").strip()

            if not sid or not user_input:
                self._json(400, {"error": "缺少 session_id 或 message"})
                return

            try:
                result = assistant.chat(sid, user_input)
            except Exception:
                result = {
                    "reply": (
                        "抱歉，服务暂时不可用。如果你感到着急或不安，"
                        "可以拨打 010-82951332（北京心理援助热线）或 12355。"
                    ),
                    "stage": "error",
                    "safety": "green",
                }
            self._json(200, result)

        else:
            self._json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        # 精简日志
        if "/api/" in str(args[0]) or args[0].startswith("POST"):
            print(f"[API] {args[0]}")


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "8080"))
    server = http.server.HTTPServer(("0.0.0.0", PORT), APIHandler)
    print(f"服务已启动 → http://localhost:{PORT}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()
