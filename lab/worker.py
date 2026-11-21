"""Isolated Playwright process. JSONL events + JPEG stream + video + trace + JUnit.

The smoke checks deliberately do not equate a loading splash with a playable game.
Gameplay assertions are explicit, calibrated image scenarios.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core import CATALOG, PROFILES


class Skipped(Exception):
    pass


def safe_url(url):
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except ValueError:
        return "[invalid URL]"


class Runner:
    def __init__(self, spec):
        self.spec = spec
        self.options = spec["options"]
        self.folder = Path(spec["folder"])
        self.events_file = self.folder / "events.jsonl"
        self.results = []
        self.diagnostics = []
        self.current = None
        self.cancelled = False
        self.frames = 0

    def event(self, kind, **data):
        with self.events_file.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")

    def step(self, message, **data):
        self.event("step", message=message, test_id=self.current, **data)

    def diagnostic(self, kind, message):
        if len(self.diagnostics) < 2000:
            self.diagnostics.append({"kind": kind, "message": str(message)[:1200], "test_id": self.current})
        if len(self.diagnostics) < 100:
            self.event("browser_log", kind=kind, message=str(message)[:500], test_id=self.current)

    async def pump(self, page):
        """Real MJPEG frames, independent of step events; no synthetic game footage."""
        interval = max(0.17, 1 / max(1, min(6, int(os.getenv("LIVE_FPS", "4")))))
        while True:
            try:
                frame = await page.screenshot(type="jpeg", quality=60, timeout=2000, animations="allow")
                temp = self.folder / "live.jpg.tmp"
                temp.write_bytes(frame)
                temp.replace(self.folder / "live.jpg")
                self.frames += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def watch_cancel(self):
        while not (self.folder / "cancel").exists():
            await asyncio.sleep(0.2)
        self.cancelled = True

    async def open(self, page):
        self.step("Открываю тестовый стенд")
        response = await page.goto(self.spec["target"], wait_until="domcontentloaded", timeout=45000)
        if response is None or response.status >= 400:
            raise AssertionError("Главный документ недоступен: " + str(response.status if response else "no response"))
        return response

    async def canvas(self, page):
        self.step("Ожидаю видимый #gameCanvas")
        canvas = page.locator("#gameCanvas")
        await canvas.wait_for(state="visible", timeout=self.options["timeout"] * 1000)
        box = await canvas.bounding_box()
        assert box and box["width"] > 100 and box["height"] > 100, "Canvas отсутствует или слишком мал"
        return canvas, box

    async def observe(self, seconds=12):
        self.step("Наблюдение: %d секунд" % seconds)
        await asyncio.sleep(seconds)

    def template(self, template_id):
        data = self.spec["templates"].get(template_id)
        if not data:
            raise Skipped("Нет шаблона '%s'. Создайте его в разделе «Калибровка»." % template_id)
        if "variants" in data:
            data = data["variants"].get(self.options["profile"] + ":" + self.options["browser"])
            if not data:
                raise Skipped("Для '%s' нет варианта %s/%s. Сохраните шаблон в нужном окружении." %
                    (template_id, self.options["browser"], self.options["profile"]))
        if data["profile"] != self.options["profile"] or data["browser"] != self.options["browser"]:
            raise Skipped("Шаблон '%s' откалиброван для %s/%s; выбран %s/%s" %
                (template_id, data["browser"], data["profile"], self.options["browser"], self.options["profile"]))
        return Path(self.spec["data_dir"]) / "templates" / data["file"]

    async def find(self, page, template_id, seconds=None):
        import cv2
        import numpy as np
        template_path = self.template(template_id)
        needle = cv2.imdecode(np.frombuffer(template_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if needle is None:
            raise AssertionError("Шаблон повреждён")
        if float(np.max(np.std(needle, axis=(0, 1)))) < 5:
            raise AssertionError("Шаблон почти одноцветен. Выделите уникальный элемент с контрастными деталями.")
        deadline = time.monotonic() + (seconds or min(75, self.options["timeout"] - 5))
        best = 0.0
        while time.monotonic() < deadline:
            image = await page.screenshot(type="png", timeout=5000)
            frame = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
            if frame is None or needle.shape[0] > frame.shape[0] or needle.shape[1] > frame.shape[1]:
                raise AssertionError("Шаблон больше исходного экрана")
            # SQDIFF handles low-variance crops without the CCOEFF constant-image trap.
            scores = cv2.matchTemplate(frame, needle, cv2.TM_SQDIFF_NORMED)
            minimum, _, location, _ = cv2.minMaxLoc(scores)
            similarity = 1.0 - minimum
            best = max(best, similarity)
            if similarity >= 0.96:
                return {"x": location[0] + needle.shape[1] / 2,
                        "y": location[1] + needle.shape[0] / 2, "similarity": round(similarity, 4)}
            await asyncio.sleep(0.4)
        raise AssertionError("Шаблон '%s' не найден; лучшее совпадение %.3f < 0.96" % (template_id, best))

    async def scenario(self, page, scenario_id):
        definition = self.spec["scenarios"][scenario_id]
        # Validate templates up front, before any interaction.
        for step in definition["steps"]:
            if "template" in step:
                self.template(step["template"])
        await self.open(page)
        await self.canvas(page)
        for index, step in enumerate(definition["steps"]):
            action = step["action"]
            self.step("%d. %s %s" % (index + 1, action, step.get("template", "")))
            if action in ("assert", "wait", "click"):
                point = await self.find(page, step["template"])
                self.event("match", template=step["template"], **point)
                if action == "click":
                    if self.options["profile"] != "desktop" and self.options["browser"] != "firefox":
                        await page.touchscreen.tap(point["x"], point["y"])
                    else:
                        await page.mouse.click(point["x"], point["y"])
                    await asyncio.sleep(0.4)
            elif action == "reload":
                await page.reload(wait_until="domcontentloaded")
                await self.canvas(page)
            elif action == "screenshot":
                await page.screenshot(path=str(self.folder / (self.case_key + "-step-%d.png" % index)))
            elif action == "drag":
                x1, y1, x2, y2 = step["coords"]
                width, height = PROFILES[self.options["profile"]]
                await page.mouse.move(x1 * width, y1 * height)
                await page.mouse.down()
                await page.mouse.move(x2 * width, y2 * height, steps=12)
                await page.mouse.up()
                await asyncio.sleep(0.5)

    async def check(self, test_id, page, errors, failures):
        if test_id.startswith("scenario:"):
            return await self.scenario(page, test_id.split(":", 1)[1])
        # Calibration-dependent checks skip BEFORE opening the page.
        if test_id == "ready":
            self.template("game_ready")
        if test_id == "baseline":
            self.template("game_ready")
            self.template("baseline_" + self.options["profile"] + "_" + self.options["browser"])
        await self.open(page)
        if test_id == "http":
            self.step("Главный документ успешно загружен")
            return
        canvas, box = await self.canvas(page)
        if test_id == "observe":
            self.step("Сеанс калибровки: можно захватывать реальные кадры игры")
            await self.observe(max(5, self.options["timeout"] - 10))
            raise Skipped("Сеанс сбора кадров завершён. Игровые утверждения не проверялись.")
        elif test_id == "canvas":
            self.step("Canvas видим", width=box["width"], height=box["height"])
        elif test_id == "render":
            from PIL import Image, ImageStat
            await self.observe(5)
            picture = Image.open(io.BytesIO(await canvas.screenshot())).convert("RGB")
            variation = max(ImageStat.Stat(picture).stddev)
            assert variation > 8, "Canvas практически одноцветен: stddev=%.2f" % variation
            self.step("Обнаружена графика; готовность игры этим тестом не подтверждается", variation=round(variation, 2))
        elif test_id == "layout":
            width, height = PROFILES[self.options["profile"]]
            horizontal = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
            assert not horizontal, "Есть горизонтальная прокрутка"
            assert box["x"] >= -2 and box["y"] >= -2 and box["x"] + box["width"] <= width + 2 and box["y"] + box["height"] <= height + 2, "Canvas выходит за границы viewport: " + str(box)
        elif test_id == "reload":
            self.step("Перезагружаю страницу")
            await page.reload(wait_until="domcontentloaded")
            await self.canvas(page)
        elif test_id == "javascript":
            await self.observe()
            assert not errors, "Необработанные JavaScript-ошибки: " + "; ".join(errors[:5])
        elif test_id == "resources":
            await self.observe()
            assert not failures, "Ошибки ресурсов стенда: " + "; ".join(failures[:8])
        elif test_id == "ready":
            self.step("Ожидаю откалиброванный готовый экран")
            await self.find(page, "game_ready")
        elif test_id == "baseline":
            from PIL import Image, ImageChops
            import numpy as np
            await self.find(page, "game_ready")
            baseline = Image.open(self.template("baseline_" + self.options["profile"] + "_" + self.options["browser"])).convert("RGB")
            actual = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")
            assert baseline.size == actual.size, "Эталон должен быть полным кадром такого же размера"
            diff = ImageChops.difference(baseline, actual)
            pixels = np.asarray(diff)
            ratio = float(np.mean(np.max(pixels, axis=2) > 35))
            diff.save(self.folder / (self.case_key + "-diff.png"))
            self.step("Сравнение с эталоном", changed_percent=round(100 * ratio, 2))
            assert ratio <= 0.05, "Изменено %.2f%% пикселей (допуск 5%%); проверьте анимацию и динамические элементы" % (100 * ratio)

    async def run_case(self, browser, test_id, repetition, index):
        self.current = test_id
        self.case_key = "%02d-%02d" % (repetition, index)
        names = {t["id"]: t["name"] for t in CATALOG}
        if test_id.startswith("scenario:"):
            name = self.spec["scenarios"][test_id.split(":", 1)[1]]["name"]
        else:
            name = names[test_id]
        result = {"id": test_id, "name": name, "repeat": repetition, "duration": 0,
                  "status": "failed", "message": "", "screenshot": ""}
        self.event("test_start", **result)
        width, height = PROFILES[self.options["profile"]]
        config = {"viewport": {"width": width, "height": height}, "device_scale_factor": 1,
                  "record_video_dir": str(self.folder / "videos"),
                  "record_video_size": {"width": width, "height": height},
                  "has_touch": self.options["profile"] != "desktop"}
        # Firefox does not implement Playwright's is_mobile emulation.
        if self.options["browser"] != "firefox":
            config["is_mobile"] = self.options["profile"] != "desktop"
        context = await browser.new_context(**config)
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = await context.new_page()
        errors, failures = [], []
        hostname = urlsplit(self.spec["target"]).hostname
        def error(exc):
            errors.append(str(exc)[:500])
            self.diagnostic("pageerror", exc)
        def request_failed(request):
            if urlsplit(request.url).hostname == hostname:
                message = safe_url(request.url) + " " + str(request.failure)
                failures.append(message)
                self.diagnostic("request_failed", message)
        def response_received(response):
            if urlsplit(response.url).hostname == hostname and response.status >= 400:
                message = str(response.status) + " " + safe_url(response.url)
                failures.append(message)
                self.diagnostic("http_error", message)
        page.on("pageerror", error)
        page.on("requestfailed", request_failed)
        page.on("response", response_received)
        page.on("console", lambda msg: self.diagnostic("console." + msg.type, msg.text) if msg.type in ("error", "warning") else None)
        pump = asyncio.create_task(self.pump(page))
        cancel = asyncio.create_task(self.watch_cancel())
        task = asyncio.create_task(self.check(test_id, page, errors, failures))
        started = time.monotonic()
        try:
            done, _ = await asyncio.wait((task, cancel), timeout=self.options["timeout"], return_when=asyncio.FIRST_COMPLETED)
            if cancel in done:
                self.cancelled = True
                raise asyncio.CancelledError()
            if task not in done:
                raise TimeoutError("Тест превысил таймаут %d секунд" % self.options["timeout"])
            await task
            result["status"] = "passed"
        except Skipped as exc:
            result.update(status="skipped", message=str(exc))
        except asyncio.CancelledError:
            result.update(status="cancelled", message="Остановлено пользователем или сервисом")
        except Exception as exc:
            result["message"] = (type(exc).__name__ + ": " + str(exc))[:2500]
        finally:
            for job in (pump, cancel, task):
                job.cancel()
            for job in (pump, cancel, task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await job
            if result["status"] != "skipped":
                image = self.case_key + "-" + result["status"] + ".png"
                try:
                    await page.screenshot(path=str(self.folder / image), timeout=3000)
                    result["screenshot"] = image
                except Exception:
                    pass
            try:
                await context.tracing.stop(path=str(self.folder / (self.case_key + "-trace.zip")))
            except Exception:
                pass
            video = page.video
            try:
                await context.close()
                if video:
                    result["video"] = Path(await video.path()).relative_to(self.folder).as_posix()
            except Exception:
                pass
            result["duration"] = round(time.monotonic() - started, 3)
            self.results.append(result)
            self.event("test_end", **result)

    def save(self, error=None):
        report = {"results": self.results, "frames": self.frames, "cancelled": self.cancelled}
        if error:
            report["error"] = error
        (self.folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.folder / "browser.json").write_text(json.dumps(self.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        suite = ET.Element("testsuite", name="Tapclap Lab", tests=str(len(self.results)),
            failures=str(sum(r["status"] == "failed" for r in self.results)),
            skipped=str(sum(r["status"] in ("skipped", "cancelled") for r in self.results)))
        for result in self.results:
            case = ET.SubElement(suite, "testcase", name=result["name"], classname=self.options["browser"] + "." + self.options["profile"], time=str(result["duration"]))
            if result["status"] == "failed":
                ET.SubElement(case, "failure", message=result["message"]).text = result["message"]
            if result["status"] in ("skipped", "cancelled"):
                ET.SubElement(case, "skipped", message=result["message"])
        if error:
            suite.set("errors", "1")
            ET.SubElement(ET.SubElement(suite, "testcase", name="worker_setup"), "error", message=error).text = error
        ET.ElementTree(suite).write(str(self.folder / "junit.xml"), encoding="utf-8", xml_declaration=True)

    async def main(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            engine = getattr(playwright, self.options["browser"])
            launch = {"headless": True}
            if self.options["browser"] == "chromium":
                launch["args"] = ["--disable-dev-shm-usage", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            browser = await engine.launch(**launch)
            self.event("browser", message="Браузер запущен", browser=self.options["browser"], version=browser.version)
            try:
                for repetition in range(1, self.options["repeat"] + 1):
                    for index, test_id in enumerate(self.options["tests"], 1):
                        if (self.folder / "cancel").exists():
                            self.cancelled = True
                            break
                        await self.run_case(browser, test_id, repetition, index)
                        self.save()
                        if self.cancelled:
                            break
                    if self.cancelled:
                        break
            finally:
                await browser.close()


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    runner = Runner(spec)
    try:
        asyncio.run(runner.main())
        runner.save()
        return 1 if any(r["status"] == "failed" for r in runner.results) else 0
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        if isinstance(exc, ModuleNotFoundError):
            error += ". Установите requirements.txt и выполните python -m playwright install."
        runner.event("error", message=error[:2000])
        runner.save(error[:2000])
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
