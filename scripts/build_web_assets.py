"""把 ``assets/js`` 与 ``assets/css`` 收成 ``assets/dist``。

生产包是经典脚本（见 ``spoolkit.web.static``）。本脚本是字节的生产者：
esbuild 只在 ``web/build.mjs`` 里做语法检查，不改写 dist，避免严格模式把
共享的 ``let`` 拆开。

用法（仓库根）::

    python scripts/build_web_assets.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spoolkit.web.static import bundle_css, bundle_js  # noqa: E402

ASSETS = ROOT / "src" / "spoolkit" / "web" / "assets"
DIST = ASSETS / "dist"
INDEX = ASSETS / "index.html"

_LINK = re.compile(
    r'(<link rel="stylesheet" href=")/assets/dist/app(?:\.[0-9a-f]{12})?\.css(">)'
)
_SCRIPT = re.compile(
    r'(<script src=")/assets/dist/app(?:\.[0-9a-f]{12})?\.js(" defer></script>)'
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return
    path.write_bytes(data)


def main() -> int:
    js = bundle_js()
    css = bundle_css()
    test_js = bundle_js(include_test=True)
    js_hash = hashlib.sha256(js.encode("utf-8")).hexdigest()[:12]
    css_hash = hashlib.sha256(css.encode("utf-8")).hexdigest()[:12]
    js_name = f"app.{js_hash}.js"
    css_name = f"app.{css_hash}.css"

    DIST.mkdir(parents=True, exist_ok=True)
    _write(DIST / "app.js", js)
    _write(DIST / "app.css", css)
    _write(DIST / js_name, js)
    _write(DIST / css_name, css)
    _write(DIST / "test-bundle.js", test_js)

    keep = {"app.js", "app.css", "test-bundle.js", js_name, css_name}
    for path in DIST.iterdir():
        if path.name not in keep and path.suffix in {".js", ".css"}:
            path.unlink()

    text = INDEX.read_bytes().decode("utf-8")
    updated, link_hits = _LINK.subn(rf"\1/assets/dist/{css_name}\2", text, count=1)
    updated, script_hits = _SCRIPT.subn(
        rf"\1/assets/dist/{js_name}\2", updated, count=1
    )
    if link_hits != 1 or script_hits != 1:
        print("index.html 里没有样式或脚本标签", file=sys.stderr)
        return 1
    _write(INDEX, updated)

    manifest = {
        "css": f"dist/{css_name}",
        "js": f"dist/{js_name}",
        "test": "dist/test-bundle.js",
    }
    _write(ASSETS / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(f"css {css_name}")
    print(f"js  {js_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
