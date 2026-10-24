"""网页壳静态资源：读取、拼装、路径白名单。

源码在包内的 ``assets/``。运行时用 ``importlib.resources`` 读，不看当前工作目录。

两份页面：

- ``index.html`` 给浏览器，样式和脚本走 ``/assets``；
- ``compat_html()`` 把同一份文件拼成内联文档，给还在读 ``HTML`` 字符串的测试。

脚本按经典脚本拼在一起（剥掉 ``import`` / ``export``）。模块之间的 ``let``
要能互相赋值，原生 ESM 的导入绑定是只读的，所以生产包不是按文件加载的 ESM。
``import`` 只用来标依赖方向，打包时去掉。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email.utils import formatdate
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

ALLOWED_SUFFIXES = frozenset({
    ".html", ".css", ".js", ".svg", ".png", ".webp", ".woff2", ".json",
})

CSS_ORDER = (
    "css/tokens.css",
    "css/base.css",
    "css/overlays.css",
    "css/layout.css",
    "css/sidebar.css",
    "css/settings.css",
    "css/thread.css",
    "css/tasks.css",
    "css/composer.css",
    "css/responsive.css",
)

# 先放会在加载时执行的绑定（DOM 常量、状态、徽标节点），入口的启动调用放最后。
JS_ORDER = (
    "js/core/dom.js",
    "js/core/store.js",
    "js/core/ctx.js",
    "js/core/format.js",
    "js/render/markdown.js",
    "js/render/failure.js",
    "js/render/trace_box.js",
    "js/render/task_card.js",
    "js/render/artifact_list.js",
    "js/render/diff.js",
    "js/render/code.js",
    "js/features/toasts.js",
    "js/features/status.js",
    "js/features/settings.js",
    "js/features/sessions.js",
    "js/features/thread.js",
    "js/features/tasks.js",
    "js/features/confirm.js",
    "js/features/sidebar.js",
    "js/features/composer.js",
    "js/core/transport.js",
    "js/main.js",
)

TEST_ENTRY = "js/test-entry.js"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
}

_SIDE_IMPORT = re.compile(r"""^import\s+["'][^"']+["'];\s*$""")
_EXPORT_LIST = re.compile(r"""^export\s+\{[^}]*\};\s*$""")
_ANY_MODULE = re.compile(r"""^\s*(?:import|export)\b""")
_LINK_TAG = re.compile(r"[ \t]*<link\b[^>]*>\r?\n?", re.IGNORECASE)
_EXTERNAL_SCRIPT = re.compile(
    r"[ \t]*<script\b[^>]*\bsrc\s*=[^>]*>\s*</script>\r?\n?",
    re.IGNORECASE,
)
_HASHED = re.compile(r"^dist/app\.[0-9a-f]{12}\.(?:css|js)$")


class AssetError(Exception):
    """路径不在白名单里，或目标不是一个普通文件。调用方按 404 处理。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Asset:
    body: bytes
    mime: str
    etag: str
    last_modified: str
    cache_control: str


def assets_root() -> Traversable:
    return files("spoolkit.web").joinpath("assets")


def normalize_rel(raw: str) -> str:
    """把 URL 路径收成包内相对路径。穿越、绝对路径、非白名单扩展名都拒绝。"""
    text = unquote((raw or "").split("?", 1)[0].split("#", 1)[0])
    if not text or "\x00" in text or "\\" in text or text.startswith(("/", "\\")):
        raise AssetError("bad")
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise AssetError("absolute")
    parts = pure.parts
    if not parts:
        raise AssetError("empty")
    for part in parts:
        if part in ("", ".", "..") or part.startswith(".") or ":" in part:
            raise AssetError("segment")
    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise AssetError("suffix")
    return "/".join(parts)


def _disk_root(root: Traversable) -> Path | None:
    if isinstance(root, Path):
        return root
    return None


def load_asset(url_path: str, root: Traversable | None = None) -> Asset:
    """读一个资源。``root`` 只给测试换目录；默认是包内 ``assets``。"""
    rel = normalize_rel(url_path)
    base = assets_root() if root is None else root
    disk = _disk_root(base)
    mtime: float | None = None
    if disk is not None:
        cursor = disk
        for part in PurePosixPath(rel).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AssetError("symlink")
        resolved_root = disk.resolve()
        resolved = cursor.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            raise AssetError("escape") from None
        if not cursor.is_file():
            raise AssetError("missing")
        data = cursor.read_bytes()
        mtime = cursor.stat().st_mtime
    else:
        node: Traversable = base
        for part in PurePosixPath(rel).parts:
            node = node.joinpath(part)
        if not node.is_file():
            raise AssetError("missing")
        data = node.read_bytes()
    suffix = PurePosixPath(rel).suffix.lower()
    digest = hashlib.sha256(data).hexdigest()
    modified = formatdate(mtime, usegmt=True) if mtime is not None else ""
    cache = "public, max-age=31536000, immutable" if _HASHED.match(rel) else "no-cache"
    return Asset(
        body=data,
        mime=MIME[suffix],
        etag='"' + digest[:16] + '"',
        last_modified=modified,
        cache_control=cache,
    )


def read_text(rel: str) -> str:
    """读包内文本，换行统一成 ``\\n``，哈希不随工作副本的 CRLF 变。"""
    data = load_asset(rel).body
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def strip_module_syntax(source: str) -> str:
    """去掉标依赖用的 import / export，留下同一份经典脚本能跑的声明。

    只接受整行的 ``import "./x.js";`` 和 ``export { a, b };``。
    named / default / 多行 import 会直接报错，避免 dist 里留下非法的 ``import``。
    """
    kept: list[str] = []
    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _SIDE_IMPORT.match(line) or _EXPORT_LIST.match(line):
            continue
        if _ANY_MODULE.match(line):
            raise RuntimeError(
                "经典脚本打包只接受 `import \"./x.js\";` 和 `export { a, b };`，"
                f"这一行不行：{line.strip()}"
            )
        kept.append(line)
    text = "\n".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def bundle_js(include_test: bool = False) -> str:
    order = list(JS_ORDER)
    if include_test:
        order.append(TEST_ENTRY)
    return "\n".join(strip_module_syntax(read_text(rel)) for rel in order)


def bundle_css() -> str:
    return "\n".join(read_text(rel) for rel in CSS_ORDER)


def index_html() -> str:
    return read_text("index.html")


def compat_html() -> str:
    """内联样式和经典脚本。没有 ``<link>``、没有 ``src=``，断网也能用。"""
    page = _LINK_TAG.sub("\n", index_html())
    page = _EXTERNAL_SCRIPT.sub("\n", page)
    style = "<style>\n" + bundle_css() + "</style>\n"
    script = "<script>\n" + bundle_js() + "</script>\n"
    page = page.replace("</head>", style + "</head>", 1)
    page = page.replace("</body>", script + "</body>", 1)
    lowered = page.lower()
    if "<link" in lowered or "src=" in lowered or "http://" in page or "https://" in page:
        raise RuntimeError("兼容页里还留着外链")
    if "<style>" not in lowered or "<script>" not in page:
        raise RuntimeError("兼容页没有拼上样式或脚本")
    return page
