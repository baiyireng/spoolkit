"""真实 Chrome 里用 Tab / Enter / Space / Escape 走侧栏，并在抽屉收起动画中点输入框。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.web.server import build_server, build_state

ROOT = Path(__file__).resolve().parents[2]


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
    import shutil

    return shutil.which("chrome") or shutil.which("msedge") or shutil.which("chromium")


def _workspace(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True)
    kernel_workspaces.register(root)
    return root.resolve()


def _key(page, key: str) -> None:
    table = {
        "Tab": (9, "Tab", ""),
        "Enter": (13, "Enter", "\r"),
        " ": (32, "Space", " "),
        "Escape": (27, "Escape", ""),
    }
    code, name, text = table[key]
    down = {
        "type": "keyDown",
        "key": key,
        "code": name,
        "windowsVirtualKeyCode": code,
        "nativeVirtualKeyCode": code,
    }
    if text:
        down["text"] = text
    page.cmd("Input.dispatchKeyEvent", down)
    page.cmd("Input.dispatchKeyEvent", {
        "type": "keyUp",
        "key": key,
        "code": name,
        "windowsVirtualKeyCode": code,
        "nativeVirtualKeyCode": code,
    })


def _eval(page, expression: str):
    return page.eval(expression)


def _wait_until(page, expression: str, timeout: float = 8):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = page.eval(expression)
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(0.05)
    raise AssertionError(last)


def _focus_of(page) -> dict:
    return page.eval(
        """(() => {
          const node = document.activeElement;
          if (!node || node === document.body || node === document.documentElement) {
            return {id: '', tag: node ? node.tagName : '', role: '', kind: '',
                    outlineStyle: '', outlineWidth: '', visible: false, inside: ''};
          }
          const style = getComputedStyle(node);
          const side = document.getElementById('sidebar');
          return {
            id: node.id || '',
            tag: node.tagName,
            role: node.getAttribute('role') || '',
            kind: (node.dataset && node.dataset.kind) || '',
            outlineStyle: style.outlineStyle,
            outlineWidth: style.outlineWidth,
            visible: node.matches(':focus-visible'),
            inside: side && side.contains(node) ? 'sidebar' : '',
          };
        })()"""
    )


def _tab(page) -> dict:
    _key(page, "Tab")
    return _focus_of(page)


def test_真实Chrome键盘链路与抽屉动画命中(tmp_path: Path) -> None:
    browser = _browser()
    if browser is None:
        pytest.skip("没有 Chrome/Edge，无法做真实键盘")
    sys.path.insert(0, str(ROOT / "recon"))
    from cdp import CDP

    # 登记表里有两个工作区时内核不肯猜目标，左栏就没有当前会话行。
    # 这里只登记一个，页面才能同时出现工作区行和会话行。
    _workspace(tmp_path, "alpha")
    state = build_state(extra_args=["--provider", "fake", "--script", ""])
    httpd = build_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server = f"http://127.0.0.1:{httpd.server_port}"
    port = 9351
    profile = tmp_path / "keyboard-profile"
    proc = subprocess.Popen(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        page = CDP(port)
        try:
            page.cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:
            pass
        page.viewport(1280, 800, mobile=False)
        page.navigate(server + "/")
        ready = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                ready = page.eval(
                    """(() => {
                      const where = document.getElementById('where');
                      const sessions = document.getElementById('sessions');
                      const snap = {
                        goal: !!document.getElementById('goal'),
                        sidebar: !!document.getElementById('sidebar'),
                        settings: !!document.getElementById('settings'),
                        ws: document.querySelectorAll('#workspaces [data-kind="ws"]').length,
                        sess: document.querySelectorAll('#sessions [data-kind="sess"]').length,
                        where: where ? where.textContent : '',
                        sessions: sessions ? sessions.textContent.slice(0, 240) : '',
                        ready: document.readyState,
                      };
                      snap.ok = snap.goal && snap.sidebar && snap.settings && snap.ws >= 1 && snap.sess >= 1;
                      return snap;
                    })()"""
                )
            except Exception as exc:  # noqa: BLE001
                ready = repr(exc)
            if isinstance(ready, dict) and ready.get("ok"):
                break
            time.sleep(0.05)
        assert isinstance(ready, dict) and ready.get("ok"), json.dumps(ready, ensure_ascii=False)
        page.eval(
            """(() => {
              window.__spoolPosts = [];
              const native = window.fetch.bind(window);
              window.fetch = function (url, opts) {
                const path = String(url);
                if (path.indexOf('/target') !== -1) {
                  window.__spoolPosts.push({
                    url: path,
                    body: opts && opts.body ? String(opts.body) : '',
                  });
                }
                return native(url, opts);
              };
            })()"""
        )
        sequence = []
        health = None
        for _ in range(10):
            item = _tab(page)
            sequence.append(item)
            if item["id"] == "st-health":
                health = item
                break
        assert health, sequence
        _key(page, "Enter")
        opened = page.eval(
            """(() => {
              const panel = document.getElementById('st-panel');
              const first = panel.firstElementChild;
              return {
                open: !panel.classList.contains('hidden'),
                first: first ? first.id : '',
                expanded: document.getElementById('st-health').getAttribute('aria-expanded'),
              };
            })()"""
        )
        assert opened == {"open": True, "first": "st-close", "expanded": "true"}, opened
        close_focus = _tab(page)
        sequence.append(close_focus)
        assert close_focus["id"] == "st-close", (close_focus, sequence)

        row = None
        for _ in range(24):
            item = _tab(page)
            sequence.append(item)
            if item["tag"] == "SELECT":
                raise AssertionError(sequence)
            if item["kind"] in {"ws", "sess"}:
                row = item
                break
        assert row, sequence
        assert row["visible"] is True, row
        assert row["outlineStyle"] != "none", row
        assert float(str(row["outlineWidth"]).replace("px", "") or 0) >= 2, row
        _key(page, " ")
        posts = _wait_posts(page)
        assert posts, sequence
        assert "/target" in posts[0]["url"], posts
        other = "sess" if row["kind"] == "ws" else "ws"
        second = None
        for _ in range(24):
            item = _tab(page)
            sequence.append(item)
            if item["tag"] == "SELECT":
                raise AssertionError(sequence)
            if item["kind"] == other:
                second = item
                break
        assert second, sequence
        before = len(posts)
        _key(page, "Enter")
        posts = _wait_posts(page, before + 1)
        assert len(posts) >= before + 1, posts
        assert "/target" in posts[-1]["url"], posts
        hidden = page.eval(
            """(() => {
              const nodes = [...document.querySelectorAll('select')];
              return nodes.map((node) => ({
                id: node.id,
                tab: node.tabIndex,
                display: getComputedStyle(node).display,
              }));
            })()"""
        )
        assert hidden == [], hidden

        page.viewport(390, 844, mobile=True)
        page.navigate(server + "/")
        _wait_until(
            page,
            "!!document.getElementById('menu')"
            " && !!document.querySelector('#workspaces [data-kind=\"ws\"]')"
            " && document.getElementById('sidebar').hasAttribute('inert')",
        )
        closed_tabs = []
        menu = None
        for _ in range(12):
            item = _tab(page)
            closed_tabs.append(item)
            if item["inside"] == "sidebar" or item["kind"] in {"ws", "sess"}:
                raise AssertionError(closed_tabs)
            if item["id"] == "menu":
                menu = item
                break
        assert menu, closed_tabs
        _key(page, "Enter")
        drawer = page.eval(
            """(() => {
              const node = document.activeElement;
              const side = document.getElementById('sidebar');
              return {
                expanded: document.getElementById('menu').getAttribute('aria-expanded'),
                focus: node ? node.id : '',
                inside: !!(node && side && side.contains(node)),
                inert: side.hasAttribute('inert'),
                hidden: side.getAttribute('aria-hidden'),
                events: getComputedStyle(side).pointerEvents,
              };
            })()"""
        )
        assert drawer["expanded"] == "true", drawer
        assert drawer["inside"] is True, drawer
        assert drawer["inert"] is False, drawer
        assert drawer["events"] == "auto", drawer
        _key(page, "Escape")
        shut = page.eval(
            """(() => {
              const node = document.activeElement;
              const side = document.getElementById('sidebar');
              return {
                expanded: document.getElementById('menu').getAttribute('aria-expanded'),
                focus: node ? node.id : '',
                drawer: document.body.classList.contains('drawer'),
                inert: side.hasAttribute('inert'),
                hidden: side.getAttribute('aria-hidden'),
                events: getComputedStyle(side).pointerEvents,
              };
            })()"""
        )
        assert shut["drawer"] is False, shut
        assert shut["expanded"] == "false", shut
        assert shut["focus"] == "menu", shut
        assert shut["inert"] is True, shut
        assert shut["hidden"] == "true", shut
        assert shut["events"] == "none", shut

        mid = page.eval(
            """new Promise((resolve) => {
              const side = document.getElementById('sidebar');
              const goal = document.getElementById('goal');
              const started = performance.now();
              document.body.classList.add('drawer');
              const openTick = () => {
                const box = side.getBoundingClientRect();
                if (box.left >= -1 || performance.now() - started > 600) {
                  document.body.classList.remove('drawer');
                  requestAnimationFrame(() => {
                    const sideBox = side.getBoundingClientRect();
                    const goalBox = goal.getBoundingClientRect();
                    const y = goalBox.top + Math.min(24, goalBox.height / 2);
                    const overlaps = sideBox.right > goalBox.left + 8
                      && sideBox.left < goalBox.right
                      && sideBox.top < y && sideBox.bottom > y;
                    const x = Math.min(goalBox.left + 12, Math.max(goalBox.left + 4, sideBox.right - 8));
                    const node = document.elementFromPoint(x, y);
                    let hit = '';
                    let cur = node;
                    while (cur) {
                      if (cur.id) { hit = cur.id; break; }
                      cur = cur.parentElement;
                    }
                    resolve({
                      overlaps: overlaps,
                      hit: hit,
                      events: getComputedStyle(side).pointerEvents,
                      side: {x: sideBox.left, right: sideBox.right, w: sideBox.width},
                      goal: {x: goalBox.left, w: goalBox.width, y: goalBox.top},
                      x: x, y: y,
                    });
                  });
                  return;
                }
                requestAnimationFrame(openTick);
              };
              requestAnimationFrame(openTick);
            })"""
        )
        assert mid["overlaps"] is True, mid
        assert mid["events"] == "none", mid
        assert mid["hit"] == "goal", mid
        page.viewport(1440, 900, mobile=False)
        _wait_until(
            page,
            "(() => { const side = document.getElementById('sidebar');"
            " return getComputedStyle(side).pointerEvents === 'auto' && !side.hasAttribute('inert'); })()",
        )
        desktop = page.eval(
            """(() => {
              const side = document.getElementById('sidebar');
              return {
                events: getComputedStyle(side).pointerEvents,
                inert: side.hasAttribute('inert'),
              };
            })()"""
        )
        assert desktop["events"] == "auto", desktop
        assert desktop["inert"] is False, desktop
    finally:
        proc.kill()
        proc.wait(timeout=10)
        httpd.shutdown()
        httpd.server_close()
        close = getattr(state, "close", None)
        if callable(close):
            close()


def _wait_posts(page, count: int = 1):
    end = time.time() + 3
    posts = []
    while time.time() < end:
        posts = page.eval("window.__spoolPosts") or []
        if len(posts) >= count:
            return posts
        time.sleep(0.05)
    return posts
