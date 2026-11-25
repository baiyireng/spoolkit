"""网页壳资源的结构检查：行数、依赖方向、外链、动态 innerHTML、dist 是否漂移。

用法（仓库根）::

    python scripts/check_web_assets.py
"""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spoolkit.web.static import (  # noqa: E402
    CSS_ORDER,
    JS_ORDER,
    TEST_ENTRY,
    bundle_css,
    bundle_js,
    compat_html,
)

ASSETS = ROOT / "src" / "spoolkit" / "web" / "assets"
JS_ROOT = ASSETS / "js"
PAGE = ROOT / "src" / "spoolkit" / "web" / "page.py"
LINE_CAP = 600
PAGE_CAP = 120

_IMPORT = re.compile(r"""^import\s+["']([^"']+)["'];\s*$""", re.MULTILINE)
_INNER = re.compile(r"\.innerHTML\s*=\s*([^;\n]+)")
_INNER_OK = re.compile(r"^ICON\.[A-Za-z]+$|^icon$")
_REMOTE = ("http://", "https://", "cdn.jsdelivr", "unpkg.com", "fonts.googleapis", "fonts.gstatic")

_LAYER_OK = {
    "core": {"core"},
    "render": {"core", "render"},
    "features": {"core", "render"},
    "main": {"core", "render", "features"},
    "test": {"main"},
}


def _rel(path: Path) -> str:
    return path.relative_to(ASSETS).as_posix()


def _layer(rel: str) -> str:
    if rel == "js/main.js":
        return "main"
    if rel == TEST_ENTRY:
        return "test"
    if rel.startswith("js/core/"):
        return "core"
    if rel.startswith("js/render/"):
        return "render"
    if rel.startswith("js/features/"):
        return "features"
    raise RuntimeError(rel)


def _lines(path: Path) -> int:
    return len(path.read_bytes().decode("utf-8").splitlines())


def _imports(path: Path) -> list[str]:
    text = path.read_bytes().decode("utf-8")
    found: list[str] = []
    for spec in _IMPORT.findall(text):
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(_rel(path)), spec))
        found.append(joined)
    return found


def _graph() -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for path in sorted(JS_ROOT.rglob("*.js")):
        graph[_rel(path)] = _imports(path)
    graph.setdefault("js/main.js", _imports(ASSETS / "js" / "main.js"))
    return graph


def _cycles(graph: dict[str, list[str]]) -> list[str]:
    color: dict[str, int] = {}
    found: list[str] = []

    def walk(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            state = color.get(nxt, 0)
            if state == 0:
                walk(nxt, stack)
            elif state == 1:
                found.append(" -> ".join(stack[stack.index(nxt):] + [nxt]))
        stack.pop()
        color[node] = 2

    for node in graph:
        if color.get(node, 0) == 0:
            walk(node, [])
    return found


_FUNC_DECL = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
_CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")
_SHADOW_NAME = re.compile(r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*ctx\.")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_STORE_JS = ASSETS / "js" / "core" / "store.js"
_INITIAL_MARK = "const INITIAL_STATE = {"
_STATE_KEYS_LINE = "const STATE_KEYS = Object.keys(INITIAL_STATE)"
_FEATURE_INIT = (
    ("js/features/toasts.js", "createToasts"),
    ("js/features/status.js", "createStatus"),
    ("js/features/settings.js", "createSettings"),
    ("js/features/sessions.js", "createSessions"),
    ("js/features/thread.js", "createThread"),
    ("js/features/tasks.js", "createTasks"),
    ("js/features/confirm.js", "createConfirm"),
    ("js/features/sidebar.js", "createSidebar"),
    ("js/features/composer.js", "createComposer"),
)


def _mask_source(text: str) -> str:
    """把注释和字符串换成空格，调用扫描就不会把说明文字当成调用。"""
    out: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        nxt = text[index + 1] if index + 1 < size else ""
        if char == "/" and nxt == "/":
            while index < size and text[index] != "\n":
                out.append(" ")
                index += 1
            continue
        if char == "/" and nxt == "*":
            out.extend("  ")
            index += 2
            while index < size and not (
                text[index] == "*" and index + 1 < size and text[index + 1] == "/"
            ):
                out.append("\n" if text[index] == "\n" else " ")
                index += 1
            if index < size:
                out.extend("  ")
                index += 2
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(" ")
            index += 1
            while index < size:
                if text[index] == "\\":
                    out.append(" ")
                    index += 1
                    if index < size:
                        out.append("\n" if text[index] == "\n" else " ")
                        index += 1
                    continue
                if text[index] == quote:
                    out.append(" ")
                    index += 1
                    break
                out.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _shadowed(masked: str, at: int, name: str) -> bool:
    """调用点所在函数里有 `const name = ctx.` 时，这不是裸的全局调用。"""
    depth = 0
    index = at - 1
    while index >= 0:
        char = masked[index]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                head = masked[max(0, index - 180) : index]
                if re.search(
                    r"(?:async\s+)?function(?:\s+[A-Za-z_$][\w$]*)?\s*\([^)]*\)\s*$",
                    head,
                ):
                    body = masked[index:at]
                    return name in _SHADOW_NAME.findall(body)
            else:
                depth -= 1
        index -= 1
    return False


def bare_cross_calls(text: str, foreign: set[str]) -> list[str]:
    """返回 text 里对 foreign 这些函数的裸调用（前面不是 `.`，也不是本函数的 ctx 别名）。"""
    masked = _mask_source(text)
    found: list[str] = []
    for match in _CALL.finditer(masked):
        name = match.group(1)
        if name not in foreign:
            continue
        prefix = masked[max(0, match.start() - 20) : match.start()]
        if re.search(r"(?:async\s+)?function\s+$", prefix):
            continue
        if _shadowed(masked, match.start(), name):
            continue
        found.append(name)
    return found


def _skip_initializer(masked: str, index: int) -> int:
    """从 `=` 后面开始，跳到这个初始化表达式结束（逗号或分号，括号配平）。"""
    depth = 0
    size = len(masked)
    while index < size:
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and char in ",;":
            return index
        index += 1
    return index


def _bind_names(masked: str, index: int) -> tuple[set[str], int]:
    """读掉 `const/let/var` 后面的绑定，返回（名字，停在声明结束处）。"""
    names: set[str] = set()
    size = len(masked)
    while index < size and masked[index] in " \t\r\n":
        index += 1
    while index < size:
        match = _IDENT.match(masked, index)
        if match is None:
            break
        names.add(match.group(0))
        index = match.end()
        while index < size and masked[index] in " \t\r\n":
            index += 1
        if index < size and masked[index] == "=":
            index = _skip_initializer(masked, index + 1)
            while index < size and masked[index] in " \t\r\n":
                index += 1
        if index < size and masked[index] == ",":
            index += 1
            while index < size and masked[index] in " \t\r\n":
                index += 1
            continue
        break
    return names, index


def _keyword_at(masked: str, index: int, word: str) -> bool:
    if not masked.startswith(word, index):
        return False
    end = index + len(word)
    if end < len(masked) and (masked[end].isalnum() or masked[end] == "_"):
        return False
    before = masked[index - 1] if index else ""
    return not (before.isalnum() or before == "_")


def parse_state_keys(text: str) -> list[str]:
    """读出 INITIAL_STATE 的顶层 key。嵌套对象、字符串和注释里的名字不算。"""
    at = text.find(_INITIAL_MARK)
    if at < 0:
        raise ValueError("缺少 const INITIAL_STATE")
    keys: list[str] = []
    index = at + len(_INITIAL_MARK) - 1
    depth = 0
    size = len(text)
    while index < size:
        char = text[index]
        nxt = text[index + 1] if index + 1 < size else ""
        if char == "/" and nxt == "/":
            while index < size and text[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            while index < size:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                break
            index += 1
            continue
        if depth == 1 and (char.isalpha() or char in "_$"):
            end = index + 1
            while end < size and (text[end].isalnum() or text[end] in "_$"):
                end += 1
            name = text[index:end]
            cursor = end
            while cursor < size and text[cursor] in " \t":
                cursor += 1
            if cursor < size and text[cursor] == ":":
                keys.append(name)
            index = end
            continue
        index += 1
    if not keys:
        raise ValueError("INITIAL_STATE 没有 key")
    return keys


def state_names(text: str | None = None) -> frozenset[str]:
    """store 初始 key 的唯一来源。正式文件必须用 Object.keys 导出，避免两份名单。"""
    source = _STORE_JS.read_text(encoding="utf-8") if text is None else text
    if text is None and _STATE_KEYS_LINE not in source:
        raise ValueError("STATE_KEYS 必须由 Object.keys(INITIAL_STATE) 得到")
    return frozenset(parse_state_keys(source))


def bare_state_reads(
    text: str,
    assign_only: bool = False,
    names: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """状态名的裸出现。局部绑定、参数、对象键、`.name` 和字符串都不算。"""
    wanted = state_names() if names is None else frozenset(names)
    masked = _mask_source(text)
    scopes: list[set[str]] = [set()]
    found: list[str] = []
    size = len(masked)
    index = 0
    while index < size:
        char = masked[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == "{":
            scopes.append(set())
            index += 1
            continue
        if char == "}":
            if len(scopes) > 1:
                scopes.pop()
            index += 1
            continue
        header = re.match(
            r"(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)\s*\{",
            masked[index:],
        )
        if header is None:
            header = re.match(r"function\s*\(([^)]*)\)\s*\{", masked[index:])
        if header is not None:
            params = {
                part.strip()
                for part in header.group(1).split(",")
                if part.strip() and re.match(r"^[A-Za-z_$][\w$]*$", part.strip())
            }
            scopes.append(params)
            index += header.end()
            continue
        declared_here = False
        for word in ("const", "let", "var"):
            if _keyword_at(masked, index, word):
                names, index = _bind_names(masked, index + len(word))
                if scopes:
                    scopes[-1].update(names)
                declared_here = True
                break
        if declared_here:
            continue
        match = _IDENT.match(masked, index)
        if match is None:
            index += 1
            continue
        name = match.group(0)
        before = masked[index - 1] if index else ""
        after_at = match.end()
        while after_at < size and masked[after_at] in " \t\r\n":
            after_at += 1
        after = masked[after_at] if after_at < size else ""
        local = any(name in scope for scope in scopes)
        if (
            name in wanted
            and before != "."
            and after != ":"
            and not local
            and (not assign_only or after == "=")
        ):
            found.append(name)
        index = match.end()
    return found


def cross_feature_problems() -> list[str]:
    """feature 之间直接叫对方的函数，算边界破了。"""
    declared: dict[str, set[str]] = {}
    texts: dict[str, str] = {}
    problems: list[str] = []
    for rel, init in _FEATURE_INIT:
        path = ASSETS / rel
        if not path.is_file():
            problems.append(f"缺少 {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        texts[rel] = text
        declared[rel] = set(_FUNC_DECL.findall(text))
        if f"function {init}(" not in text:
            problems.append(f"{rel} 缺少 {init}(ctx)")
    owner: dict[str, str] = {}
    for rel, names in declared.items():
        for name in sorted(names):
            previous = owner.get(name)
            if previous is not None:
                problems.append(f"{name}() 同时在 {previous} 和 {rel} 声明")
            else:
                owner[name] = rel
    for rel, text in texts.items():
        foreign = {name for name, src in owner.items() if src != rel}
        for name in bare_cross_calls(text, foreign):
            problems.append(f"{rel} 直接调用了 {owner[name]} 的 {name}()，要改成 ctx")
    return problems


def bare_state_problems() -> list[str]:
    """feature 里不许裸读写页面状态；main 以外也不许直接给这些名字赋值。"""
    problems: list[str] = []
    for rel, _init in _FEATURE_INIT:
        text = (ASSETS / rel).read_text(encoding="utf-8")
        for name in bare_state_reads(text):
            problems.append(f"{rel} 裸用了 {name}，要走 ctx.store")
    feature_rels = {rel for rel, _init in _FEATURE_INIT}
    for path in JS_ROOT.rglob("*.js"):
        rel = _rel(path)
        if rel in feature_rels or rel in {"js/main.js", "js/core/store.js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name in bare_state_reads(text, assign_only=True):
            problems.append(f"{rel} 直接给 {name} 赋值，要走 store")
    return problems


def _scan_inner(path: Path, problems: list[str]) -> None:
    text = path.read_bytes().decode("utf-8")
    for match in _INNER.finditer(text):
        rhs = match.group(1).strip()
        if not _INNER_OK.match(rhs):
            problems.append(f"{_rel(path)}: 动态 innerHTML ({rhs})")
    for token in ("insertAdjacentHTML", "document.write", "eval("):
        if token in text:
            problems.append(f"{_rel(path)}: 出现 {token}")


def main() -> int:
    problems: list[str] = []
    page_lines = _lines(PAGE)
    if page_lines >= PAGE_CAP:
        problems.append(f"page.py {page_lines} 行，要小于 {PAGE_CAP}")
    page_text = PAGE.read_text(encoding="utf-8")
    if "<style" in page_text or "function applyState" in page_text:
        problems.append("page.py 里还写着样式或脚本")

    sources = [ASSETS / rel for rel in (*CSS_ORDER, *JS_ORDER, TEST_ENTRY)]
    for path in sources:
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(f"缺少或是空文件: {path.name}")
            continue
        count = _lines(path)
        if count > LINE_CAP:
            problems.append(f"{_rel(path)} {count} 行，超过 {LINE_CAP}")
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bTODO\b", text):
            problems.append(f"{_rel(path)} 有 TODO")
        if "export {}" in text:
            problems.append(f"{_rel(path)} 是空导出")

    present = sorted(_rel(path) for path in JS_ROOT.rglob("*.js"))
    expected = sorted([*JS_ORDER, TEST_ENTRY])
    if present != expected:
        problems.append(f"JS 文件和打包顺序不一致: {present} vs {expected}")

    graph = _graph()
    for src, deps in graph.items():
        src_layer = _layer(src)
        for dep in deps:
            if dep == TEST_ENTRY:
                problems.append(f"{src} 引了测试入口")
            if not (ASSETS / dep).is_file():
                problems.append(f"{src} 引了不存在的 {dep}")
                continue
            dep_layer = _layer(dep)
            if dep_layer not in _LAYER_OK[src_layer]:
                problems.append(f"{src} ({src_layer}) 不能依赖 {dep} ({dep_layer})")
    for cycle in _cycles(graph):
        problems.append(f"循环依赖: {cycle}")

    main_js = (ASSETS / "js" / "main.js").read_text(encoding="utf-8")
    if re.search(r"^\s*import\b", main_js, re.MULTILINE):
        problems.append("js/main.js 不能写 import：执行顺序只认 static.JS_ORDER")
    if "function wireFeatures(" not in main_js or "wireFeatures();" not in main_js:
        problems.append("js/main.js 缺少 wireFeatures()")
    for _rel_name, init in _FEATURE_INIT:
        if f"{init}(app)" not in main_js:
            problems.append(f"wireFeatures 没有调用 {init}(app)")
    problems.extend(cross_feature_problems())
    problems.extend(bare_state_problems())

    index = (ASSETS / "index.html").read_text(encoding="utf-8")
    if "<style" in index.lower():
        problems.append("index.html 有内联样式")
    for tag in re.findall(r"<script\b[^>]*>", index, flags=re.IGNORECASE):
        if "src=" not in tag.lower():
            problems.append("index.html 有内联脚本")

    dist = ASSETS / "dist"
    dist_files = list(dist.glob("*")) if dist.is_dir() else []
    for path in [ASSETS / "index.html", *sources, *dist_files]:
        if not path.is_file():
            continue
        if path.suffix not in {".html", ".css", ".js", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in _REMOTE:
            if token in text:
                problems.append(f"{path.name} 含远程地址 {token}")
    for path in list(JS_ROOT.rglob("*.js")) + [path for path in dist_files if path.suffix == ".js"]:
        _scan_inner(path, problems)

    app = ASSETS / "dist" / "app.js"
    if not app.is_file():
        problems.append("缺少 dist/app.js，先跑 python scripts/build_web_assets.py")
    else:
        built = app.read_bytes().decode("utf-8")
        fresh = bundle_js()
        if built != fresh:
            problems.append("dist/app.js 与源码不一致，重新跑 build_web_assets.py")
        for token in ("this.store", "this.app", "__spoolTest"):
            if re.search(re.escape(token) + r"\b", built):
                problems.append(f"生产包含测试钩子 {token}")
        if "test-entry" in built or "var syncToastChrome" in built:
            problems.append("生产包里混进了测试入口")
        main_js = (ASSETS / "js" / "main.js").read_text(encoding="utf-8")
        for token in ("this.store", "this.app", "__spoolTest", "var syncToastChrome"):
            if re.search(re.escape(token) + r"\b", main_js):
                problems.append(f"main.js 还留着测试钩子 {token}")
        css = (ASSETS / "dist" / "app.css").read_text(encoding="utf-8")
        if css != bundle_css():
            problems.append("dist/app.css 与源码不一致")
        test_bundle = (ASSETS / "dist" / "test-bundle.js").read_text(encoding="utf-8")
        if "__spoolTest" not in test_bundle:
            problems.append("test-bundle.js 没有测试面")
        for token in ("this.store", "this.app"):
            if re.search(re.escape(token) + r"\b", test_bundle):
                problems.append(f"test-bundle 不该再挂 {token}")
        if bundle_js(include_test=True) != test_bundle:
            problems.append("test-bundle.js 与源码不一致")

    try:
        compat_html()
    except RuntimeError as exc:
        problems.append(str(exc))

    if problems:
        for item in problems:
            print(item)
        return 1
    print("web assets ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
