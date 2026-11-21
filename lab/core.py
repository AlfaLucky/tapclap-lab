"""Persistence, validation and statistics. No browser or third-party imports."""
import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import struct
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

TARGET = "https://gems2-testers.stand.tapclap.com/"
TERMINAL = {"passed", "failed", "cancelled", "interrupted", "error", "skipped"}
PROFILES = {"desktop": (1280, 720), "mobile": (390, 844), "tablet": (820, 1180)}
CATALOG = [
    {"id": "http", "name": "HTTP: страница доступна", "kind": "smoke", "description": "Главный документ отвечает HTTP 2xx/3xx."},
    {"id": "canvas", "name": "Canvas: видимость и размер", "kind": "smoke", "description": "gameCanvas видим и имеет ненулевой размер. Это не проверка окончания загрузки игры."},
    {"id": "render", "name": "Canvas: непустое изображение", "kind": "smoke", "description": "На Canvas есть графика, а не одноцветный экран. Загрузочная заставка тоже считается графикой."},
    {"id": "layout", "name": "Экран: Canvas помещается в viewport", "kind": "layout", "description": "Нет обрезания Canvas и горизонтальной прокрутки."},
    {"id": "reload", "name": "Перезагрузка: Canvas появляется снова", "kind": "smoke", "description": "После перезагрузки снова доступен Canvas. Сохранность игрового прогресса этим не проверяется."},
    {"id": "javascript", "name": "JavaScript: нет необработанных ошибок", "kind": "diagnostic", "description": "В течение окна наблюдения нет pageerror. console.error сохраняется отдельно в журнале."},
    {"id": "resources", "name": "Ресурсы стенда: нет ошибок загрузки", "kind": "diagnostic", "description": "Нет failed-запросов и HTTP 4xx/5xx к домену стенда за окно наблюдения."},
    {"id": "ready", "name": "Игра: достигнут готовый экран", "kind": "visual", "description": "Поиск откалиброванного шаблона game_ready. Без шаблона — skipped, без ложного passed."},
    {"id": "baseline", "name": "Визуальная регрессия: эталон", "kind": "visual", "description": "Сравнение с утверждённым эталоном данного браузера и viewport. До калибровки — skipped."},
    {"id": "observe", "name": "Калибровка: длительное наблюдение", "kind": "tool", "description": "Держит игру открытой почти до таймаута для захвата шаблонов. Это сбор кадров, а не игровая проверка; итог skipped."},
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Invalid(ValueError):
    pass


def normalize_options(raw, scenario_ids=()):
    if not isinstance(raw, dict):
        raise Invalid("Ожидается объект параметров")
    tests = raw.get("tests", [t["id"] for t in CATALOG[:7]])
    allowed = {t["id"] for t in CATALOG} | {"scenario:" + i for i in scenario_ids}
    if not isinstance(tests, list) or not tests or len(tests) > 20:
        raise Invalid("Выберите от 1 до 20 тестов")
    if any(not isinstance(t, str) or t not in allowed for t in tests):
        raise Invalid("Неизвестный тест")
    profile = raw.get("profile", "desktop")
    browser = raw.get("browser", "chromium")
    if profile not in PROFILES or browser not in ("chromium", "firefox", "webkit"):
        raise Invalid("Неизвестный профиль или браузер")
    repeat = raw.get("repeat", 1)
    if type(repeat) is not int or not 1 <= repeat <= 10 or len(tests) * repeat > 60:
        raise Invalid("Повторы: 1–10, не более 60 выполнений за прогон")
    timeout = raw.get("timeout", 90)
    if type(timeout) is not int or not 15 <= timeout <= 180:
        raise Invalid("Таймаут теста: 15–180 секунд")
    return {"tests": list(dict.fromkeys(tests)), "profile": profile, "browser": browser,
            "repeat": repeat, "timeout": timeout}


def validate_scenario(raw, templates):
    if not isinstance(raw, dict):
        raise Invalid("Ожидается объект сценария")
    name = raw.get("name", "").strip()
    steps = raw.get("steps", [])
    if not 1 <= len(name) <= 100 or not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise Invalid("Название до 100 символов, от 1 до 30 шагов")
    valid = []
    for step in steps:
        if not isinstance(step, dict):
            raise Invalid("Некорректный шаг")
        action = step.get("action")
        if action in ("assert", "click", "wait"):
            template = step.get("template")
            if template not in templates:
                raise Invalid("Сначала сохраните шаблон: " + str(template))
            valid.append({"action": action, "template": template})
        elif action in ("reload", "screenshot"):
            valid.append({"action": action})
        elif action == "drag":
            coords = step.get("coords")
            if not isinstance(coords, list) or len(coords) != 4 or any(
                type(c) not in (int, float) or not math.isfinite(c) or not 0 <= c <= 1 for c in coords
            ):
                raise Invalid("Для drag нужны x1,y1,x2,y2 в диапазоне 0–1 относительно viewport")
            valid.append({"action": "drag", "coords": coords})
        else:
            raise Invalid("Неизвестное действие: " + str(action))
    if not any(s["action"] == "assert" for s in valid):
        raise Invalid("Добавьте хотя бы одну проверку assert ожидаемого экрана")
    return {"name": name, "steps": valid}


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        (self.root / "templates").mkdir(exist_ok=True)
        self.path = self.root / "lab.sqlite3"
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, started TEXT, finished TEXT,
                    status TEXT NOT NULL, source TEXT NOT NULL, options TEXT NOT NULL,
                    results TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '',
                    parent_id TEXT, cancel INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    time TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, options TEXT NOT NULL,
                    hour INTEGER NOT NULL, minute INTEGER NOT NULL, offset INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_at TEXT NOT NULL, last_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scenarios (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(str(self.path), timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def runs(self, limit=100):
        with self.db() as db:
            return [self._run(r) for r in db.execute("SELECT * FROM runs ORDER BY created DESC, rowid DESC LIMIT ?", (limit,))]

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(row) if row else None

    @staticmethod
    def _run(row):
        r = dict(row)
        for key in ("options", "results"):
            r[key] = json.loads(r[key])
        r["cancel"] = bool(r["cancel"])
        return r

    def enqueue(self, options, source="manual", parent_id=None):
        options = normalize_options(options, self.objects("scenarios"))
        with self.db() as db:
            count = db.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0]
            if count >= 10:
                raise Invalid("Очередь заполнена: максимум 10 ожидающих прогонов")
            run_id = secrets.token_hex(8)
            db.execute("INSERT INTO runs(id,created,status,source,options,parent_id) VALUES(?,?,?,?,?,?)",
                       (run_id, now(), "queued", source, json.dumps(options), parent_id))
        return self.run(run_id)

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM runs WHERE status='queued' AND cancel=0 ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE runs SET status='running', started=? WHERE id=?", (now(), row["id"]))
        return self.run(row["id"])

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE runs SET status='interrupted', finished=?, summary=? WHERE status='running'",
                       (now(), "Сервис перезапущен. Незавершённый прогон не считается успешным."))

    def update_run(self, run_id, **values):
        if not values or not set(values) <= {"status", "finished", "summary", "results", "cancel"}:
            raise Invalid("Недопустимые поля")
        args = [json.dumps(v, ensure_ascii=False) if k == "results" else v for k, v in values.items()]
        with self.db() as db:
            db.execute("UPDATE runs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", args + [run_id])

    def cancel(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise Invalid("Прогон не найден")
            if row[0] == "queued":
                db.execute("UPDATE runs SET cancel=1,status='cancelled',finished=? WHERE id=?", (now(), run_id))
            elif row[0] == "running":
                db.execute("UPDATE runs SET cancel=1 WHERE id=?", (run_id,))
        return self.run(run_id)

    def event(self, run_id, kind, data):
        with self.db() as db:
            db.execute("INSERT INTO events(run_id,time,type,data) VALUES(?,?,?,?)", (run_id, now(), kind, json.dumps(data, ensure_ascii=False)))

    def events(self, run_id, after=0, limit=500):
        with self.db() as db:
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit))]

    def objects(self, table):
        assert table in ("templates", "scenarios")
        with self.db() as db:
            return {r["id"]: json.loads(r["data"]) for r in db.execute("SELECT * FROM " + table)}

    def put_object(self, table, obj_id, data):
        assert table in ("templates", "scenarios")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO " + table + "(id,data) VALUES(?,?)", (obj_id, json.dumps(data, ensure_ascii=False)))

    def session(self):
        token = secrets.token_urlsafe(36)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (now(),))
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(),
                       (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()))
        return token

    def authenticated(self, token):
        if not token:
            return False
        with self.db() as db:
            return db.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                              (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone() is not None

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    @staticmethod
    def next_daily(hour, minute, offset, at=None):
        at = at or datetime.now(timezone.utc)
        zone = timezone(timedelta(minutes=offset))
        local = at.astimezone(zone)
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= local:
            due += timedelta(days=1)
        return due.astimezone(timezone.utc).isoformat(timespec="milliseconds")

    def schedules(self):
        with self.db() as db:
            return [{**dict(r), "options": json.loads(r["options"]), "enabled": bool(r["enabled"])}
                    for r in db.execute("SELECT * FROM schedules ORDER BY name")]

    def add_schedule(self, raw):
        if len(self.schedules()) >= 10:
            raise Invalid("Максимум 10 расписаний")
        hour, minute, offset = raw.get("hour"), raw.get("minute"), raw.get("offset", 240)
        if any(type(v) is not int for v in (hour, minute, offset)) or not (0 <= hour <= 23 and 0 <= minute <= 59 and -720 <= offset <= 840):
            raise Invalid("Некорректное время или смещение UTC")
        options = normalize_options(raw.get("options", {}), self.objects("scenarios"))
        name = str(raw.get("name", "Ежедневный smoke")).strip()[:100] or "Ежедневный smoke"
        schedule_id = secrets.token_hex(8)
        with self.db() as db:
            db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,NULL)",
                       (schedule_id, name, json.dumps(options), hour, minute, offset, 1,
                        self.next_daily(hour, minute, offset)))
        return schedule_id

    def toggle_schedule(self, schedule_id, enabled):
        with self.db() as db:
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                raise Invalid("Расписание не найдено")
            db.execute("UPDATE schedules SET enabled=?,next_at=? WHERE id=?",
                       (int(enabled), self.next_daily(row["hour"], row["minute"], row["offset"]), schedule_id))

    def tick(self, at=None):
        at = at or datetime.now(timezone.utc)
        for schedule in self.schedules():
            if schedule["enabled"] and parse_time(schedule["next_at"]) <= at:
                try:
                    self.enqueue(schedule["options"], "schedule:" + schedule["name"])
                except Invalid:
                    # Keep the due date; retry when the queue has space.
                    continue
                with self.db() as db:
                    db.execute("UPDATE schedules SET last_at=?,next_at=? WHERE id=?",
                               (at.isoformat(), self.next_daily(schedule["hour"], schedule["minute"], schedule["offset"], at), schedule["id"]))

    def stats(self):
        runs = self.runs(500)
        grouped = {}
        for run in reversed(runs):
            for r in run["results"]:
                key = (r["id"], run["options"]["browser"], run["options"]["profile"])
                item = grouped.setdefault(key, {"id": r["id"], "name": r.get("name", r["id"]),
                    "browser": key[1], "profile": key[2], "passed": 0, "failed": 0, "skipped": 0, "durations": []})
                if r["status"] in ("passed", "failed", "skipped"):
                    item[r["status"]] += 1
                if r["status"] in ("passed", "failed"):
                    item["durations"].append(round(r.get("duration", 0), 2))
        for item in grouped.values():
            total = item["passed"] + item["failed"]
            item["pass_rate"] = round(100 * item["passed"] / total, 1) if total else None
            item["unstable"] = item["passed"] > 0 and item["failed"] > 0
            item["average"] = round(sum(item["durations"]) / len(item["durations"]), 2) if item["durations"] else None
            item["durations"] = item["durations"][-20:]
        return {"runs": len(runs), "passed": sum(r["status"] == "passed" for r in runs),
                "failed": sum(r["status"] in ("failed", "error", "interrupted") for r in runs),
                "tests": list(grouped.values())}

    def setting(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))
                return value
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None


def decode_png(raw):
    if not isinstance(raw, str) or not raw.startswith("data:image/png;base64,"):
        raise Invalid("Нужен PNG из редактора шаблонов")
    try:
        data = base64.b64decode(raw.split(",", 1)[1], validate=True)
    except (ValueError, TypeError):
        raise Invalid("Некорректный base64")
    if len(data) > 2_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        raise Invalid("PNG не должен превышать 2 MB")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (8 <= width <= 2560 and 8 <= height <= 2560):
        raise Invalid("Размер шаблона: от 8 до 2560 px")
    offset, kinds = 8, []
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + length
        if end > len(data):
            raise Invalid("Повреждённый PNG")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise Invalid("Некорректная контрольная сумма PNG")
        if not kinds and (kind != b"IHDR" or length != 13):
            raise Invalid("Отсутствует PNG IHDR")
        kinds.append(kind)
        offset = end
        if kind == b"IEND":
            break
    if offset != len(data) or not kinds or kinds[-1] != b"IEND" or b"IDAT" not in kinds:
        raise Invalid("Неполный PNG")
    return data
