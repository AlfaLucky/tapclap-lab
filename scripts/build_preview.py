"""Create a clearly labelled, offline UI preview. No invented run data."""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.core import CATALOG


source = (ROOT / "lab/static/index.html").read_text(encoding="utf-8")
css = (ROOT / "lab/static/style.css").read_text(encoding="utf-8")
source = source.replace('<link rel="stylesheet" href="/static/style.css"><script src="/static/app.js" defer></script>', '<style>' + css + '</style>')
source = source.replace('<div class="workspace">', '<div class="workspace"><div style="padding:10px 25px;background:#3b3021;color:#f1d19a;font:12px system-ui">ПРЕДВАРИТЕЛЬНЫЙ ПРОСМОТР · Статичный интерфейс без API и запусков. Рабочее приложение запускается по инструкции README.</div>')
source = source.replace('id="connection">Подключение…', 'id="connection">Просмотр интерфейса')
picker = "".join('<label class="test-option" title="' + html.escape(t["description"],quote=True) + '"><input type="checkbox" disabled ' + ('checked' if i<7 else '') + '><span>' + html.escape(t["name"]) + '</span></label>' for i,t in enumerate(CATALOG))
source = source.replace('<div id="testPicker" class="test-picker"></div>', '<div id="testPicker" class="test-picker">' + picker + '</div>')
for marker in ('historyTable','stabilityTable','scheduleList'):
    source = re.sub('(<div[^>]*id="' + marker + '"[^>]*>)</div>',r'\1<div class="table-empty">Нет данных: это предварительный просмотр интерфейса.</div></div>',source)
source = re.sub(r'<button(?![^>]*\bdisabled\b)', '<button disabled', source)
source = source.replace('</body>', '''<script>
function route(){var name=location.hash.slice(1)||'overview';document.querySelectorAll('[data-page]').forEach(function(e){e.hidden=e.dataset.page!==name});document.querySelectorAll('[data-nav]').forEach(function(e){e.classList.toggle('active',e.dataset.nav===name)});}window.addEventListener('hashchange',route);route();
</script></body>''')
(ROOT / "preview.html").write_text(source, encoding="utf-8")
print(ROOT / "preview.html")
