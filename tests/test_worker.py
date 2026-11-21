import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lab.worker import Runner, Skipped, safe_url


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = Runner({"folder": self.temp.name, "data_dir": self.temp.name,
            "options": {"browser": "chromium", "profile": "desktop", "timeout": 30, "tests": ["http"], "repeat": 1},
            "templates": {}, "scenarios": {}, "target": "https://gems2-testers.stand.tapclap.com/"})

    def tearDown(self):
        self.temp.cleanup()

    def page(self, status=200, box=None, horizontal=False):
        canvas = SimpleNamespace(wait_for=AsyncMock(), bounding_box=AsyncMock(return_value=box or {"x":0,"y":0,"width":1280,"height":720}))
        return SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=status)), locator=lambda _: canvas,
            reload=AsyncMock(), evaluate=AsyncMock(return_value=horizontal))

    async def test_http_failure_is_not_passed(self):
        with self.assertRaises(AssertionError):
            await self.runner.check("http", self.page(status=503), [], [])

    async def test_canvas_check_is_only_visibility(self):
        page = self.page()
        await self.runner.check("canvas", page, [], [])
        page.goto.assert_awaited_once()

    async def test_layout_rejects_clipped_canvas(self):
        with self.assertRaises(AssertionError):
            await self.runner.check("layout", self.page(box={"x":20,"y":0,"width":1280,"height":720}), [], [])

    async def test_layout_rejects_horizontal_scroll(self):
        with self.assertRaises(AssertionError):
            await self.runner.check("layout", self.page(horizontal=True), [], [])

    async def test_ready_skips_before_navigation_without_template(self):
        page = self.page()
        with self.assertRaises(Skipped):
            await self.runner.check("ready", page, [], [])
        page.goto.assert_not_awaited()

    async def test_baseline_skips_without_ready_template(self):
        with self.assertRaises(Skipped):
            await self.runner.check("baseline", self.page(), [], [])

    async def test_javascript_errors_fail_after_observation(self):
        self.runner.observe = AsyncMock()
        with self.assertRaises(AssertionError):
            await self.runner.check("javascript", self.page(), ["TypeError"], [])

    async def test_resource_errors_fail_after_observation(self):
        self.runner.observe = AsyncMock()
        with self.assertRaises(AssertionError):
            await self.runner.check("resources", self.page(), [], ["404 assets.png"])

    async def test_cancel_monitor_detects_sentinel(self):
        (Path(self.temp.name) / "cancel").touch()
        await asyncio.wait_for(self.runner.watch_cancel(), timeout=1)
        self.assertTrue(self.runner.cancelled)

    def test_template_variants_select_exact_environment(self):
        self.runner.spec["templates"] = {"ready": {"variants": {"desktop:chromium": {"file":"ready.png","profile":"desktop","browser":"chromium"}}}}
        self.assertEqual(self.runner.template("ready").name, "ready.png")
        self.runner.options["profile"] = "mobile"
        with self.assertRaises(Skipped):
            self.runner.template("ready")

    def test_query_secrets_not_in_url_diagnostics(self):
        self.assertEqual(safe_url("https://test.example/path?token=secret#fragment"), "https://test.example/path")

    def test_junit_records_skipped_and_errors(self):
        self.runner.results = [{"id":"ready","name":"Ready","status":"skipped","message":"Not calibrated","duration":0}]
        self.runner.save("Missing browser")
        text = (Path(self.temp.name) / "junit.xml").read_text()
        self.assertIn('skipped="1"', text)
        self.assertIn('errors="1"', text)
        self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
