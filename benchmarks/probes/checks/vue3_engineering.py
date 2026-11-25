"""判据：从零搭出来的 Vue3 前端工程，工程化结构是不是真的立起来了。

这一条判的是**结构**，不是"跑起来没有"——所以默认完全离线、秒级完成，
`tests/test_probe_runner.py` 才能在种子上判红、在人工修好的版本上判绿。

判据分四组：

1. **入口链**：`index.html` → `src/main.ts` → `App.vue` → 路由视图，逐段接得上；
2. **分层目录**：视图 / 组件 / 路由 / 状态 / 接口 / 组合式函数 / 工具 / 样式
   各归各位，而且每类都要**非空、非占位**；
3. **工程化配置**：TS 严格模式、路径别名两处一致、环境变量与类型、
   lint / format / editorconfig、单测、CI；
4. **一致性**：代码里用到的依赖必须在 `package.json` 里声明、用到 `@/`
   别名就必须两处都配、README 里写的脚本必须真的存在。

`--with-toolchain` 时再往前一步：装依赖（`pnpm install --node-linker=hoisted`）
并跑 `vue-tsc --noEmit`——**结构不是纸面的**。默认不开：CI 与判据的红绿
契约必须离线可复现，装依赖是网络动作。

**本机限制（如实标注，不假装验过）**：受限沙箱里 `vite build` 与 `vitest run`
会因为 esbuild 的服务进程 `spawn EPERM` 失败，那是环境不是工程；所以这两步
既不在默认判据里，也不在 `--with-toolchain` 里。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Checker  # noqa: E402

#: 视图/组件这类文件不能是空壳：小于这个字节数就当占位符没写内容。
MIN_SOURCE_BYTES = 80


def _parse_args():
    """本判据比别家多一个 `--with-toolchain`，所以自己解析参数。"""
    parser = argparse.ArgumentParser(description="Vue3 前端工程的工程化结构")
    parser.add_argument("--root", required=True, help="探针沙箱副本的根目录")
    parser.add_argument(
        "--with-toolchain",
        action="store_true",
        help="再装依赖并跑 vue-tsc --noEmit（要网络；默认判据保持离线可复现）",
    )
    return parser.parse_args()


def _strip_jsonc(text: str) -> str:
    """把 JSONC 注释与尾逗号去掉（tsconfig 允许注释，`json.loads` 不允许）。

    逐字符扫描而不是正则：字符串里的 `//`（比如 URL）不能被当成注释。
    """
    out = []
    index = 0
    in_string = False
    quote = ""
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if char == "\\":
                if nxt:
                    out.append(nxt)
                    index += 2
                    continue
            elif char == quote:
                in_string = False
            index += 1
            continue
        if char in "\"'":
            in_string = True
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_json(path: Path):
    """读一个 JSON/JSONC 文件；读不了就返回 None（判据按"缺失"处理）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for candidate in (raw, _strip_jsonc(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _files(root: Path, pattern: str) -> list[Path]:
    return sorted(item for item in root.glob(pattern) if item.is_file())


def _first(root: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        found = _files(root, pattern)
        if found:
            return found[0]
    return None


def _has_bom(path: Path) -> bool:
    try:
        return path.read_bytes()[:3] == b"\xef\xbb\xbf"
    except OSError:
        return False


def _script(scripts: dict, names: tuple[str, ...], needle: str) -> tuple[bool, str]:
    """脚本里有没有一类命令（名字可以叫 typecheck / type-check / tsc 都行）。"""
    for name in names:
        value = str(scripts.get(name) or "")
        if needle in value:
            return True, f"{name}: {value}"
    return False, "，".join(names)


def _imports_of(text: str) -> set[str]:
    """这段 TS/Vue 代码 import 了哪些**包名**（相对路径与 `@/` 不算）。"""
    found = set()
    for match in re.finditer(r"""from\s+['"]([^'"]+)['"]""", text):
        spec = match.group(1)
        if spec.startswith(".") or spec.startswith("/") or spec.startswith("@"):
            continue
        root = spec.split("/")[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
        found.add(root)
    return found


def _local_imports(text: str) -> list[str]:
    """这段代码 import 的**本地**模块（相对路径与 `@/` 别名）。

    三种写法都要认：`from 'x'`、副作用 `import 'x'`、动态 `import('x')`。
    """
    specs: list[str] = []
    for pattern in (
        r"""from\s+['"]([^'"]+)['"]""",
        r"""(?<![.\w])import\s+['"]([^'"]+)['"]""",
        r"""import\(\s*['"]([^'"]+)['"]\s*\)""",
    ):
        for match in re.finditer(pattern, text):
            spec = match.group(1)
            if spec.startswith("./") or spec.startswith("../") or spec.startswith("@/"):
                specs.append(spec.split("?")[0])
    return specs


def _resolves(root: Path, source: Path, spec: str) -> bool:
    """本地 import 指到的文件在不在（候选后缀与目录 index 都算）。

    这一条不是"风格"：**自己写出来的导入指向不存在的文件**，工程就是散的——
    真机跑里就抓到了 `import App from './App.vue'`（App.vue 没写）与路由里
    `import('../pages/Home.vue')`（页面写去了 `src/views/`）两处。
    """
    if spec.startswith("@/"):
        base = root / "src" / spec[2:]
    else:
        base = (source.parent / spec).resolve()
    candidates = [base]
    for suffix in (".ts", ".tsx", ".js", ".vue", ".json", ".css", ".scss", ".less", ".d.ts"):
        candidates.append(Path(str(base) + suffix))
    for index in ("index.ts", "index.tsx", "index.js", "index.vue"):
        candidates.append(base / index)
    return any(item.is_file() for item in candidates)


def check_structure(root: Path, checker: Checker) -> None:
    """四组结构判据。任何一条不成立都会进 `checker.failures`。"""
    # ---- ① 入口链 --------------------------------------------------------
    index = root / "index.html"
    html = _read(index)
    checker.require(index.is_file(), "index.html 存在")
    checker.require(re.search(r"""id=["']app["']""", html) is not None, "index.html 里有 #app 挂载点")
    checker.require("src/main.ts" in html.replace("\\", "/"), "index.html 引到 /src/main.ts")

    main = _first(root, ("src/main.ts", "src/main.js"))
    main_text = _read(main) if main else ""
    checker.require(main is not None, "src/main.ts 存在")
    checker.require("createApp(" in main_text, "main.ts 用 createApp 建应用")
    checker.require(".mount(" in main_text, "main.ts 挂载到 #app")
    checker.require(
        "createPinia(" in main_text or "use(pinia" in main_text or "pinia" in main_text.lower(),
        "main.ts 接上 Pinia",
    )
    checker.require(
        "router" in main_text or "createRouter(" in main_text, "main.ts 接上路由"
    )

    app = root / "src" / "App.vue"
    app_text = _read(app)
    checker.require(app.is_file(), "src/App.vue 存在")
    checker.require("<script setup" in app_text, "App.vue 用 <script setup>")
    checker.require('lang="ts"' in app_text or "lang='ts'" in app_text, "App.vue 是 TS")
    checker.require("<RouterView" in app_text or "<router-view" in app_text, "App.vue 里有 <RouterView>")

    # ---- ② 分层目录 ------------------------------------------------------
    #
    # 每一层都**接受常见写法的变体**（router/index.ts 或 routes/、stores 或 store、
    # composables 或 hooks、utils 或 lib/helpers）：判的是"这一层有没有、是不是
    # 空的"，不是"目录名和我心里那份一不一样"。
    # 路由：**表**与**实例**分开找，允许放在不同文件（`router/routes.ts` 放表、
    # main.ts 建实例是常见写法）。两样都要有，而且 history 模式要指明。
    router_table = None
    for item in (
        _files(root, "src/router/**/*.ts")
        + _files(root, "src/routes/**/*.ts")
        + _files(root, "src/**/*.ts")
    ):
        if len(re.findall(r"path:\s*['\"]", _read(item))) >= 2:
            router_table = item
            break
    router_instance = None
    for item in _files(root, "src/**/*.ts"):
        text = _read(item)
        if "createRouter(" in text and (
            "createWebHistory(" in text or "createWebHashHistory(" in text
        ):
            router_instance = item
            break
    checker.require(router_table is not None, "路由表存在且至少两条路由")
    checker.require(router_instance is not None, "路由实例用 createRouter + history 模式")
    if router_table is not None:
        checker.note(f"路由表：{router_table.relative_to(root).as_posix()}")
    if router_instance is not None:
        checker.note(f"路由实例：{router_instance.relative_to(root).as_posix()}")

    stores = [
        item
        for item in _files(root, "src/stores/*.ts") + _files(root, "src/store/*.ts")
        if "defineStore(" in _read(item)
    ]
    checker.require(bool(stores), "Pinia store 存在且用 defineStore 定义（src/stores/*.ts）")

    views = [
        item
        for item in (_files(root, "src/views/*.vue") + _files(root, "src/pages/*.vue"))
        if item.stat().st_size >= MIN_SOURCE_BYTES
    ]
    checker.require(len(views) >= 2, f"至少两个示例页面（src/views 或 src/pages）：{len(views)} 个")

    components = [
        item
        for item in _files(root, "src/components/*.vue")
        if item.stat().st_size >= MIN_SOURCE_BYTES
    ]
    checker.require(bool(components), f"至少一个可复用组件：{len(components)} 个")

    api_files = (
        _files(root, "src/api/*.ts")
        + _files(root, "src/services/*.ts")
        + _files(root, "src/service/*.ts")
    )
    api_ok = any(
        "export" in _read(item)
        and ("fetch(" in _read(item) or "axios" in _read(item) or "Promise<" in _read(item))
        for item in api_files
    )
    checker.require(api_ok, "请求层 src/api 或 src/services 有导出的请求封装")

    composables = _files(root, "src/composables/use*.ts") + _files(root, "src/hooks/use*.ts")
    checker.require(bool(composables), "组合式函数存在（src/composables 或 src/hooks 下的 use*.ts）")

    utils = (
        _files(root, "src/utils/*.ts")
        + _files(root, "src/lib/*.ts")
        + _files(root, "src/helpers/*.ts")
    )
    checker.require(bool(utils), "工具函数目录存在（src/utils、src/lib 或 src/helpers）")

    styles = (
        _files(root, "src/**/*.css")
        + _files(root, "src/**/*.scss")
        + _files(root, "src/**/*.less")
    )
    # 样式放在哪儿不判，判的是"有没有、以及有没有被引进来"——Vite 默认模板
    # 就是 `src/style.css` + main.ts 里 import，那也是合格的写法。
    imported_style = any(item.name in main_text or item.name in app_text for item in styles)
    checker.require(bool(styles), "全局样式文件存在（src 下的 css/scss/less）")
    checker.require(imported_style, "全局样式被 main.ts 或 App.vue 引入")

    # ---- ③ 工程化配置 ----------------------------------------------------
    package = _load_json(root / "package.json")
    checker.require(isinstance(package, dict), "package.json 存在且是合法 JSON")
    package = package if isinstance(package, dict) else {}
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    checker.require(package.get("type") == "module", 'package.json 声明 "type": "module"')
    for name, label in (
        ("vue", "Vue 3"),
        ("vue-router", "Vue Router"),
        ("pinia", "Pinia"),
        ("vite", "Vite"),
        ("@vitejs/plugin-vue", "@vitejs/plugin-vue"),
        ("typescript", "TypeScript"),
        ("vue-tsc", "vue-tsc"),
        ("vitest", "Vitest"),
    ):
        checker.require(name in deps, f"依赖声明里有 {label}（{name}）")

    scripts = package.get("scripts") or {}
    checker.require(isinstance(scripts, dict) and bool(scripts), "package.json 有 scripts")
    dev_ok, dev_where = _script(scripts, ("dev",), "vite")
    build_ok, build_where = _script(scripts, ("build",), "vite build")
    test_ok, test_where = _script(scripts, ("test",), "vitest")
    type_ok, type_where = _script(
        scripts, ("typecheck", "type-check", "check:types", "types"), "tsc"
    )
    lint_ok, lint_where = _script(scripts, ("lint",), "eslint")
    checker.require(dev_ok, f"有 dev 脚本（跑 vite）：{dev_where}")
    checker.require(build_ok, f"有 build 脚本（跑 vite build）：{build_where}")
    checker.require(test_ok, f"有 test 脚本（跑 vitest）：{test_where}")
    checker.require(type_ok, f"有类型检查脚本：{type_where}")
    checker.require(lint_ok, f"有 lint 脚本（跑 eslint）：{lint_where}")

    tsconfig_path = root / "tsconfig.json"
    tsconfig = _load_json(tsconfig_path)
    checker.require(isinstance(tsconfig, dict), "tsconfig.json 存在且是合法 JSON/JSONC")
    tsconfig = tsconfig if isinstance(tsconfig, dict) else {}
    options = tsconfig.get("compilerOptions") or {}
    checker.require(options.get("strict") is True, "tsconfig 打开 strict")
    paths = options.get("paths") or {}
    alias_ok = any(
        str(key).startswith("@") and any("src" in str(item) for item in (settings or []))
        for key, settings in paths.items()
    )
    checker.require(alias_ok, "tsconfig.paths 把 @ 映射到 src")
    include = tsconfig.get("include") or []
    checker.require(
        any("src" in str(item) for item in include), "tsconfig.include 覆盖 src"
    )

    vite_config = _first(root, ("vite.config.ts", "vite.config.js", "vite.config.mts"))
    vite_text = _read(vite_config) if vite_config else ""
    checker.require(vite_config is not None, "vite.config.ts 存在")
    checker.require("@vitejs/plugin-vue" in vite_text, "vite 配置里用了 @vitejs/plugin-vue")
    checker.require("plugins" in vite_text, "vite 配置里有 plugins")
    # 别名两种合格写法：手写 `resolve.alias`，或用 `vite-tsconfig-paths` 这类
    # 插件直接吃 tsconfig 的 paths（后者本身就把"两处一致"变成了结构保证）。
    vite_alias_ok = (
        bool(re.search(r"""['"]@['"]\s*:""", vite_text)) and "src" in vite_text
    ) or "vite-tsconfig-paths" in vite_text
    checker.require(
        vite_alias_ok,
        "vite 配置把 @ 指到 src（手写 alias 或 vite-tsconfig-paths 插件，与 tsconfig 一致）",
    )

    env_example = root / ".env.example"
    env_text = _read(env_example)
    checker.require(env_example.is_file(), ".env.example 存在")
    checker.require(re.search(r"^VITE_[A-Z0-9_]+\s*=", env_text, re.M) is not None, ".env.example 里有 VITE_ 变量")
    env_types = [
        item
        for item in _files(root, "src/**/*.d.ts")
        if "ImportMetaEnv" in _read(item) or "vite/client" in _read(item)
    ]
    checker.require(bool(env_types), "环境变量在 src/**/*.d.ts 里有类型（ImportMetaEnv / vite/client）")

    eslint_config = _first(
        root,
        (
            "eslint.config.js",
            "eslint.config.mjs",
            "eslint.config.cjs",
            "eslint.config.ts",
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.cjs",
            ".eslintrc.js",
        ),
    )
    checker.require(eslint_config is not None, "ESLint 配置存在")
    prettier_config = _first(
        root, (".prettierrc", ".prettierrc.json", ".prettierrc.json5", ".prettierrc.yml", "prettier.config.js", "prettier.config.mjs")
    )
    checker.require(prettier_config is not None, "Prettier 配置存在")
    checker.require((root / ".editorconfig").is_file(), ".editorconfig 存在")

    gitignore = _read(root / ".gitignore")
    checker.require("node_modules" in gitignore, ".gitignore 忽略 node_modules")
    checker.require("dist" in gitignore, ".gitignore 忽略 dist")

    test_files = [
        item
        for item in _files(root, "**/*.spec.ts") + _files(root, "**/*.test.ts")
        if "node_modules" not in item.parts and "vitest" in _read(item)
    ]
    checker.require(bool(test_files), f"至少一个 Vitest 单测文件：{len(test_files)} 个")

    readme = root / "README.md"
    readme_text = _read(readme)
    mentioned = [name for name in scripts if re.search(rf"\b{re.escape(name)}\b", readme_text)]
    # 只说"README 存在"是不够的：create-project 这类种子里，需求说明本身就叫
    # README.md——把它算成"项目文档"会让这一条白送。判据落在**它说清了怎么用**
    # （至少点到两个真实脚本）上。
    checker.require(
        readme.is_file() and len(mentioned) >= 2,
        f"README 说清了怎么用（存在且提到 ≥2 个真实脚本；实际 {len(mentioned)} 个）",
    )

    workflows = _files(root, ".github/workflows/*.yml") + _files(root, ".github/workflows/*.yaml")
    ci_text = "\n".join(_read(item) for item in workflows)
    checker.require(bool(workflows), "CI 工作流存在（.github/workflows/*.yml）")
    checker.require(
        ("install" in ci_text) and sum(1 for name in ("lint", "test", "build", "typecheck") if name in ci_text) >= 2,
        "CI 里装了依赖并跑了至少两件（lint / test / build / typecheck）",
    )

    # ---- ④ 一致性 --------------------------------------------------------
    #
    # 依赖声明不留口子：src 里 import 的**每一个第三方包**都要在 package.json
    # 里声明。只看 vue/vue-router/pinia 是不够的——真机产物里 `src/api/http.ts`
    # `import axios from 'axios'`，而 axios 根本没声明，`vue-tsc` 一跑就
    # `TS2307: Cannot find module 'axios'`。
    needed = set()
    source_files = (
        _files(root, "src/**/*.ts") + _files(root, "src/**/*.vue") + _files(root, "src/**/*.tsx")
    )
    source_text = {item: _read(item) for item in source_files}
    for text in source_text.values():
        for name in _imports_of(text):
            # 标准库（`node:fs` 或裸 `path`）不是"依赖没声明"，跳过。
            if name.startswith("node:") or name in sys.stdlib_module_names:
                continue
            needed.add(name)
    missing = sorted(name for name in needed if name not in deps)
    checker.require(not missing, f"src 里 import 的第三方包都在 package.json 里：缺 {missing}")

    # 自己写出来的本地导入必须指得到文件——工程"立起来"的最低标准。
    # 真机跑里这一条抓到了 main.ts 引不存在的 `./App.vue`、路由引不存在的
    # `../pages/Home.vue`/`../pages/About.vue`（页面实际写去了 src/views/）。
    dangling = []
    for item, text in source_text.items():
        for spec in _local_imports(text):
            if not _resolves(root, item, spec):
                dangling.append(f"{item.relative_to(root).as_posix()} → {spec}")
    checker.require(not dangling, f"src 里的本地导入都能指到文件：断链 {dangling}")

    uses_alias = any("'@/" in text or '"@/' in text for text in source_text.values())
    if uses_alias:
        checker.require(
            alias_ok and vite_alias_ok,
            "代码用了 @/ 别名 → vite 配置与 tsconfig.paths 两处都配了",
        )
    else:
        checker.note("没有用到 @/ 别名（这一条跳过）")

    for item in (root / "package.json", root / "tsconfig.json"):
        if item.is_file():
            checker.require(not _has_bom(item), f"{item.name} 没有 BOM（无 BOM 的 UTF-8）")

    junk = [
        item.relative_to(root).as_posix()
        for item in source_files
        if item.suffix in {".ts", ".vue"} and item.stat().st_size == 0
    ]
    checker.require(not junk, f"src 里没有 0 字节的占位文件：{junk}")


def check_toolchain(root: Path, checker: Checker) -> None:
    """`--with-toolchain`：装依赖 + 类型检查。**有网络才跑得动**。

    这一步回答的是"结构是不是纸面的"：`vue-tsc --noEmit` 会去解析 `@/` 别名、
    读 `vue` / `vue-router` / `pinia` 的类型、检查每个 SFC 的 script 块——
    配置写错、依赖没声明、类型不通，这里都会红。

    没有 pnpm/npm 时如实记为环境缺件（不算判据失败）：这一条是加分项，
    默认那一轮不依赖它。
    """
    pnpm = shutil.which("pnpm")
    npm = shutil.which("npm")
    if not pnpm and not npm:
        checker.note("环境里没有 pnpm / npm：跳过工具链验证（结构判据照旧）")
        return

    cache = root / ".probe-cache"
    install_argv = (
        [pnpm, "install", "--node-linker=hoisted", "--ignore-scripts", f"--config.cache-dir={cache}"]
        if pnpm
        else [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", f"--cache={cache}"]
    )
    try:
        done = subprocess.run(
            install_argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        checker.require(False, f"装依赖失败（{type(exc).__name__}）：{exc}")
        return
    if done.returncode != 0:
        tail = " ".join(((done.stderr or "") + (done.stdout or "")).split())[-300:]
        checker.require(False, f"装依赖失败（退出码 {done.returncode}）：{tail}")
        return
    checker.note("装依赖：通过")

    runner = pnpm if pnpm else npm
    exec_prefix = ["exec"] if pnpm else ["exec", "--"]
    typecheck = subprocess.run(
        [runner, *exec_prefix, "vue-tsc", "--noEmit"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    output = ((typecheck.stdout or "") + (typecheck.stderr or "")).strip()
    if typecheck.returncode == 0:
        checker.note("vue-tsc --noEmit：零错误")
    else:
        checker.require(False, f"vue-tsc --noEmit 有错：{' '.join(output.split())[:300]}")


def main() -> int:
    args = _parse_args()
    root = Path(args.root).resolve()
    checker = Checker("vue3-engineering/工程化结构")
    check_structure(root, checker)
    if args.with_toolchain:
        check_toolchain(root, checker)
    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
