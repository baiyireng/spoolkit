"""静态资源路由、打包产物和 wheel 里的网页壳。"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

from spoolkit.web.page import HTML, INDEX
from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server
from spoolkit.web.static import AssetError, load_asset

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src" / "spoolkit" / "web" / "assets"
PAGE = ROOT / "src" / "spoolkit" / "web" / "page.py"


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> str:
    workspace = tmp_path_factory.mktemp("assets-server")
    runner = Runner(
        workspace,
        session="t",
        extra_args=["--provider", "fake", "--script", ""],
    )
    httpd = build_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response.read(), headers
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, exc.read(), headers


def test_page_py只做加载() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 120
    assert "<style" not in text
    assert "function applyState" not in text
    assert "<script>" in HTML and "<style>" in HTML
    assert "<link" not in HTML.lower()
    assert "src=" not in HTML.lower()
    assert 'id="st-mode"' in INDEX
    assert "<style" not in INDEX.lower()
    assert 'src="/assets/dist/app.' in INDEX


def test_兼容页和产物都留着标题归档草稿和失败路径() -> None:
    script = HTML[HTML.find("<script>") + len("<script>") : HTML.rfind("</script>")]
    bundled = (ASSETS / "dist" / "app.js").read_text(encoding="utf-8")
    for text in (script, bundled):
        assert "maybeAgentTitle(true)" in text
        assert "updateArchiveBadge(sremoved)" in text
        assert "post('/session/new', {})" in text
        assert "showFailure('失败：', data.text, data.error)" in text
        assert "goalBox.value = ''" in text
        assert "__spoolTest" not in text
    test_bundle = (ASSETS / "dist" / "test-bundle.js").read_text(encoding="utf-8")
    assert "__spoolTest" in test_bundle
    assert "applyState: applyState" in test_bundle
    for token in ("this.store", "this.app", "__spoolTest"):
        assert token not in script
        assert token not in bundled
    assert "this.store" not in test_bundle
    assert "this.app" not in test_bundle


def test_dist与源码一致() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_web_assets.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_样式脚本能访问且缓存头正确(server: str) -> None:
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    status, body, headers = _get(server + "/assets/css/tokens.css")
    assert status == 200
    assert "text/css" in headers["content-type"]
    assert headers["cache-control"] == "no-cache"
    assert headers["etag"].startswith('"')
    assert "gmt" in headers["last-modified"].lower()
    assert b"--bg" in body

    status, body, headers = _get(server + "/assets/" + manifest["js"])
    assert status == 200, body[:200]
    assert "javascript" in headers["content-type"]
    assert "immutable" in headers["cache-control"]
    assert b"function applyState" in body

    status, body, headers = _get(server + "/assets/" + manifest["css"])
    assert status == 200
    assert "text/css" in headers["content-type"]
    assert "immutable" in headers["cache-control"]
    assert b".code" in body


def test_首页走index而不是内联大字符串(server: str) -> None:
    status, body, headers = _get(server + "/")
    text = body.decode("utf-8")
    assert status == 200
    assert "<html" in text.lower()
    assert 'id="st-mode"' in text
    assert "<style" not in text.lower()
    assert "function applyState" not in text
    assert headers["cache-control"] == "no-cache"
    assert "/assets/dist/app." in text


def test_只读_INDEX_不拼兼容页() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUTF8"] = "1"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import spoolkit.web.page as page\n"
            "assert 'HTML' not in page.__dict__\n"
            "assert page.INDEX.startswith('<')\n"
            "assert 'function applyState' not in page.INDEX\n"
            "page.HTML\n"
            "assert 'function applyState' in page.HTML\n",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_执行顺序只认_JS_ORDER且打包拒绝具名导入() -> None:
    from spoolkit.web.static import JS_ORDER, strip_module_syntax

    main = (ASSETS / "js" / "main.js").read_text(encoding="utf-8")
    assert re.search(r"^\s*import\b", main, re.MULTILINE) is None
    assert "function wireFeatures(" in main
    assert main.find("wireFeatures();") < main.find("drawStatus();")
    order = list(JS_ORDER)
    assert order[order.index("js/core/store.js") + 1] == "js/core/ctx.js"
    assert order[-1] == "js/main.js"
    assert "function createStore(" in strip_module_syntax(
        (ASSETS / "js" / "core" / "store.js").read_text(encoding="utf-8")
    )
    assert "export " not in strip_module_syntax('export { createStore, store };\nconst n = 1;\n')
    with pytest.raises(RuntimeError, match="import"):
        strip_module_syntax('import { drawTarget } from "./sidebar.js";\n')
    with pytest.raises(RuntimeError, match="export"):
        strip_module_syntax("export default function nope() {}\n")


def test_跨_feature_裸调用会被检查出来() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_web_assets

    assert check_web_assets.cross_feature_problems() == []
    foreign = {"drawTarget", "setRunning"}
    assert check_web_assets.bare_cross_calls(
        "function act() { drawTarget(lastState); }\n", foreign
    ) == ["drawTarget"]
    assert check_web_assets.bare_cross_calls(
        "function act() { ctx.sidebar.drawTarget(lastState); }\n", foreign
    ) == []
    assert check_web_assets.bare_cross_calls(
        "function refresh() {\n"
        "  const setRunning = ctx.composer.setRunning;\n"
        "  if (busy) { setRunning(false); }\n"
        "}\n",
        foreign,
    ) == []
    assert check_web_assets.bare_cross_calls(
        "function drawTarget(state) { return state; }\n",
        {"drawTarget"},
    ) == []


def test_每个_feature_由_main_注入() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_web_assets

    main = (ASSETS / "js" / "main.js").read_text(encoding="utf-8")
    for rel, factory in check_web_assets._FEATURE_INIT:
        body = (ASSETS / rel).read_text(encoding="utf-8")
        assert f"function {factory}(ctx)" in body
        assert f"{factory}(app)" in main
        assert "function init" not in body
    assert check_web_assets.bare_state_problems() == []
    assert check_web_assets.bare_state_reads("function act() { return running; }\n") == ["running"]
    assert check_web_assets.bare_state_reads(
        "function row(running) { return running; }\n"
    ) == []
    live = check_web_assets.state_names()
    assert "statusOpen" in live and "pendings" in live and "healthView" in live
    assert "const STATE_KEYS = Object.keys(INITIAL_STATE)" in (
        ASSETS / "js" / "core" / "store.js"
    ).read_text(encoding="utf-8")
    assert check_web_assets.bare_state_reads(
        "function act() { return statusOpen; }\n"
    ) == ["statusOpen"]


def test_新增_store_key_会自动进入裸读检查() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_web_assets

    sample = "const INITIAL_STATE = {\n  running: false,\n  inventedKey: 1,\n};\n"
    keys = check_web_assets.parse_state_keys(sample)
    assert keys == ["running", "inventedKey"]
    assert check_web_assets.bare_state_reads(
        "function act() { return inventedKey; }\n",
        names=keys,
    ) == ["inventedKey"]
    assert check_web_assets.bare_state_reads(
        "function act(inventedKey) { return inventedKey; }\n",
        names=keys,
    ) == []


def _browser() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    return shutil.which("chrome") or shutil.which("msedge") or shutil.which("chromium")


def _contrast(fg: str, bg: str) -> float:
    def channel(hex_color: str, index: int) -> float:
        value = int(hex_color.lstrip("#")[index : index + 2], 16) / 255
        if value <= 0.04045:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    def lum(hex_color: str) -> float:
        red, green, blue = channel(hex_color, 0), channel(hex_color, 2), channel(hex_color, 4)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    hi, lo = max(lum(fg), lum(bg)), min(lum(fg), lum(bg))
    return (hi + 0.05) / (lo + 0.05)


def test_对比度达到AA() -> None:
    """深色 / 浅色下，审计里不够的那几处都要到 4.5:1。"""
    assert _contrast("#ffffff", "#1f6b32") >= 4.5
    assert _contrast("#9b9b9b", "#212121") >= 4.5
    assert _contrast("#9b9b9b", "#171717") >= 4.5
    assert _contrast("#9b9b9b", "#0d0d0d") >= 4.5
    assert _contrast("#5d5d5d", "#ffffff") >= 4.5
    assert _contrast("#5d5d5d", "#f9f9f9") >= 4.5
    assert _contrast("#5d5d5d", "#f4f4f4") >= 4.5
    css = (ASSETS / "css" / "thread.css").read_text(encoding="utf-8")
    assert ".ask-apply { background:#1f6b32; color:#fff; }" in css
    tokens = (ASSETS / "css" / "tokens.css").read_text(encoding="utf-8")
    assert "--bubble-assistant:#f7f7f8;" in tokens
    assert "--text-dim:#9b9b9b;" in tokens
    assert "--text-dim:#5d5d5d;" in tokens


def test_三档宽度下浮层不压输入条(server: str, tmp_path: Path) -> None:
    browser = _browser()
    if browser is None:
        pytest.skip("没有 Chrome/Edge，无法量布局")
    sys.path.insert(0, str(ROOT / "recon"))
    from cdp import CDP

    port = 9347
    profile = tmp_path / "layout-profile"
    proc = subprocess.Popen(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            "--user-data-dir=" + str(profile),
            server + "/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        page = CDP(port)
        page.navigate(server + "/")
        ready = False
        for _ in range(40):
            try:
                if page.eval("!!document.getElementById('thinking')"):
                    ready = True
                    break
            except Exception:
                pass
            import time
            time.sleep(0.25)
        assert ready, "页面没有画出输入条"
        script = r"""
          (() => {
            const thinking = document.getElementById('thinking');
            const toBottom = document.getElementById('toBottom');
            const conn = document.getElementById('conn');
            const goal = document.getElementById('goal');
            const panel = document.getElementById('st-panel');
            const toasts = document.getElementById('toasts');
            thinking.classList.add('show');
            toBottom.classList.add('show');
            conn.style.display = 'block';
            goal.value = '一行任务\n'.repeat(8);
            goal.style.height = '160px';
            panel.classList.remove('hidden');
            toasts.classList.add('show');
            document.getElementById('toast-all').classList.add('show');
            const toast = document.createElement('div');
            toast.className = 'toast error';
            toast.id = 'probe-toast';
            toast.textContent = '最新错误';
            toasts.appendChild(toast);
            const ask = document.createElement('button');
            ask.className = 'ask-apply';
            ask.textContent = '应用';
            document.body.appendChild(ask);
            const act = document.createElement('button');
            act.className = 'row-act';
            act.textContent = '移除';
            document.body.appendChild(act);
            const closer = document.createElement('button');
            closer.className = 'toast-close';
            closer.textContent = '关';
            document.body.appendChild(closer);
            const boxOf = (el) => {
              const r = el.getBoundingClientRect();
              return {x: r.left, y: r.top, w: r.width, h: r.height};
            };
            const area = (a, b) => {
              const w = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
              const h = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
              return Math.round(w * h);
            };
            const hitId = (x, y) => {
              const node = document.elementFromPoint(x, y);
              let cur = node;
              while (cur) {
                if (cur.id) { return cur.id; }
                cur = cur.parentElement;
              }
              return node ? node.tagName : '';
            };
            const send = document.getElementById('send');
            const box = document.querySelector('.box');
            const sendBox = boxOf(send);
            const input = boxOf(box);
            const sizes = {};
            for (const id of ['st-health', 'menu', 'send', 'toast-all', 'st-close']) {
              const r = boxOf(document.getElementById(id));
              sizes[id] = {w: Math.round(r.w), h: Math.round(r.h)};
            }
            sizes['toast-close'] = {w: Math.round(boxOf(closer).w), h: Math.round(boxOf(closer).h)};
            sizes['ask-apply'] = {w: Math.round(boxOf(ask).w), h: Math.round(boxOf(ask).h)};
            sizes['row-act'] = {w: Math.round(boxOf(act).w), h: Math.round(boxOf(act).h)};
            const toastBox = boxOf(toast);
            const panelBox = boxOf(panel);
            return {
              send: sendBox,
              panel: panelBox,
              corner: boxOf(document.getElementById('corner')),
              thinking_box: area(boxOf(thinking), input),
              thinking_send: area(boxOf(thinking), sendBox),
              bottom_box: area(boxOf(toBottom), input),
              bottom_send: area(boxOf(toBottom), sendBox),
              conn_box: area(boxOf(conn), input),
              panel_toast: area(panelBox, toastBox),
              send_hit: hitId(sendBox.x + sendBox.w / 2, sendBox.y + sendBox.h / 2),
              send_stack: (document.elementsFromPoint(sendBox.x + sendBox.w / 2, sendBox.y + sendBox.h / 2) || []).slice(0, 6).map((node) => node.id || node.tagName),
              sidebar: boxOf(document.getElementById('sidebar')),
              drawer: document.body.className,
              side_transform: getComputedStyle(document.getElementById('sidebar')).transform,
              goal_box: boxOf(goal),
              goal_hit: hitId(boxOf(goal).x + boxOf(goal).w / 2, boxOf(goal).y + boxOf(goal).h / 2),
              toast_h: Math.round(toastBox.h),
              sizes: sizes,
            };
          })()
        """
        report = {}
        for width, height in ((390, 844), (1024, 768), (1440, 900)):
            page.viewport(width, height, mobile=width < 500)
            if width <= 860:
                page.eval(
                    "new Promise((resolve) => {"
                    " const start = performance.now();"
                    " const tick = () => {"
                    "  const edge = document.getElementById('sidebar').getBoundingClientRect().right;"
                    "  if (edge <= 1 || performance.now() - start > 1000) resolve(edge);"
                    "  else requestAnimationFrame(tick);"
                    " };"
                    " tick();"
                    "})"
                )
            else:
                page.eval(
                    "new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
            report[str(width)] = page.eval(script)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    for width, item in report.items():
        assert item["thinking_box"] == 0, (width, item)
        assert item["thinking_send"] == 0, (width, item)
        assert item["bottom_box"] == 0, (width, item)
        assert item["bottom_send"] == 0, (width, item)
        assert item["conn_box"] == 0, (width, item)
        assert item["panel_toast"] == 0, (width, item)
        assert item["send_hit"] == "send", (
            width, item["send_hit"], item["drawer"], item["side_transform"], item["sidebar"],
        )
        assert item["goal_hit"] == "goal", (width, item["goal_hit"], item["goal_box"], item["sidebar"])
        assert item["toast_h"] > 0, (width, item)
    mobile = report["390"]["sizes"]
    for name in ("st-health", "menu", "send", "toast-all", "st-close", "toast-close", "ask-apply", "row-act"):
        assert mobile[name]["w"] >= 44 and mobile[name]["h"] >= 44, (name, mobile[name])


def test_390任务卡单列且不溢出(server: str, tmp_path: Path) -> None:
    """窄屏任务卡保持单列，长摘要不把页面撑出横向滚动。"""
    browser = _browser()
    if browser is None:
        pytest.skip("没有 Chrome/Edge，无法量布局")
    sys.path.insert(0, str(ROOT / "recon"))
    from cdp import CDP

    port = 9348
    profile = tmp_path / "task-card-profile"
    proc = subprocess.Popen(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            "--user-data-dir=" + str(profile),
            server + "/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        page = CDP(port)
        page.navigate(server + "/")
        ready = False
        for _ in range(40):
            try:
                if page.eval("!!document.getElementById('stream')"):
                    ready = True
                    break
            except Exception:
                pass
            import time
            time.sleep(0.25)
        assert ready, "页面没有画出对话区"
        page.viewport(390, 844, mobile=True)
        page.eval(
            "new Promise((resolve) => {"
            " const start = performance.now();"
            " const tick = () => {"
            "  const edge = document.getElementById('sidebar').getBoundingClientRect().right;"
            "  if (edge <= 1 || performance.now() - start > 1000) resolve(edge);"
            "  else requestAnimationFrame(tick);"
            " };"
            " tick();"
            "})"
        )
        metrics = page.eval(
            r"""
            (() => {
              const col = document.querySelector('#stream .col') || document.getElementById('stream');
              const list = document.createElement('div');
              list.className = 'task-list';
              const card = document.createElement('div');
              card.className = 'task-card task-card-running';
              const head = document.createElement('button');
              head.className = 'task-head';
              head.type = 'button';
              head.setAttribute('aria-expanded', 'true');
              const goal = '写一个文件' + '很长的目标'.repeat(12);
              head.innerHTML = '<span class="task-mark task-chip">进行中</span><span class="task-goal"></span><span class="task-meta"><span class="task-steps task-chip">1 步</span><span class="task-verify task-chip">未验证</span></span><span class="task-chevron"></span>';
              const goalNode = head.querySelector('.task-goal');
              goalNode.textContent = goal;
              goalNode.title = goal;
              const body = document.createElement('div');
              body.className = 'task-body';
              const row = document.createElement('div');
              row.className = 'task-row task-observe';
              const summary = document.createElement('div');
              summary.className = 'task-summary';
              const summaryText = '摘要'.repeat(40);
              summary.textContent = summaryText;
              summary.title = summaryText;
              row.appendChild(summary);
              body.appendChild(row);
              for (let i = 0; i < 24; i += 1) {
                const extra = document.createElement('div');
                extra.className = 'task-row';
                extra.textContent = '第 ' + i + ' 行 ' + '内容'.repeat(8);
                body.appendChild(extra);
              }
              card.appendChild(head);
              card.appendChild(body);
              list.appendChild(card);
              col.appendChild(list);
              const stream = document.getElementById('stream');
              const cardBox = card.getBoundingClientRect();
              const streamBox = stream.getBoundingClientRect();
              const headStyle = getComputedStyle(head);
              const goalStyle = getComputedStyle(goalNode);
              const summaryStyle = getComputedStyle(summary);
              const bodyStyle = getComputedStyle(body);
              const goalBox = goalNode.getBoundingClientRect();
              const markBox = head.querySelector('.task-mark').getBoundingClientRect();
              return {
                card_right: Math.round(cardBox.right),
                stream_right: Math.round(streamBox.right),
                card_left: Math.round(cardBox.left),
                stream_left: Math.round(streamBox.left),
                page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                card_overflow: card.scrollWidth - card.clientWidth,
                head_display: headStyle.display,
                head_width: Math.round(head.getBoundingClientRect().width),
                goal_w: Math.round(goalBox.width),
                goal_top: Math.round(goalBox.top),
                mark_bottom: Math.round(markBox.bottom),
                goal_clamp: goalStyle.webkitLineClamp,
                summary_clamp: summaryStyle.webkitLineClamp,
                goal_title: goalNode.title,
                goal_h: Math.round(goalBox.height),
                summary_h: Math.round(summary.getBoundingClientRect().height),
                body_overflow_x: bodyStyle.overflowX,
                body_scrolls: body.scrollHeight > body.clientHeight + 1,
              };
            })()
            """
        )
        shot = tmp_path / "task-card-390.png"
        page.shot(str(shot))
        shot_copy = ROOT / ".tmp" / "task-card-390.png"
        shot_copy.parent.mkdir(parents=True, exist_ok=True)
        shot_copy.write_bytes(shot.read_bytes())
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert metrics["head_display"] == "grid", metrics
    assert metrics["goal_top"] >= metrics["mark_bottom"] - 1, metrics
    assert metrics["goal_w"] >= metrics["head_width"] - 28, metrics
    assert metrics["goal_clamp"] == "2", metrics
    assert metrics["summary_clamp"] == "2", metrics
    assert metrics["goal_title"].startswith("写一个文件")
    assert len(metrics["goal_title"]) > 40
    assert metrics["goal_h"] <= 48, metrics
    assert metrics["summary_h"] <= 48, metrics
    assert metrics["body_overflow_x"] in ("hidden", "clip", "auto"), metrics
    assert metrics["body_scrolls"] is True, metrics
    assert metrics["card_right"] <= metrics["stream_right"] + 1, metrics
    assert metrics["card_left"] >= metrics["stream_left"] - 1, metrics
    assert metrics["page_overflow"] <= 1, metrics
    assert metrics["card_overflow"] <= 1, metrics


def test_浏览器执行打包脚本(server: str, tmp_path: Path) -> None:
    browser = _browser()
    if browser is None:
        pytest.skip("没有 Chrome/Edge，无法做真实浏览器 smoke")
    profile = tmp_path / "browser-profile"
    # `--headless=new` 会等网络静默。页面有一条不结束的 EventSource，dump 就不会返回。
    # 旧 headless 在文档加载完就把 DOM 打出来，脚本已经跑过。
    done = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            "--user-data-dir=" + str(profile),
            "--dump-dom",
            server + "/",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    dom = done.stdout.decode("utf-8", "replace")
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    assert 'id="goal"' in dom
    assert 'id="sidebar"' in dom
    assert 'id="settings"' in dom
    assert "st-label" in dom
    assert 'src="/assets/dist/app.' in dom
    assert dom.find('src="/assets/dist/app.') < dom.find(".js", dom.find('src="/assets/dist/app.'))


def test_未知资源与路径穿越都是404(server: str) -> None:
    status, body, _headers = _get(server + "/assets/missing.js")
    assert status == 404
    assert "没有这个路径" in body.decode("utf-8")

    parsed = urlparse(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    for path in (
        "/assets/%2e%2e/%2e%2e/pyproject.toml",
        "/assets/css/../../pyproject.toml",
        "/assets/..%5Cpyproject.toml",
        "/assets/css",
    ):
        conn.request("GET", path)
        response = conn.getresponse()
        payload = response.read()
        assert response.status == 404, path
        assert b"[project]" not in payload
        assert b"name = " not in payload
    conn.close()


def test_目录和坏扩展名不列出() -> None:
    with pytest.raises(AssetError):
        load_asset("css")
    with pytest.raises(AssetError):
        load_asset("page.py")
    with pytest.raises(AssetError):
        load_asset("../page.py")
    with pytest.raises(AssetError):
        load_asset("%2e%2e/%2e%2e/pyproject.toml")
    with pytest.raises(AssetError):
        load_asset("css\\tokens.css")
    with pytest.raises(AssetError):
        load_asset("/etc/passwd")


def test_符号链接即使指向包内也拒绝(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    real = root / "real.js"
    real.write_text("var real = 1;\n", encoding="utf-8")
    alias = root / "alias.js"
    outside = tmp_path / "secret.js"
    outside.write_text("var secret = 1;\n", encoding="utf-8")
    try:
        alias.symlink_to(real)
        leaked = root / "leak.js"
        leaked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"这个环境建不了符号链接: {exc}")
    with pytest.raises(AssetError) as inside:
        load_asset("alias.js", root=root)
    assert inside.value.reason == "symlink"
    with pytest.raises(AssetError) as outside_link:
        load_asset("leak.js", root=root)
    assert outside_link.value.reason == "symlink"
    ok = load_asset("real.js", root=root)
    assert b"var real" in ok.body


def test_wheel带上资源且安装布局能开首页(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv 不在 PATH，打不出 wheel")
    out = tmp_path / "wheels"
    # 沙箱里 `%LOCALAPPDATA%\uv\cache` 可能不可写；把 uv 缓存钉在测试临时目录，
    # 否则这条测试会因为环境权限失败，看起来像打包坏了。
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    done = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr or done.stdout
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        archive.extractall(tmp_path / "site")
    assert "spoolkit/web/assets/index.html" in names
    assert any(
        name.startswith("spoolkit/web/assets/dist/app.") and name.endswith(".js")
        for name in names
    )
    assert "spoolkit/web/assets/js/main.js" in names

    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path / "site")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    probe = subprocess.run(
        [sys.executable, "-c", _WHEEL_PROBE, str(tmp_path / "ws")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    assert "wheel-ok" in probe.stdout


_WHEEL_PROBE = r"""
import sys
import threading
import urllib.request
from pathlib import Path

from spoolkit.web.page import HTML, INDEX
from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server
from spoolkit.web.static import load_asset

assert "<style" not in INDEX.lower()
assert "function applyState" in HTML
assert "<link" not in HTML.lower()
asset = load_asset("js/main.js")
assert b"function applyState" in asset.body

workspace = Path(sys.argv[1])
workspace.mkdir()
runner = Runner(workspace, session="t", extra_args=["--provider", "fake", "--script", ""])
httpd = build_server(runner, "127.0.0.1", 0)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{httpd.server_port}"
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        page = response.read().decode("utf-8")
        assert response.status == 200
    assert 'id="st-mode"' in page
    marker = '<script src="'
    start = page.find(marker)
    assert start != -1
    src = page[start + len(marker) : page.find('"', start + len(marker))]
    assert src.startswith("/assets/dist/app.") and src.endswith(".js")
    with urllib.request.urlopen(base + src, timeout=10) as response:
        script = response.read()
        assert response.status == 200
    assert b"function applyState" in script
finally:
    httpd.shutdown()
    httpd.server_close()
print("wheel-ok")
"""
