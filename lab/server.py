"""Small HTTP control plane; standard library keeps idle resource use low."""
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import threading
import time
import zipfile
from collections import defaultdict, deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import CATALOG, PROFILES, TARGET, TERMINAL, Invalid, Store, decode_png, validate_scenario
from .manager import Manager

STATIC = Path(__file__).resolve().parent / "static"


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, admin_token, start_worker=True):
        if len(admin_token) < 24:
            raise ValueError("ADMIN_TOKEN must contain at least 24 characters")
        self.store = store
        self.admin_token = admin_token
        self.public = os.getenv("PUBLIC_READ", "true").lower() == "true"
        self.secure_cookie = os.getenv("COOKIE_SECURE", "true" if os.getenv("RAILWAY_ENVIRONMENT") else "false").lower() == "true"
        self.login_attempts = defaultdict(deque)
        self.auth_lock = threading.Lock()
        self.manager = Manager(store)
        self.streams = threading.BoundedSemaphore(6)
        super().__init__(address, Handler)
        if start_worker:
            self.manager.start()

    def server_close(self):
        self.manager.stop()
        super().server_close()


def report_html(run, offline=False):
    esc = html.escape
    rows = []
    for test in run["results"]:
        attachment = test.get("screenshot", "")
        if attachment:
            prefix = "" if offline else '/api/runs/' + run["id"] + '/artifacts/'
            attachment = '<a href="' + prefix + esc(attachment, quote=True) + '">Скриншот</a>'
        rows.append("<tr><td>" + esc(test.get("name", test["id"])) + "</td><td>" + esc(test["status"]) +
                    "</td><td>" + str(round(test.get("duration", 0), 2)) + " s</td><td>" +
                    esc(test.get("message", "")) + "</td><td>" + attachment + "</td></tr>")
    body = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Tapclap Lab — отчёт</title>'
    body += '<style>body{font:16px system-ui;background:#10131e;color:#e3e7f4;max-width:1100px;margin:40px auto;padding:20px}a{color:#82dec6}table{border-collapse:collapse;width:100%}td,th{padding:14px;border-bottom:1px solid #343c52;text-align:left}small{color:#a8b2cb}</style>'
    body += '<a href="/">← Tapclap Lab</a><h1>Прогон ' + esc(run["id"]) + '</h1><p>' + esc(run["status"]) + ' · ' + esc(run["created"]) + '</p>'
    body += '<p>' + esc(run["options"]["browser"]) + ' · ' + esc(run["options"]["profile"]) + '</p><p>' + esc(run["summary"]) + '</p>'
    body += '<table><thead><tr><th>Тест</th><th>Результат</th><th>Время</th><th>Описание</th><th>Материалы</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
    body += '<p><small>Skipped не считается passed. Наличие Canvas или заставки не доказывает готовность игры. Проверка viewport mobile эмулирует экран и touch; это не реальный телефон.</small></p></html>'
    return body.encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "TapclapLab/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log auth headers, cookies or request payloads.
        logging.getLogger("http").info("%s %s", self.command, self.path.split("?")[0])

    @property
    def session_token(self):
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return jar["lab_session"].value if "lab_session" in jar else ""
        except cookies.CookieError:
            return ""

    def is_admin(self):
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), self.server.admin_token.encode()):
            return True
        return self.server.store.authenticated(self.session_token)

    def require_write(self):
        if not self.is_admin():
            self.respond(401, {"error": "Войдите для управления прогонами"})
            return False
        return self.same_origin()

    def same_origin(self):
        # Cookie auth is same-origin only. CLI clients can use bearer auth.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.respond(403, {"error": "Запросы с другого сайта запрещены"})
            return False
        return True

    def body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Invalid("Некорректная длина запроса")
        if not 0 < size <= 3_000_000:
            raise Invalid("Пустой или слишком большой запрос")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise Invalid("Нужен Content-Type application/json")
        try:
            result = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise Invalid("Некорректный JSON")
        if not isinstance(result, dict):
            raise Invalid("Ожидается JSON-объект")
        return result

    def respond(self, status, data, mime="application/json; charset=utf-8", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            self.get_route()
        except (Invalid, ValueError) as exc:
            self.respond(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("GET failed")
            self.respond(500, {"error": "Внутренняя ошибка сервиса"})

    def get_route(self):
        path = unquote(urlparse(self.path).path)
        store = self.server.store
        if path == "/healthz":
            return self.respond(200, {"ok": True})
        if path == "/api/session":
            return self.respond(200, {"admin": self.is_admin(), "public_read": self.server.public})
        if path.startswith("/api/") and not self.server.public and not self.is_admin():
            return self.respond(401, {"error": "Войдите для просмотра"})
        if path == "/api/catalog":
            return self.respond(200, {"tests": CATALOG, "profiles": PROFILES, "target": TARGET,
                "templates": store.objects("templates"), "scenarios": store.objects("scenarios"),
                "capabilities": {"live_stream": "MJPEG, до 6 кадров/с", "parallel_runs": 1,
                    "retention_days": int(os.getenv("RETENTION_DAYS", "7"))}})
        if path == "/api/runs":
            return self.respond(200, store.runs())
        if path == "/api/stats":
            return self.respond(200, store.stats())
        if path == "/api/schedules":
            return self.respond(200, store.schedules())
        if path == "/api/compare":
            query = parse_qs(urlparse(self.path).query)
            first = store.run(query.get("a", [""])[0])
            second = store.run(query.get("b", [""])[0])
            if not first or not second:
                return self.respond(404, {"error": "Выберите два существующих прогона"})
            return self.respond(200, {"a": first, "b": second})
        match = re.fullmatch(r"/api/runs/([a-f0-9]{16})(?:/(.*))?", path)
        if match:
            run_id, action = match.groups()
            run = store.run(run_id)
            if run is None:
                return self.respond(404, {"error": "Прогон не найден"})
            folder = store.root / "runs" / run_id
            if not action:
                return self.respond(200, run)
            if action == "events":
                after = max(0, int(parse_qs(urlparse(self.path).query).get("after", [0])[0]))
                return self.respond(200, store.events(run_id, after))
            if action == "files":
                paths = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and
                         p.name not in ("spec.json", "cancel") and not p.name.endswith(".tmp")]
                return self.respond(200, sorted(paths))
            if action == "report":
                return self.respond(200, report_html(run), "text/html; charset=utf-8")
            if action == "stream":
                return self.stream(run_id, folder)
            if action == "snapshot":
                image = folder / "live.jpg"
                if not image.exists():
                    return self.respond(404, {"error": "Кадр ещё не получен"})
                return self.respond(200, image.read_bytes(), "image/jpeg")
            if action.startswith("artifacts/"):
                relative = action[len("artifacts/"):]
                file = (folder / relative).resolve()
                if folder.resolve() not in file.parents or not file.is_file() or file.name in ("spec.json", "cancel") or file.name.endswith(".tmp"):
                    return self.respond(404, {"error": "Файл отсутствует или удалён по сроку хранения"})
                if file.stat().st_size > 100 * 1024 * 1024:
                    return self.respond(413, {"error": "Файл слишком большой для выдачи"})
                return self.file_response(file)
            if action == "download":
                if run["status"] not in TERMINAL:
                    return self.respond(409, {"error": "Дождитесь завершения прогона"})
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("report.html", report_html(run, offline=True))
                    archive.writestr("run.json", json.dumps(run, ensure_ascii=False, indent=2))
                    total = 0
                    for file in folder.rglob("*"):
                        if file.is_file() and file.name not in ("spec.json", "cancel") and not file.name.endswith(".tmp"):
                            total += file.stat().st_size
                            if total > 100 * 1024 * 1024:
                                return self.respond(413, {"error": "Архив больше 100 MB. Скачайте материалы по отдельности."})
                            archive.write(file, file.relative_to(folder))
                return self.respond(200, output.getvalue(), "application/zip", {"Content-Disposition": 'attachment; filename="run-' + run_id + '.zip"'})
        if path == "/":
            return self.respond(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            file = (STATIC / path[len("/static/"):]).resolve()
            if STATIC.resolve() in file.parents and file.is_file():
                return self.file_response(file)
        self.respond(404, {"error": "Не найдено"})

    def file_response(self, file):
        data = file.read_bytes()
        mime = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        # Browser video players use Range requests when seeking in WebM.
        byte_range = self.headers.get("Range", "")
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
            if not match or not data or not any(match.groups()):
                return self.respond(416, b"", headers={"Content-Range": "bytes */" + str(len(data))})
            left, right = match.groups()
            start = int(left) if left else max(0, len(data) - int(right))
            end = min(int(right), len(data) - 1) if left and right else len(data) - 1
            if start >= len(data) or end < start:
                return self.respond(416, b"", headers={"Content-Range": "bytes */" + str(len(data))})
            return self.respond(206, data[start:end + 1], mime,
                {"Accept-Ranges": "bytes", "Content-Range": "bytes %d-%d/%d" % (start, end, len(data))})
        return self.respond(200, data, mime, {"Accept-Ranges": "bytes"})

    def stream(self, run_id, folder):
        if not self.server.streams.acquire(blocking=False):
            return self.respond(429, {"error": "Слишком много зрителей. Откройте режим скриншотов."})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            seen = None
            started = time.monotonic()
            while time.monotonic() - started < 1200:
                image = folder / "live.jpg"
                try:
                    stamp = image.stat().st_mtime_ns
                    if stamp != seen:
                        data = image.read_bytes()
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush()
                        seen = stamp
                except FileNotFoundError:
                    pass
                if self.server.store.run(run_id)["status"] in TERMINAL:
                    break
                time.sleep(0.17)
            self.wfile.write(b"--frame--\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.streams.release()

    def do_POST(self):
        try:
            self.post_route()
        except (Invalid, ValueError, TypeError, KeyError) as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            logging.exception("POST failed")
            self.respond(500, {"error": "Внутренняя ошибка сервиса"})

    def post_route(self):
        path = urlparse(self.path).path
        store = self.server.store
        if path == "/api/login":
            if not self.same_origin():
                return
            data = self.body()
            # Peer-based throttling avoids trusting a spoofed X-Forwarded-For header.
            address = self.client_address[0]
            with self.server.auth_lock:
                attempts = self.server.login_attempts[address]
                while attempts and attempts[0] < time.monotonic() - 600:
                    attempts.popleft()
                if len(attempts) >= 10:
                    return self.respond(429, {"error": "Слишком много попыток. Повторите через 10 минут."})
                attempts.append(time.monotonic())
            token = data.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.server.admin_token.encode()):
                return self.respond(401, {"error": "Неверный ключ администратора"})
            session = store.session()
            cookie = "lab_session=" + session + "; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict"
            if self.server.secure_cookie:
                cookie += "; Secure"
            return self.respond(200, {"admin": True}, headers={"Set-Cookie": cookie})
        if not self.require_write():
            return
        data = self.body()
        if path == "/api/logout":
            store.logout(self.session_token)
            return self.respond(200, {"admin": False}, headers={"Set-Cookie": "lab_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        if path == "/api/runs":
            return self.respond(201, store.enqueue(data))
        match = re.fullmatch(r"/api/runs/([a-f0-9]{16})/(cancel|retry)", path)
        if match:
            run = store.run(match[1])
            if not run:
                return self.respond(404, {"error": "Прогон не найден"})
            if match[2] == "cancel":
                return self.respond(200, store.cancel(run["id"]))
            if run["status"] not in TERMINAL:
                raise Invalid("Повтор доступен после завершения")
            failed = list(dict.fromkeys(r["id"] for r in run["results"] if r["status"] == "failed"))
            options = dict(run["options"])
            if data.get("failed_only", True) and failed:
                options["tests"] = failed
                options["repeat"] = 1
            elif data.get("failed_only", True) and run["status"] == "passed":
                raise Invalid("В прогоне нет упавших тестов")
            return self.respond(201, store.enqueue(options, "retry", run["id"]))
        if path == "/api/schedules":
            return self.respond(201, {"id": store.add_schedule(data)})
        match = re.fullmatch(r"/api/schedules/([a-f0-9]{16})", path)
        if match:
            if type(data.get("enabled")) is not bool:
                raise Invalid("enabled должен быть boolean")
            store.toggle_schedule(match[1], data["enabled"])
            return self.respond(200, {"ok": True})
        if path == "/api/templates":
            obj_id = data.get("id", "")
            if not isinstance(obj_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,47}", obj_id):
                raise Invalid("Имя шаблона: 3–48 символов a-z, 0-9, _")
            profile, browser = data.get("profile"), data.get("browser")
            if profile not in PROFILES or browser not in ("chromium", "firefox", "webkit"):
                raise Invalid("Укажите профиль и браузер исходного кадра")
            png = decode_png(data.get("image"))
            revision = hashlib.sha256(png).hexdigest()[:16]
            filename = obj_id + "-" + revision + ".png"
            file = store.root / "templates" / filename
            file.write_bytes(png)
            info = {"id": obj_id, "file": filename, "profile": profile, "browser": browser,
                    "source_width": PROFILES[profile][0], "revision": revision}
            existing = store.objects("templates").get(obj_id, {})
            variants = existing.get("variants", {})
            if existing and not variants:
                variants[existing["profile"] + ":" + existing["browser"]] = existing
            variants[profile + ":" + browser] = dict(info)
            info["variants"] = variants
            store.put_object("templates", obj_id, info)
            return self.respond(201, info)
        if path == "/api/scenarios":
            scenario = validate_scenario(data, store.objects("templates"))
            obj_id = secrets.token_hex(8)
            store.put_object("scenarios", obj_id, scenario)
            return self.respond(201, {"id": obj_id, **scenario})
        self.respond(404, {"error": "Не найдено"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("ADMIN_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("Set ADMIN_TOKEN (at least 24 random characters). See README.md.")
    server = LabServer((os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8080"))),
                       Store(os.getenv("DATA_DIR", "./data")), token)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info("Tapclap Lab listening on port %s", server.server_port)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
