"""Run the same real worker outside the web panel, e.g. in GitHub Actions."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.core import TARGET, normalize_options


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="chromium", choices=("chromium", "firefox", "webkit"))
    parser.add_argument("--profile", default="desktop", choices=("desktop", "mobile", "tablet"))
    parser.add_argument("--output", default="test-results/live-smoke")
    args = parser.parse_args()
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "spec.json").exists():
        raise SystemExit("Use a fresh output directory to keep earlier results intact.")
    spec = {"id": "cli", "target": TARGET, "folder": str(folder), "data_dir": str(folder),
        "templates": {}, "scenarios": {}, "options": normalize_options({"browser": args.browser, "profile": args.profile})}
    file = folder / "spec.json"
    file.write_text(json.dumps(spec), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "lab.worker", str(file)], cwd=str(ROOT))
    print("Artifacts:", folder)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
