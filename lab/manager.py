"""Single browser worker, persistent queue, cancellation and retention."""
import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import TARGET, TERMINAL, now, parse_time

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, store, worker_command=None):
        self.store = store
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.worker_command = worker_command or [sys.executable, "-m", "lab.worker"]
        self.current = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self.loop, name="queue", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.terminate(self.process)
        if self.thread:
            self.thread.join(timeout=12)

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=8)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def loop(self):
        last_maintenance = 0.0
        while not self.stop_event.is_set():
            try:
                self.store.tick()
                if time.monotonic() - last_maintenance > 300:
                    self.prune()
                    last_maintenance = time.monotonic()
                run = self.store.claim()
                if run:
                    self.execute(run)
                else:
                    self.stop_event.wait(1)
            except Exception:
                log.exception("Queue iteration failed")
                self.stop_event.wait(2)

    def execute(self, run):
        run_id = run["id"]
        self.current = run_id
        folder = self.store.root / "runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        spec = {"id": run_id, "options": run["options"], "folder": str(folder),
                "templates": self.store.objects("templates"), "scenarios": self.store.objects("scenarios"),
                "data_dir": str(self.store.root), "target": TARGET}
        (folder / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        results = []
        reason = None
        code = 1
        process = None
        log_file = folder / "worker.log"
        self.store.event(run_id, "run_start", {"message": "Браузер запускается", "options": run["options"]})
        try:
            with log_file.open("wb") as output:
                process = subprocess.Popen(self.worker_command + [str(folder / "spec.json")],
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=(os.name == "posix"),
                    cwd=str(Path(__file__).resolve().parent.parent))
                self.process = process
                start = time.monotonic()
                pos = 0
                partial = ""
                exited = False
                while True:
                    events_file = folder / "events.jsonl"
                    if events_file.exists():
                        with events_file.open("r", encoding="utf-8") as events:
                            events.seek(pos)
                            text = partial + events.read()
                            pos = events.tell()
                        lines = text.split("\n")
                        partial = lines.pop()
                        for line in lines:
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                kind = event.pop("type")
                                self.store.event(run_id, kind, event)
                                if kind == "test_end":
                                    results.append(event)
                                    self.store.update_run(run_id, results=results)
                            except (ValueError, KeyError):
                                log.warning("Ignored malformed worker event for %s", run_id)
                    if exited:
                        code = process.returncode
                        break
                    if process.poll() is not None:
                        # Drain events once more after exit, including the final test_end.
                        exited = True
                        continue
                    if self.stop_event.is_set():
                        reason = "interrupted"
                    elif self.store.run(run_id)["cancel"]:
                        reason = "cancelled"
                    elif time.monotonic() - start > int(os.environ.get("MAX_RUN_SECONDS", "1200")):
                        reason = "error"
                        self.store.event(run_id, "error", {"message": "Превышен общий лимит времени прогона"})
                    if reason:
                        # Cooperative cancellation lets Playwright finalize video and trace.
                        (folder / "cancel").touch()
                        try:
                            process.wait(timeout=6)
                        except subprocess.TimeoutExpired:
                            self.terminate(process)
                        continue
                    self.stop_event.wait(0.3)
            expected = len(run["options"]["tests"]) * run["options"]["repeat"]
            report_file = folder / "result.json"
            report = json.loads(report_file.read_text()) if report_file.exists() else {}
            if reason:
                status = reason
            elif code != 0 and report.get("error"):
                status = "error"
            elif len(results) != expected:
                status = "error"
            elif any(r["status"] == "failed" for r in results):
                status = "failed"
            elif code != 0:
                status = "error"
            elif all(r["status"] == "skipped" for r in results):
                status = "skipped"
            else:
                status = "passed"
            summary = report.get("error", "")
            if status == "skipped":
                summary = "Все проверки пропущены. Откалибруйте нужные шаблоны или выберите smoke-проверки."
                if run["options"]["tests"] == ["observe"]:
                    summary = "Сеанс калибровки завершён. Это сбор кадров, а не проверка игрового поведения."
            if status == "error" and not summary:
                summary = "Worker завершился без полного результата. См. worker.log. Проверьте зависимости и доступ к стенду."
            if status in ("cancelled", "interrupted"):
                summary = "Прогон остановлен; незавершённые тесты не считаются успешными."
            self.store.update_run(run_id, status=status, finished=now(), results=results, summary=summary)
            self.store.event(run_id, "run_end", {"status": status, "message": summary})
        except Exception as exc:
            log.exception("Worker failed")
            if process:
                self.terminate(process)
            self.store.update_run(run_id, status="error", finished=now(), results=results, summary=str(exc)[:500])
            self.store.event(run_id, "run_end", {"status": "error", "message": str(exc)[:500]})
        finally:
            self.process = None
            self.current = None

    def prune(self):
        """Keep metadata; evict old artifacts, then oldest artifacts over the budget."""
        days = max(1, int(os.environ.get("RETENTION_DAYS", "7")))
        budget = max(50, int(os.environ.get("ARTIFACT_BUDGET_MB", "1000"))) * 1024 * 1024
        deadline = datetime.now(timezone.utc) - timedelta(days=days)
        candidates = []
        for run in reversed(self.store.runs(100000)):
            if run["status"] not in TERMINAL or not run["finished"]:
                continue
            folder = self.store.root / "runs" / run["id"]
            if not folder.is_dir():
                continue
            size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
            candidates.append((run, folder, size))
        total = sum(x[2] for x in candidates)
        for run, folder, size in candidates:
            if parse_time(run["finished"]) < deadline or total > budget:
                shutil.rmtree(folder)
                total -= size
                self.store.event(run["id"], "retention", {"message": "Артефакты удалены по сроку хранения или лимиту объёма. Метаданные сохранены."})
