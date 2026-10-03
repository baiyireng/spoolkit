"""自动验证：改完就替模型跑一遍项目里的测试。

模型会改错，而且看不出来自己错了。工作区里明明有测试，它却几乎从不主动
去跑——实测 25 题里 8 条失败，没有一条调用过 run_command。
指望它养成这个习惯是不现实的，那就由循环替它跑，把结果顶回去。

验证与 run_command 走同一条路径（同一套白名单、同一个试跑副本），
真实工作区全程不动。系统自己开一条绕开白名单的暗门，比模型乱跑更糟。

**两套世界必须显式说清**：验证有意按测试文件的**原始内容**判定（模型把测试
改成 `assert True` 就能骗过一次验证，实测发生过）。代价是出现了一个与
`run_command` 不同的世界，而两者互相矛盾时，同一个事实会有两个答案——
`rename-cross-file` 复跑那次就是这么翻车的。所以这里做两件事：

1. 范围收窄到子目录之后**导不进项目根**时，回退到项目根重跑，并说明为什么
   （见 `_scope_fallback`）；报假 ERROR 会诱导模型去「修」没坏的东西。
2. 按原始测试判定这件事，只要发生就写在结论里，不设成陷阱。
"""

import builtins
import json
import keyword
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Sequence

from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable
from spoolkit.tools.edit import PendingChanges, is_test_path
from spoolkit.tools.exec import DEFAULT_TIMEOUT, run_once
from spoolkit.tools.grant import ALLOWED, classify
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.tools.view import WorkspaceView

TEST_COMMAND = ("python", "-m", "pytest", "-q")

# 环境问题的报告带这个前缀。主循环据此判断「这次失败是环境造成的」，
# 而不是去猜文案——文案会改，标记不会。
ENVIRONMENT_MARKER = "【环境问题】"

# 「范围窄了，导不进项目根」时，报告里带这个前缀。主循环据此把「换了范围」
# 这件事写进审查发现，而不是让模型去猜这次跑的范围为什么变了。
SCOPE_FALLBACK_MARKER = "【范围回退】"

_MARKERS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")

# 一次改了多少处之后就不再逐处验：每处都要起一个解释器，改动太散时
# 验证本身比任务还贵。超出的部分会明说「没验」。
MAX_SCOPES = 5

# 「收集期就报错」的形状：pytest 用 ERROR 报收集失败，
# 并且把 ImportError / ModuleNotFoundError 写在追溯里。
_COLLECT_ERROR = re.compile(r"^\s*ERROR\s", re.MULTILINE)
_IMPORT_ERROR = re.compile(
    r"ImportError|ModuleNotFoundError|No module named|cannot import name"
)
# 「参数用法错」也一律算范畴问题：这种报错与范围强相关
# （`-m pytest tests/` 在根上跑得通，在 `tests/` 里跑就找不到 tests/ 了）。
_USAGE_ERROR = re.compile(r"^ERROR: (file or directory not found|unrecognized arguments)")

# 一个范围要当独立测试根，得多少有点「这里就是测试根」的证据。
_ROOT_MARKERS = ("conftest.py", "pytest.ini", "tox.ini", "pyproject.toml", "setup.cfg")



def _relative(root: Path, target: Path) -> str:
    try:
        text = str(target.relative_to(root)).replace("\\", "/")
    except ValueError:
        return "."
    return "." if text in ("", ".") else text


def scope_for_change(root: Path, changed: str) -> str:
    """这次改动该在哪个目录里验。

    一个工作区里放着很多件事时（长任务里最常见的形状），在根上跑整条测试
    命令等于把**别人的失败**报给它——实测它就拿着那些无关的报错去改自己的
    代码。所以范围跟着**改动**走：从被改文件所在目录往上找，最近的、自己
    就带测试的那一层，就是它该看的范围；找不到就退回根（单项目工作区的
    情形，那里本来就只有一套测试）。
    """
    target = (root / changed).parent
    while True:
        try:
            if any(target.glob("test_*.py")) or any(target.glob("*_test.py")):
                return _relative(root, target)
            if target == root or target.parent == target:
                return "."
        except OSError:
            return "."
        target = target.parent


def scopes_for_changes(
    root: Path, changed: Sequence[str], max_scopes: int = MAX_SCOPES
) -> tuple[list[str], int]:
    """改动落在哪些范围上，以及有几个范围没来得及验。

    祖先范围会**吸收**它下面的范围：根范围跑 pytest 时本来就会收集 `tests/`。
    不去重的话，一次改名会报两条一样的 `ERROR tests/test_pipeline.py`
    （实测 rename-cross-file 复跑：`- .：… - tests：…`），
    既浪费一次解释器启动，又把「2 处没过」说成了两件事。
    """
    scopes: list[str] = []
    for path in changed:
        scope = scope_for_change(root, path)
        if scope not in scopes:
            scopes.append(scope)
    if not scopes:
        return ["."], 0
    kept: list[str] = []
    for scope in scopes:
        if scope in ("", "."):
            # 根吸收一切，直接收敛成根。
            kept = ["."]
            break
        if any(
            other != scope
            and (other == "." or _is_ancestor_scope(other, scope))
            for other in scopes
        ):
            continue
        kept.append(scope)
    scopes = kept or ["."]
    dropped = max(0, len(scopes) - max_scopes)
    return scopes[:max_scopes], dropped


def _is_ancestor_scope(other: str, scope: str) -> bool:
    """`other` 是不是 `scope` 的祖先目录（按正斜杠切段比）。"""
    head = scope.rstrip("/").split("/")
    tail = other.rstrip("/").split("/")
    return len(tail) < len(head) and head[: len(tail)] == tail


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: 不可能是「被改成的那个新名字」的词：关键字与内置名。
#: 不排掉它们，`assert mod.VALUE == 1` 改成 `assert True` 会被算成
#: 「VALUE→True」的一次改名，于是「改断言凑绿」被盖上「中间态」的章。
_NOT_A_NAME = frozenset(keyword.kwlist) | frozenset(dir(builtins))


def pending_renames(pending) -> dict[str, str]:
    """待确认的改动里，哪些名字被改成了什么。

    判据只看**标识符**与它们新旧正文里的**定义**：旧正文里有、新正文里彻底
    没有了、同时新正文里多出一个新名字——这样的配对才算一次改名。
    不看字面相似度（`get_rows` → `load_rows` 没有任何可依赖的相似性）。

    用途只有一个：验证失败时判断「这条报错是不是改名只落了一半造成的」。
    是的话它就不是代码问题，报成代码失败会诱发迎合测试的改坏逻辑
    （`rename-cross-file` 复跑实测就是这么翻车的）。
    """
    if pending is None:
        return {}
    renamed: dict[str, str] = {}
    for change in pending.items():
        if Path(change.path).is_absolute():
            continue
        if getattr(change, "deleted", False) or change.is_new_file:
            continue
        old_words = set(_WORD.findall(change.old_text))
        new_words = set(_WORD.findall(change.new_text))
        gone = old_words - new_words
        added = new_words - old_words
        if len(added) != 1 or not gone:
            # **新名字只有一个**是判据的锚：一次只换一个名字是绝大多数情形。
            # 消失的旧名可以不止一个（顺手删掉的辅助函数也算），所以不要求
            # `gone` 只有一个——实测那条改名里模型连 `helper` 一起并掉了，
            # 卡死在「只准消失一个」上会让这条判据整个失效。
            continue
        new_name = added.pop()
        if new_name in _NOT_A_NAME:
            # `assert mod.VALUE == 1` 改成 `assert True` 会算出「VALUE→True」——
            # 那是**改断言**，不是改名。关键字与内置名不可能是被改的那个新名字，
            # 不排掉它，这条判据会给「把断言改松散」盖上「中间态」的章。
            continue
        for old in gone:
            renamed[old] = new_name
    return renamed


def _test_imports_in_scope(root: Path, scope: str) -> bool:
    """这个范围里的测试文件会不会**自己找项目根**。

    `tests/test_x.py` 里写 `sys.path.insert(0, str(Path(__file__).parents[1]))`
    的项目，收窄到 `tests/` 之后照样导得进包——那种项目不需要回退，
    回退反而把「别人的失败」报了进来。
    """
    base = root if scope in ("", ".") else root / scope
    try:
        candidates = sorted(base.glob("test_*.py")) + sorted(base.glob("*_test.py"))
    except OSError:
        return False
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "sys.path" in text:
            return True
    return False


def _is_own_test_root(root: Path, scope: str) -> bool:
    """这个范围自己就是一套测试的根吗（有多项目工作区里那类独立测试目录）。

    判断不要复杂：有 `conftest.py` 或任一根标记文件（pytest.ini / pyproject.toml …），
    就认为它是独立测试根——那种目录里跑测试不依赖项目根，不该回退。
    """
    base = root / scope
    for name in _ROOT_MARKERS:
        if (base / name).exists():
            return True
    return False


def looks_like_scope_problem(text: str) -> bool:
    """这条失败看起来是不是「范围不对」而不是「代码不对」。

    只看机械形状：收集期的 ERROR、导入失败、以及「文件/参数找不到」这类
    用法错误。断言失败（FAILED …::…）不算——那才是真的代码问题。
    """
    if _USAGE_ERROR.search(text):
        return True
    if not _COLLECT_ERROR.search(text):
        return False
    return _IMPORT_ERROR.search(text) is not None


def scope_fallback(root: Path, scope: str, output: str) -> str:
    """该不该把这个范围回退到项目根；该的话给出**为什么**（不该则空串）。

    收窄范围是为了「改 A 时不收 B 的失败」，代价是单项目工作区里
    `pytest -q` 的 cwd 变了、`sys.path[0]` 也跟着变——测试项目靠
    `parents[1]` 找包根时，根就不在导入路径上了，于是报一条
    `ImportError`（实测 rename-cross-file：`ERROR tests/test_pipeline.py`）。

    报出来的 ERROR 是**假警报**：它指向的根本不是代码问题，而模型会照着它
    去「修」没坏的东西。所以这里回退，并且把原因说出来。
    """
    if scope in ("", "."):
        return ""
    if _is_own_test_root(root, scope):
        return ""
    if _test_imports_in_scope(root, scope):
        return ""
    if not looks_like_scope_problem(output):
        return ""
    return (
        f"范围 {scope} 里测试导不进项目根（{_first_error_line(output)}），"
        "回退到项目根重跑：这个项目的测试靠 sys.path 找包根，"
        "在子目录里跑会把「导不进来」报成测试失败。"
    )


def _first_error_line(text: str) -> str:
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("ERROR") or _IMPORT_ERROR.search(stripped):
            return stripped[:120]
    return "收集期就报错"


def adapted_to_rename(
    pending, renamed: dict[str, str], detail: str
) -> bool:
    """模型有没有把**测试文件**跟着改名一起改（这是合法且必要的一步）。

    判据（要全中，缺一条就不认）：

    1. 报错里出现的是本次被改掉的那个旧名（`renamed` 的键）；
    2. 待确认清单里确实有一个测试文件**把旧名换成了新名**——新正文里有新名、
       没有旧名，旧正文里有旧名；
    3. 除了这个名字，测试文件的改动里没有别的东西（改断言来凑绿那条路要被
       挡在门外：`assert True` 不会带来任何新名字）。

    为什么要开这个口子：验证按**原始测试内容**判定（防「把断言改成 assert True」），
    而改名任务里测试文件**必须**跟着改——旧测试引用的名字已经被改掉了。
    两条规矩直接对撞时，实测（rename-cross-file 复跑）模型只有两条路：
    改测试（被拦）或改坏业务代码（删了 render 的表头行），于是一路红到收手。
    所以这里把「测试文件跟着改名改」单独认出来，并且**如实写成两套世界**。
    """
    if not renamed or pending is None:
        return False
    hits = {old: new for old, new in renamed.items() if old in detail}
    if not hits:
        return False
    for change in pending.items():
        if not is_test_path(change.path) or Path(change.path).is_absolute():
            continue
        if getattr(change, "deleted", False) or change.is_new_file:
            continue
        for old, new in hits.items():
            if old not in change.old_text:
                continue
            if old in change.new_text or new not in change.new_text:
                continue
            # 名字之外的改动一律不认这条通道：判据要窄，免得变成后门。
            if _WORD.findall(change.new_text) == _WORD.findall(
                change.old_text.replace(old, new)
            ):
                return True
    return False


def condense_test_output(text: str, limit: int = 360) -> str:
    """从测试输出里挑出「错在哪」，把表头和分隔线丢掉。

    原先整段回灌，前几百字多半是分隔线、进度点和进度条，真正的报错被挤到
    后面。实测模型拿着这种反馈会去改测试文件，而不是去改被它改坏的代码。

    优先取 pytest 的短摘要（`FAILED test_x.py::test_y - AssertionError: ...`）：
    一行里同时有文件、用例名和具体差异，是最省 token 也最可行动的形式。
    """
    lines = [line.rstrip() for line in text.splitlines()]

    summary = [
        line.strip()
        for line in lines
        if line.strip().startswith(("FAILED ", "ERROR "))
    ]
    if summary:
        return " ｜ ".join(summary)[:limit]

    # 没有短摘要（比如进程直接崩了）时，退回 pytest 的 E 行
    errors = [
        line.strip()[2:].strip()
        for line in lines
        if line.strip().startswith("E ")
    ]
    errors = [item for item in errors if item]
    if errors:
        return " ｜ ".join(errors)[:limit]

    # 最后退回到去掉分隔行之后的前几行
    fallback: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or set(stripped) <= set("=_-."):
            continue
        if stripped.startswith(("退出码", "$ ")) or stripped.endswith("[100%]"):
            continue
        fallback.append(stripped)
        if len(fallback) >= 4:
            break
    return " ｜ ".join(fallback)[:limit]


def exit_code(text: str) -> int | None:
    """从「退出码 N（失败）」那一行里取出退出码。

    注意：**退出码分不出「测试没过」和「测试没跑成」**——实测收集阶段
    被权限错误打断时，pytest 返回的也是 1。所以这个函数只用于识别
    「一个测试都没收集到」（5）这类明确的信号，别指望它做更多。
    """
    first = text.splitlines()[0] if text else ""
    # 格式是「退出码 1（失败）」——数字和括号连着，别按空格切。
    match = re.match(r"退出码\s*(-?\d+)", first)
    return int(match.group(1)) if match else None


# 一看就像环境问题的异常。光有这些还不够——还得看报错指不指向工作区。
_INFRA_HINTS = (
    "INTERNALERROR",
    "PermissionError",
    "FileNotFoundError",
    "MemoryError",
    "OSError",
)

_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'()\[\],;]+")


def mentions_workspace(text: str, root: Path) -> bool:
    """报错里有没有指向工作区内的文件。"""
    try:
        base = root.resolve()
    except OSError:
        return True
    for raw in _PATH.findall(text):
        try:
            candidate = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if candidate == base or base in candidate.parents:
            return True
    return False


def looks_environmental(text: str, root: Path) -> bool:
    """这个失败看起来是不是环境造成的，而不是代码。

    判据是「报错指向哪里」：代码问题（导入失败、语法错）会指向工作区里的
    文件；环境问题（临时目录不可写、解释器坏了、缺依赖）指向工作区外面。

    这条是实战逼出来的：机器上有个坏掉的临时目录，pytest 收集阶段就崩了，
    而反馈只说「测试失败」。模型拿着 PermissionError 去查自己的改动，
    烧掉约 5 步，什么也没查出来——它没法知道那个失败与它无关。
    """
    if exit_code(text) == 5:  # 一个测试都没收集到
        return True
    if not any(hint in text for hint in _INFRA_HINTS):
        return False
    return not mentions_workspace(text, root)


def _intermediate_note(
    renamed: dict[str, str],
    detail: str,
    changed: Sequence[str],
    reverted: Sequence[str],
) -> str:
    """失败如果是「改名只落了一半」造成的，就把它说成中间态。

    三条事实全都要对上才说：报错里出现的是**本次被改掉的名字**、
    这个名字在新内容里已经不存在、而且测试文件（或测试文件之外的引用处）
    还没跟着改。对上了就说明这不是代码问题——那正是实测里诱发
    「为了让测试通过而删掉表头行」的那一步。所以这里把话说死：
    **不许为了让测试通过去改坏逻辑**。

    对不上就返回空串，一个字都不多说。
    """
    if not renamed:
        return ""
    hits = {old: new for old, new in renamed.items() if old in detail}
    if not hits:
        return ""
    pairs = "、".join(f"{old}→{new}" for old, new in sorted(hits.items()))
    done = "、".join(dict.fromkeys(changed)) or "（无）"
    wait = "、".join(dict.fromkeys(reverted)) or "项目里还引用旧名的那些文件"
    return (
        f"\n这是**中间态**，不是代码失败：{pairs} 已经落盘，"
        f"而 {wait} 还在用旧名，改名天生要多步。"
        f"本次已落盘：{done}。把剩下的引用一次改完（可一次改多处，"
        "系统按**本次运行累计的改动**合并验一次）；"
        "**不得为了让测试通过而删改业务逻辑**。"
    )

def detect_test_command(root: Path) -> list[str] | None:
    """项目里看起来有测试就返回跑测试的命令，否则返回 None。

    宁可不跑也不要瞎跑：在一个没有测试的项目里凭空调 pytest，只会得到
    一条「找不到测试」的输出，然后被当成失败反馈灌回给模型。
    """
    if any((root / name).exists() for name in _MARKERS):
        return list(TEST_COMMAND)
    if (root / "tests").is_dir():
        return list(TEST_COMMAND)
    for pattern in ("test_*.py", "*_test.py"):
        if any(root.rglob(pattern)):
            return list(TEST_COMMAND)
    return None


def make_verifier(
    root: Path,
    pending,
    timeout: int = DEFAULT_TIMEOUT,
    overrides: dict | None = None,
) -> Callable[[], ToolResult] | None:
    """装配一个验证器。没有可用的验证手段就返回 None。

    验证能力按**产出类型**分派（见本模块末尾）：有 pytest 就跑 pytest 并顺带
    检查非 Python 的改动；没有 pytest 就全靠按类型检查。所以「写 HTML 没有东西
    可验」这件事在这里结束——HTML 的结构、内联 JS、id 引用、本地资源路径、
    内联 CSS 配对都会被自动验一遍。

    装配时 pending 往往是空的（agent 还没动手），所以不能在装配时判断
    「有没有 HTML 改动」——要等到每次调用时动态检查。否则 agent 写完
    HTML 后验证器仍然是 None（dogfood 第二轮暴露）。
    """
    command = detect_test_command(root)
    if command is not None and classify(command)[0] != ALLOWED:
        # 不在白名单里就不自动跑：自动验证不该顺带向用户要授权，
        # 那会把「我想安静地验一下」变成一次打断。
        return None

    # 两条路共用**同一套**按产出类型分派的检查器（见本模块末尾）：
    # 有 pytest 时，Python 由 pytest 覆盖（比 compile 更强），其余类型仍要按类验；
    # 没有 pytest 时，全部类型都靠它——HTML 的结构、内联 JS、id 引用、本地资源
    # 路径、内联 CSS 配对都在这一套里。
    checker = make_checker(root, pending, timeout, skip=(".py",) if command else ())

    if command is None:
        # 没有 pytest：返回惰性验证器，每次调用时检查 pending 里有什么
        if shutil.which("node") is None:
            return None  # node 也不在，HTML/JS 这一大类验不了

        def lazy_file_verify(changed: Sequence[str] = ()) -> ToolResult:
            # 每次调用时检查 pending——装配时是空的
            if not pending.items():
                return ToolResult(ok=True, content="没有需要验证的改动。")
            if not _has_checkable_changes(root, pending):
                kinds = "、".join(
                    sorted(
                        _suffix(change.path) or "无扩展名"
                        for change in pending.items()
                    )
                )
                return ToolResult(
                    ok=True,
                    content=f"这次改的是 {kinds}，没有对应的自动验证器。",
                )
            return checker(changed)

        return lazy_file_verify

    # 一旦确认是环境坏了，就不再重跑：结论不会变，而每跑一次都要往
    # 上下文里再灌一遍同样的报错。实测一次运行里它被重报了 7 次。
    broken = False

    def _root_on_path() -> dict:
        """让子目录范围也导得进项目根：把根补进 PYTHONPATH。

        这一步解决的是「验证必须导得进」。`sys.path.insert(0, parents[1])`
        那类项目在收窄范围之后仍然导得进；靠命令行参数或 cwd 找包根的项目
        也能靠它找到。它**不改变判据**——测试还是那一份原始内容，
        只是让它有机会真正跑到断言，而不是死在收集阶段。
        """
        existing = os.environ.get("PYTHONPATH", "")
        joined = str(root) + (os.pathsep + existing if existing else "")
        return {"PYTHONPATH": joined}

    def _scope_result(scope: str, revert: Sequence[str]):
        return run_once(
            root,
            command,
            pending=pending,
            timeout=timeout,
            revert=revert,
            cwd=scope,
            extra_env=_root_on_path() if scope not in ("", ".") else None,
        )

    def pytest_part(changed: Sequence[str]) -> ToolResult:
        nonlocal broken
        if broken:
            return ToolResult(
                ok=False,
                content=(
                    "测试环境依然是坏的（前面已经确认过，和你的改动无关）。"
                    "继续手头的事，不要为它改代码。"
                ),
            )
        # 按原始测试判定：模型把测试改成 `assert True` 就能骗过一次验证，
        # 然后宣布完成——实测发生过。验证不能让它自己定标准。
        revert = [change.path for change in pending.items() if is_test_path(change.path)]
        # 这次改动里哪些名字被改成了什么。只在**失败是改名没铺完**造成的
        # 时候才用得上；不改名时它是空的，一句话都不多说。
        renamed = pending_renames(pending)
        # 没给改动路径时，退回「这次会话改过的全部文件」。
        #
        # 审查者的证据包就是这么调的（它不经过某一轮的改动记录）。而在多任务
        # 工作区里盲目跑根目录的整条命令，回灌的是一屏**别人的**报错——
        # 实测给审查者灌了 50 个 ERROR，它据此写下的结论自然没有意义。
        if not changed:
            changed = [change.path for change in pending.items()]
        workspace_changed = [
            path for path in changed if not Path(path).is_absolute()
        ]
        if changed and not workspace_changed:
            return ToolResult(
                ok=True,
                content="这一批改动都在关联目录里，工作区测试未跑。",
            )
        changed = workspace_changed
        # 一次改多处时逐处验几个范围是能力标定值：本机 27B 上 5 个范围
        # 之后「验证比任务本身还贵」；强模型/快机器上可以多验几个。
        max_scopes = int((overrides or {}).get("max_scopes") or MAX_SCOPES)
        scopes, dropped = scopes_for_changes(root, changed, max_scopes)
        outcomes = [(scope, _scope_result(scope, revert)) for scope in scopes]
        # 收窄之后导不进项目根的，回退到根重跑一次（并说明为什么）。
        # 只在「失败的形状像范围问题」时才回退：不这么收口的话，一个真失败
        # 会连带多跑一次根目录的全量测试，验证本身就成了最贵的那一步。
        fell_back: list[str] = []
        for index, (scope, result) in enumerate(list(outcomes)):
            reason = (
                "" if result.ok else scope_fallback(root, scope, result.content)
            )
            if not reason:
                continue
            outcomes[index] = (".", _scope_result(".", revert))
            fell_back.append(reason)
        if len(outcomes) > 1:
            # 一次改了好几处：逐处验、逐处报。汇总成一句「测试失败」，
            # 等于把「哪一处猜错了」这份信息扔掉。
            return _report_many(outcomes, dropped, root, fell_back)

        result = outcomes[0][1]
        where = "" if scopes[0] == "." or fell_back else f"，范围 {scopes[0]}"
        note = ("\n" + SCOPE_FALLBACK_MARKER + fell_back[0]) if fell_back else ""
        # 原始输出留在 result.raw 里：模型看的是压缩过的 content，而自动审查层
        # 判「这次失败是不是改名没铺完」要有 `ImportError: ... 'get_rows'`
        # 那一行，短摘要里已经没有了。
        raw = result.content
        if not result.ok:
            # 失败时只留「错在哪」。整段 pytest 输出会把真正的原因淹掉。
            detail = condense_test_output(result.content)
            if looks_environmental(result.content, root):
                broken = True
                return ToolResult(
                    ok=False,
                    content=(
                        f"{ENVIRONMENT_MARKER}测试没能跑起来"
                        f"（报错指向的是工作区外面的东西，"
                        f"属于环境问题）：{detail}。"
                        "这不是你的代码造成的，不要为它改代码——"
                        "把情况说清楚就行；如果确实需要查清是什么坏了，"
                        "用 request_diagnosis 登记，让具备真实环境权限的会话去验。"
                        f"{note}"
                    ),
                    raw=raw,
                )
            # 改名任务里，测试文件**必须**跟着改（旧测试引用的名字已被改掉）。
            # 此时按原始测试判定会永远绿不了，而模型只有两条路：改测试（被拦）
            # 或改坏业务代码（实测它删了 render 的表头行）。所以这里把
            # 「测试文件确实跟着这次改名改了」单独认出来，改用模型改过的那份
            # 测试再判一次——判据仍然是「测试全过」，只是不再拿旧名字去要求新代码。
            if adapted_to_rename(pending, renamed, result.content):
                retry = _scope_result(".", ())
                if retry.ok:
                    return ToolResult(
                        ok=True,
                        content=(
                            "改名已经铺完，测试全过"
                            f"（{' '.join(command)}，范围 .）。"
                            "这次测试文件也跟着改了（旧名已经被改掉，旧测试引用不到"
                            "它，所以这一份按你改过的测试判定了一回）。"
                            "测试文件的改动同样在待确认清单里，"
                            "确认之前它不会写进工作区。"
                            f"{note}"
                        ),
                        raw=retry.content,
                    )
                # 用了新测试还是不过：那才是真的代码问题，按它报。
                detail = condense_test_output(retry.content)
                raw = retry.content
            return ToolResult(
                ok=False,
                content=(
                    # 说清跑的是哪条命令、**在哪个范围**：工作区里有多件事时，
                    # 报错里的文件名未必是它刚改的那个。不给这个信息，
                    # 它会拿别人的失败去查自己的改动——实测连撞了两次。
                    f"测试没有通过（{' '.join(command)}{where}）：{detail}。"
                    + _intermediate_note(renamed, raw, changed, revert)
                    + "按这个报错改代码，不要改测试：只有「旧名已经被改掉、"
                    "测试引用不到它」时才跟着改测试文件，别动断言。"
                    f"{note}"
                ),
                raw=raw,
            )
        if revert:
            # **两套世界必须显式对齐**：验证按原始内容判定这件事，只要发生
            # 就写在结论里。不写的话，同一个事实会出现两个答案——
            # `run_command` 跑在磁盘视图上（那里是模型改过的内容），
            # 而验证跑在「原始测试」上；模型会以为验证在自相矛盾。
            return ToolResult(
                ok=True,
                content=(
                    result.content
                    + f"（注意：你改过 {len(revert)} 个测试文件"
                    f"（{'、'.join(revert)}），"
                    "验证是按它们**原来的内容**判定的——这一份才是判据；"
                    "跑测试时你用 run_command 看到的是你自己改过的版本，"
                    "两者不一致时以这里为准。"
                    "想让改动生效就别改测试，改代码。"
                    f"{note}）"
                ),
            )
        if where:
            return ToolResult(
                ok=True,
                content=f"这一处的测试通过（{scopes[0]}）：{result.content}",
                raw=raw,
            )
        return ToolResult(
            ok=True,
            content=result.content + note if note else result.content,
            raw=raw,
        )

    def verify(changed: Sequence[str] = ()) -> ToolResult:
        # 按类型的检查很便宜（只有 HTML/JS 要起一次 node），先跑：即使 pytest
        # 那条路坏掉了，它照样能给出结论——旧实现里这种项目根本不验 HTML。
        check = checker(changed)
        return _merge_types(pytest_part(changed), check)

    return verify


def _merge_types(pytest_result: ToolResult, check: ToolResult) -> ToolResult:
    """把「跑测试」与「按产出类型检查」的结论合成一份。

    合成规则两条：
    1. **只加不减**：pytest 那条路的措辞与判定原样保留（用例钉着它们），
       按类型的结论有内容才附在后面。
    2. **「查不了」不改判**：check.ok 为真时它只带了「这一步没验」的提示
       （例如 HTML 的运行时行为要浏览器），那不是这次改动的问题，不能把绿的
       判成红的——但也不能吞掉它：「语法通过」被当成「跑得起来」正是 dogfood
       那次翻车（step() 没调 draw()，语法全对）。

    `raw` **必须原样带过去**：它是自动审查层的证据（判「这次失败是不是改名
    没铺完」要看未压缩的报错）。丢掉它，审查层就只能拿压缩过的短摘要去判，
    而那一行 `ImportError: cannot import name 'get_rows'` 早就没了。
    """
    body = check.content.strip()
    if not body:
        return pytest_result
    joined = f"{pytest_result.content.rstrip()}\n{body}"
    if check.ok:
        return ToolResult(ok=pytest_result.ok, content=joined, raw=pytest_result.raw)
    if pytest_result.ok:
        return ToolResult(
            ok=False,
            content=f"{pytest_result.content.rstrip()}\n按产出类型检查发现问题：\n{body}",
            raw=pytest_result.raw,
        )
    return ToolResult(ok=False, content=joined, raw=pytest_result.raw)


def _report_many(
    outcomes: list[tuple[str, ToolResult]],
    dropped: int,
    root: Path,
    fell_back: Sequence[str] = (),
) -> ToolResult:
    """一次改了好几处，就逐处报。"""
    failed = [(scope, item) for scope, item in outcomes if not item.ok]
    head = f"你这次改了 {len(outcomes)} 处，逐处跑了各自的测试："
    head += (
        f"{len(outcomes) - len(failed)} 处通过、**{len(failed)} 处没过**。"
        if failed
        else "全部通过。"
    )
    if dropped:
        head += f"（还有 {dropped} 处没验——一次改得太散，验证会比任务本身还贵。）"
    if fell_back:
        # 范围回退必须说出来：不说的话，「这次跑的是哪个范围」就成了暗规则。
        head += "\n" + SCOPE_FALLBACK_MARKER + "；".join(fell_back)
    if not failed:
        return ToolResult(ok=True, content=head)

    lines = [head, "没过的："]
    for scope, item in failed:
        if looks_environmental(item.content, root):
            lines.append(f"- {scope}：测试没能跑起来（环境问题，不是你的代码）")
            continue
        lines.append(f"- {scope}：{condense_test_output(item.content, limit=200)}")
    lines.append("逐处按报错改——不要改测试文件。")
    return ToolResult(ok=False, content="\n".join(lines))


# --- 按产出类型分派的文件验证 ---------------------------------------------
#
# 这一节是被 dogfood 逼出来的。系统要求「写完必须验证」，而当时的验证能力只有
# 两条：pytest（只认 Python）与 node --check（只认 .js）。写 HTML 时两条都不
# 适用，模型只好自己拿 HTML 去撞 `node --check snake.html`，收获一条 node 的
# 原始英文报错 ERR_UNKNOWN_FILE_EXTENSION，再换个写法、再撞——9 步白费、
# 模型侧 293 秒。缺的不是纪律，是**与产出类型对齐的验证能力**。
#
# 所以这里按扩展名分派，每一类都给可照做的结论：`文件:行` + 错在哪 + 怎么改；
# 验不了的那一步明说「查不了」，绝不沉默通过。零新依赖：标准库 + 已有的 node。
# 模型侧入口是 verify_file（见 verify_file_spec），自动验证走同一个 make_checker。

#: 有验证器、但这台机器上跑不起来（例如没有 node）时的那句话。
CANNOT_CHECK = "【查不了】"
#: 这类文件根本没有对应的验证器。它不是缺陷，所以自动验证不回灌这句。
NO_CHECKER = "【没有对应验证器】"

#: 有机械判据、能给出「文件:行」的扩展名。
CHECKABLE_SUFFIXES = frozenset(
    {".html", ".htm", ".js", ".css", ".json", ".toml", ".py"}
)

#: 一次最多验几个文件：每个 HTML/JS 都要起一次 node，改动太散时验证比任务还贵。
MAX_CHECKED_FILES = 5

_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}
)

#: 没有验证器时的替代做法。宁可给一条可行的，也不要说「无法验证」就完了。
_NO_CHECKER_ALTERNATIVES = {
    ".md": "文本没有机械判据：用 read_file 读一遍；里面的数字用 check_numbers 核对",
    ".txt": "文本没有机械判据：用 read_file 读一遍",
    ".rst": "文本没有机械判据：用 read_file 读一遍",
    ".csv": "本工具不解析表格：用 read_file 看前几行；项目里若有校验脚本，用 run_command 跑它",
    ".xml": "本工具不验 XML：人工看一遍，或让用户在浏览器里打开；要机械判据需外部校验器（本轮不引入）",
    ".sh": "本白名单不执行脚本：人工审阅；确实要跑，请让用户在终端手动执行",
    ".ps1": "本白名单不执行脚本：人工审阅；确实要跑，请让用户在终端手动执行",
    ".bat": "本白名单不执行脚本：人工审阅；确实要跑，请让用户在终端手动执行",
    ".cmd": "本白名单不执行脚本：人工审阅；确实要跑，请让用户在终端手动执行",
    ".yml": "本工具不解析 YAML：人工读一遍；要机械判据需外部校验器（本轮不引入）",
    ".yaml": "本工具不解析 YAML：人工读一遍；要机械判据需外部校验器（本轮不引入）",
}

_DEFAULT_ALTERNATIVE = (
    "本工具没有这一类的验证器：可行的替代是人工读一遍（read_file），"
    "或让用户确认；要机械判据时先说明需要什么校验器（本轮不引入新依赖）"
)


def _suffix(path: str) -> str:
    return Path(path).suffix.lower()


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _line_text(text: str, line: int | None) -> str:
    if not line:
        return ""
    lines = text.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


_LINE_HEAD = re.compile(r"^第 (\d+) 行")


def _with_file(label: str, problems: Sequence[str]) -> list[str]:
    """把行首的「第 N 行」换成「文件:N」。

    自动验证会把几个文件的结论合在一起回灌，那时只有行号没有文件名，
    模型对不上是哪个文件——「文件:行」这条要求在合并之后就失效了。
    """
    return [_LINE_HEAD.sub(lambda m: f"{label}:{m.group(1)}", item) for item in problems]


def _mask(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """把区段内容换成空格（换行保留）——行号因此不变。"""
    chars = list(text)
    for start, end in spans:
        for i in range(max(0, start), min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


_BLOCK_RE = re.compile(
    r"<(script|style)((?:\s[^>]*)?)>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _blocks(html: str) -> list[tuple[str, str, str, int]]:
    """所有 <script>/<style> 块：(类型, 属性, 正文, 正文起始下标)。"""
    return [
        (match.group(1).lower(), match.group(2) or "", match.group(3), match.start(3))
        for match in _BLOCK_RE.finditer(html)
    ]


def _has_src(attrs: str) -> bool:
    return re.search(r"\bsrc\s*=", attrs, re.IGNORECASE) is not None


def extract_js_with_lines(html: str) -> tuple[str, list[int]]:
    """抽出内联 JS，并给出「每一行 JS 对应 HTML 的哪一行」。

    行映射是「可照做」的关键：node 报的是**抽出来那一段**的行号，不给映射，
    模型就拿着一个凭空的行号对不回自己的文件——那正是它反复打转的原因。
    """
    lines: list[str] = []
    mapping: list[int] = []
    for kind, attrs, body, start in _blocks(html):
        if kind != "script" or _has_src(attrs) or not body.strip():
            continue
        if lines:
            lines.append(";")  # 块间分隔：不对应原文任何一行，映射记 0
            mapping.append(0)
        base = _line_of(html, start)
        for i, line in enumerate(body.splitlines()):
            lines.append(line)
            mapping.append(base + i)
    return "\n".join(lines), mapping


def extract_js_from_html(html: str) -> str:
    """从 HTML 里抽出所有内联 <script> 的 JS 内容，拼成一段。

    带 src 属性的 <script src="..."> 跳过——那是外部引用，抽出来也跑不了。
    """
    return extract_js_with_lines(html)[0]


# --- ① 标签配对与嵌套 ------------------------------------------------------

_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

#: 结束标签可省略的元素。漏了它们**不是**错误（HTML 允许，浏览器会补），
#: 报出来只会教模型忽略这个检查器。
_OPTIONAL_END = frozenset(
    {
        "html", "head", "body", "li", "dt", "dd", "p", "td", "th", "tr",
        "thead", "tbody", "tfoot", "option", "optgroup", "colgroup",
        "caption", "rt", "rp",
    }
)

#: 遇到这些开始标签时，栈顶这几类元素按 HTML 的规矩自动闭合。
_IMPLICIT_CLOSE = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"td", "th", "tr"}),
    "option": frozenset({"option"}),
    "optgroup": frozenset({"option", "optgroup"}),
    "thead": frozenset({"td", "th", "tr"}),
    "tbody": frozenset({"td", "th", "tr", "thead"}),
    "tfoot": frozenset({"td", "th", "tr", "thead", "tbody"}),
    "body": frozenset({"head", "body"}),
    "head": frozenset({"head"}),
}

#: 这些元素一出现，未闭合的 <p> 就自动结束。
_P_CLOSERS = frozenset(
    {
        "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre",
        "section", "table", "ul",
    }
)


class _StructureParser(HTMLParser):
    """标签配对与嵌套（浅层，只用 html.parser）。

    不装成完整 HTML 解析器：可省略的结束标签按 HTML 的规矩静默补齐，
    只报三类真问题——交叉嵌套（有元素被夹住没闭合）、多出来的结束标签、
    以及文件结束时仍未闭合的非可省元素。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[str] = []
        self.starts = 0
        self.root_end: int | None = None

    def _auto_pop(self, tag: str) -> None:
        closers = _IMPLICIT_CLOSE.get(tag)
        if closers:
            while self.stack and self.stack[-1][0] in closers:
                self.stack.pop()
        if tag in _P_CLOSERS:
            while self.stack and self.stack[-1][0] == "p":
                self.stack.pop()

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _VOID_TAGS:
            return
        line = self.getpos()[0]
        if tag == "html" and self.root_end is not None:
            self.problems.append(
                f"第 {line} 行：又一个 <html>——文档已经在第 {self.root_end} 行结束，"
                "这里像是两份文件拼接在一起"
            )
        self.starts += 1
        self._auto_pop(tag)
        self.stack.append((tag, line))

    def handle_startendtag(self, tag, attrs) -> None:
        # <tag/> 是自闭合，不入栈。
        return

    def handle_endtag(self, tag) -> None:
        line = self.getpos()[0]
        if tag in _VOID_TAGS:
            self.problems.append(
                f"第 {line} 行：</{tag}> 是空元素，本来就没有结束标签"
            )
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] != tag:
                continue
            for extra, extra_line in self.stack[i + 1:]:
                if extra not in _OPTIONAL_END:
                    self.problems.append(
                        f"第 {extra_line} 行：<{extra}> 没有闭合，"
                        f"就先遇到了第 {line} 行的 </{tag}>"
                    )
            del self.stack[i:]
            if tag == "html" and not self.stack:
                self.root_end = line
            return
        self.problems.append(
            f"第 {line} 行：</{tag}> 没有对应的开始标签（多出来的结束标签）"
        )

    def finish(self, total_lines: int) -> None:
        for tag, line in self.stack:
            if tag in _OPTIONAL_END:
                continue
            self.problems.append(
                f"第 {line} 行：<{tag}> 一直没有闭合（文件总共 {total_lines} 行）"
            )


def _structure_report(text: str) -> tuple[list[str], int]:
    parser = _StructureParser()
    parser.feed(text)
    parser.close()
    parser.finish(len(text.splitlines()))
    return parser.problems, parser.starts


# --- ② id 复用与引用一致性 --------------------------------------------------

_ID_ATTR = re.compile(r"""\bid\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)

_REF_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"""getElementById\s*\(\s*["']([^"']+)["']"""), "getElementById"),
    (
        re.compile(r"""querySelector(?:All)?\s*\(\s*["']#([A-Za-z][-\w]*)"""),
        "querySelector",
    ),
    (re.compile(r"""\bhref\s*=\s*["']#([^"']+)["']"""), 'href="#…"'),
    (re.compile(r"""\bfor\s*=\s*["']([^"']+)["']"""), 'label for="…"'),
)


def _id_report(text: str) -> tuple[list[str], int, int]:
    """id 声明、重复与引用一致性。返回（问题, 声明数, 引用处数）。"""
    # 声明只看标记：JS 里的 `el.id = "x"` 是动态创建，把它算成声明会掩盖真问题。
    spans = [(body_start, body_start + len(body)) for _k, _a, body, body_start in _blocks(text)]
    spans += [(match.start(), match.end()) for match in _COMMENT_RE.finditer(text)]
    masked = _mask(text, spans)

    declared: dict[str, list[int]] = {}
    for match in _ID_ATTR.finditer(masked):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if not value:
            continue
        declared.setdefault(value, []).append(_line_of(masked, match.start()))

    problems: list[str] = []
    for name, lines in declared.items():
        if len(lines) > 1:
            rest = "、".join(f"第 {n} 行" for n in lines[1:])
            problems.append(
                f'第 {lines[0]} 行：id="{name}" 重复——{rest}也用了同一个，'
                "getElementById 只会拿到第一个"
            )

    refs = 0
    for pattern, how in _REF_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            refs += 1
            if not name or name in declared:
                continue
            problems.append(
                f"第 {_line_of(text, match.start())} 行：{how} 引用了不存在的 "
                f'id "{name}"——文件里没有 id="{name}"'
                "（若它由 JS 动态创建，这条忽略）"
            )
    return problems, len(declared), refs


# --- ③ <script src> / <link href> 的本地相对路径 ---------------------------

_RES_REF = re.compile(
    r"""<(script|link|img|source|audio|video|iframe|embed|object|track)\b[^>]*?"""
    r"""\b(src|href|poster|data)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)

_REMOTE_PREFIX = (
    "http://", "https://", "//", "data:", "mailto:", "javascript:",
    "tel:", "blob:", "about:", "#", "ws://", "wss://", "ftp://",
)


def _resource_report(
    text: str, base_dir: Path, exists: Callable[[Path], bool], label: str
) -> tuple[list[str], int, int]:
    """本地相对资源是否存在。远程 URL 跳过；站点根路径没法在本地判断，也跳过。"""
    problems: list[str] = []
    found = 0
    skipped = 0
    for match in _RES_REF.finditer(text):
        tag = match.group(1).lower()
        attr = match.group(2).lower()
        raw = match.group(3) if match.group(3) is not None else match.group(4)
        value = (raw or "").strip()
        if not value or value.lower().startswith(_REMOTE_PREFIX):
            continue
        path_part = value.split("#", 1)[0].split("?", 1)[0]
        if not path_part:
            continue
        if path_part.startswith(("/", "\\")):
            skipped += 1  # 站点根路径：本地文件系统里对不上，不报也不假装验过
            continue
        target = (base_dir / path_part.replace("\\", "/")).resolve()
        if exists(target):
            found += 1
            continue
        problems.append(
            f'第 {_line_of(text, match.start())} 行：<{tag} {attr}="{value}"> '
            f"指向的本地文件不存在：{target}（相对 {label} 所在目录）"
        )
    return problems, found, skipped


# --- ④ 内联 CSS / .css 的括号与注释配对（浅层） ----------------------------

_CSS_OPEN = "{(["
_CSS_CLOSE = "}])"
_CSS_MATCH = {"}": "{", ")": "(", "]": "["}
_CSS_END = {"{": "}", "(": ")", "[": "]"}


def _css_report(css: str, offset: int = 0) -> tuple[list[str], int]:
    """大括号/圆括号/方括号配对与未闭合注释。

    刻意只做这一层，别装成完整 CSS 解析器：它能把「少一个大括号、注释没关」
    这类错抓住，抓不住语义错误——那要浏览器。
    """
    problems: list[str] = []
    stack: list[tuple[str, int]] = []
    matched = 0
    line = 1 + offset
    i = 0
    quote: str | None = None
    while i < len(css):
        char = css[i]
        if char == "\n":
            line += 1
            i += 1
            continue
        if quote is not None:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end < 0:
                problems.append(
                    f"第 {line} 行：'/*' 注释没有闭合（到文件结束都没等到 '*/'）"
                )
                return problems, matched
            line += css.count("\n", i, end)
            i = end + 2
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if char in _CSS_OPEN:
            stack.append((char, line))
            i += 1
            continue
        if char in _CSS_CLOSE:
            if not stack:
                problems.append(
                    f"第 {line} 行：多出来的 '{char}'（前面没有对应的 "
                    f"'{_CSS_MATCH[char]}'）"
                )
            elif stack[-1][0] != _CSS_MATCH[char]:
                opener, opened = stack.pop()
                problems.append(
                    f"第 {line} 行：'{char}' 与第 {opened} 行的 '{opener}' 不配对"
                )
            else:
                stack.pop()
                matched += 1
            i += 1
            continue
        i += 1
    for opener, opened in stack:
        problems.append(
            f"第 {opened} 行：'{opener}' 没有闭合（到文件结束都没等到 "
            f"'{_CSS_END[opener]}'）"
        )
    return problems, matched


# --- ⑤ node --check（.js 与 HTML 内联脚本共用） ----------------------------


def _js_report(
    root: Path,
    js: str,
    mapping: Sequence[int],
    label: str,
    timeout: int,
) -> tuple[list[str], str | None, str | None]:
    """node --check 一段 JS。把 node 的原始报错翻译成「文件:行 + 这一行是什么」。

    返回（问题, 查不了, 通过说明）。翻译这一步是必须的：node 报的是它自己那份
    临时文件的行号，原文交给模型，它对不回自己的文件——实测它就是这么打转的。
    """
    if shutil.which("node") is None:
        return (
            [],
            f"{CANNOT_CHECK}JS 语法：本机没有 node，这一步没验"
            "（替代：让用户在浏览器里打开，看控制台第一条报错）",
            None,
        )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(js)
        tmp_path = tmp.name
    try:
        # 不传 pending：验的是刚写出来的临时文件，试跑副本对它没有意义，
        # 而复制一份项目只为跑一条 --check 是纯开销。
        result = run_once(root, ["node", "--check", tmp_path], timeout=timeout)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if result.ok:
        return [], None, f"JS 语法检查通过（node --check，{js.count(chr(10)) + 1} 行）"

    body = result.content.split("\n", 1)[1] if "\n" in result.content else result.content
    lines = [line.rstrip() for line in body.splitlines()]
    if lines and lines[0].startswith("$ "):
        lines = lines[1:]  # 去掉回显的那条命令：跑的其实是临时文件

    where = label
    source = ""
    detail = ""
    for index, raw in enumerate(lines):
        located = re.match(r"^(.*?):(\d+)$", raw.strip())
        if not located:
            continue
        js_line = int(located.group(2))
        if 0 < js_line <= len(mapping) and mapping[js_line - 1]:
            where = f"{label}:{mapping[js_line - 1]}"
        else:
            where = f"{label}（内联 JS 第 {js_line} 行）"
        if index + 1 < len(lines):
            source = lines[index + 1].strip()
        break
    for raw in reversed(lines):
        if re.search(r"\b\w*Error\b|SyntaxError:", raw):
            detail = raw.strip()
            break
    if not detail:
        detail = next((raw.strip() for raw in reversed(lines) if raw.strip()), "")

    problem = f"{where}：JS 语法错误：{detail or 'node 报错但没给出结论行'}"
    if source:
        problem += f"；这一行是：{source[:160]}"
    return [problem], None, None


# --- ⑥ Python / JSON / TOML -----------------------------------------------


def _has_tests_near(base_dir: Path) -> bool:
    """这一层或上面两层里有没有测试——有就在结论里告诉它该怎么跑。"""
    for candidate in (base_dir, *list(base_dir.parents)[:2]):
        try:
            if (candidate / "tests").is_dir() or any(candidate.glob("test_*.py")):
                return True
        except OSError:
            continue
    return False


def _python_report(
    text: str, label: str, base_dir: Path
) -> tuple[list[str], str | None]:
    try:
        compile(text.lstrip("\ufeff"), label, "exec")
    except SyntaxError as exc:
        line = exc.lineno or 0
        where = f"{label}:{line}" if line else label
        detail = _line_text(text, line)
        message = f"{where}：Python 语法错误：{exc.msg}"
        if detail:
            message += f"；这一行是：{detail}"
        return [message], None
    except ValueError as exc:
        return [f"{label}：Python 源码无法编译：{exc}"], None
    if _has_tests_near(base_dir):
        return [], '这个项目里有测试：可以用 run_command 跑 ["python", "-m", "pytest", "-q"] 验行为'
    return [], None


def _json_report(text: str, label: str) -> list[str]:
    try:
        json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        message = f"{label}:{exc.lineno}:{exc.colno}：JSON 解析失败：{exc.msg}"
        detail = _line_text(text, exc.lineno)
        if detail:
            message += f"；这一行是：{detail}"
        return [message]
    return []


def _toml_report(text: str, label: str) -> tuple[list[str], str | None]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - 3.11+ 都有 tomllib
        return (
            [],
            f"{CANNOT_CHECK}TOML 解析：本机 Python 没有 tomllib（要 3.11+），这一步没验",
        )
    try:
        tomllib.loads(text)
    except Exception as exc:
        line = getattr(exc, "lineno", None)
        if line is None:
            found = re.search(r"at line (\d+)", str(exc))
            line = int(found.group(1)) if found else None
        where = f"{label}:{line}" if line else label
        message = f"{where}：TOML 解析失败：{exc}"
        detail = _line_text(text, line)
        if detail:
            message += f"；这一行是：{detail}"
        return [message], None
    return [], None


# --- 分派与两个出口（模型工具 / 自动验证） --------------------------------


@dataclass(frozen=True)
class CheckReport:
    """一次按类型检查的结果。

    三样分开是为了两个出口能用同一份事实：模型手动调用时全说（包括通过了
    哪几步）；自动验证只回灌问题与「查不了」，否则每一步都往上下文里灌一堆
    「通过」，等于噪声。
    """

    problems: tuple[str, ...] = ()
    cannot: tuple[str, ...] = ()      # 有验证器，但这台机器上跑不了
    uncovered: tuple[str, ...] = ()   # 这类文件根本没有验证器
    notes: tuple[str, ...] = ()       # 通过了哪几步


def analyze_text(
    root: Path,
    label: str,
    text: str,
    base_dir: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    exists: Callable[[Path], bool] | None = None,
) -> CheckReport:
    """按扩展名分派验证。返回结构化结论，不渲染。"""
    base_dir = base_dir or root
    if exists is None:
        # 默认就是「磁盘上在不在」；调用方可以传一个带待确认改动的判定。
        def exists(target: Path) -> bool:
            return target.exists()

    suffix = _suffix(label)
    problems: list[str] = []
    cannot: list[str] = []
    uncovered: list[str] = []
    notes: list[str] = []

    if suffix in (".html", ".htm"):
        structure, starts = _structure_report(text)
        problems += _with_file(label, structure)
        if not structure:
            notes.append(
                f"标签配对：通过（扫描 {starts} 个开始标签，无交叉嵌套、无多余结束标签）"
            )

        js, mapping = extract_js_with_lines(text)
        if js.strip():
            js_problems, js_cannot, js_note = _js_report(
                root, js, mapping, label, timeout
            )
            problems += js_problems
            if js_cannot:
                cannot.append(js_cannot)
            if js_note:
                notes.append(js_note)
        else:
            notes.append("内联 JS：没有可检查的块（没有 <script>，或都带 src）")

        id_problems, declared, refs = _id_report(text)
        problems += _with_file(label, id_problems)
        if not id_problems:
            notes.append(
                f"id 引用：通过（声明 {declared} 个，引用 {refs} 处，无重复、无悬空）"
            )

        res_problems, found, skipped = _resource_report(
            text, base_dir, exists, label
        )
        problems += _with_file(label, res_problems)
        if not res_problems:
            if found:
                notes.append(f"本地资源：{found} 处引用都存在")
            else:
                notes.append("本地资源：没有引用本地文件（远程 URL 跳过）")
        if skipped:
            cannot.append(
                f"{CANNOT_CHECK}有 {skipped} 处资源用的是站点根路径（以 / 开头），"
                "本地文件系统里对不上，没有验"
            )

        style_blocks = [item for item in _blocks(text) if item[0] == "style"]
        css_problems: list[str] = []
        css_matched = 0
        for _kind, _attrs, body, start in style_blocks:
            found_problems, matched = _css_report(body, _line_of(text, start) - 1)
            css_problems += found_problems
            css_matched += matched
        problems += _with_file(label, css_problems)
        if style_blocks and not css_problems:
            notes.append(
                f"内联 CSS：括号配对通过（{css_matched} 对，浅层检查）"
            )

        # 静态检查到此为止。说不清的那一步必须说出来，不能让它以为「通过了」
        # 就等于「能用」——实测它就是拿着「语法通过」去宣布游戏做完了。
        cannot.append(
            f"{CANNOT_CHECK}{label}：HTML 的运行时行为"
            "（渲染、游戏逻辑、事件是否真的触发）查不了——"
            "那要人工在浏览器里打开看；这里只做静态检查"
        )

    elif suffix == ".js":
        mapping = list(range(1, len(text.splitlines()) + 1))
        js_problems, js_cannot, js_note = _js_report(
            root, text, mapping, label, timeout
        )
        problems += js_problems
        if js_cannot:
            cannot.append(js_cannot)
        if js_note:
            notes.append(js_note)

    elif suffix == ".py":
        py_problems, py_note = _python_report(text, label, base_dir)
        problems += py_problems
        if py_note:
            notes.append(py_note)
        if not py_problems:
            notes.append("Python 语法编译：通过")

    elif suffix == ".json":
        problems += _json_report(text, label)
        if not problems:
            notes.append("JSON 解析：通过")

    elif suffix == ".toml":
        toml_problems, toml_cannot = _toml_report(text, label)
        problems += toml_problems
        if toml_cannot:
            cannot.append(toml_cannot)
        if not toml_problems:
            notes.append("TOML 解析：通过")

    elif suffix == ".css":
        css_problems, matched = _css_report(text)
        problems += _with_file(label, css_problems)
        if not css_problems:
            notes.append(f"括号与注释配对：通过（{matched} 对，浅层检查）")

    else:
        kind = suffix or "无扩展名"
        if suffix in _IMAGE_SUFFIXES:
            alternative = "图片没有机械判据：让用户在浏览器或看图工具里打开确认"
        else:
            alternative = _NO_CHECKER_ALTERNATIVES.get(suffix) or _DEFAULT_ALTERNATIVE
        uncovered.append(
            f"{NO_CHECKER}{kind} 没有对应的验证器，本工具没有验它。可行的替代：{alternative}"
        )

    return CheckReport(
        problems=tuple(problems),
        cannot=tuple(cannot),
        uncovered=tuple(uncovered),
        notes=tuple(notes),
    )


def check_text(
    root: Path,
    label: str,
    text: str,
    base_dir: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    exists: Callable[[Path], bool] | None = None,
) -> ToolResult:
    """模型可见的验证结论：问题、查不了的步骤、通过的步骤全说。"""
    report = analyze_text(root, label, text, base_dir, timeout, exists)
    kind = _suffix(label) or "无扩展名"
    head = f"{label}（{kind}）：" + (
        f"发现 {len(report.problems)} 处问题"
        if report.problems
        else "没有发现问题"
    )
    lines = [head]
    lines += [f"- {item}" for item in report.problems]
    lines += [f"- {item}" for item in report.cannot]
    lines += [f"- {item}" for item in report.uncovered]
    lines += [f"- {item}" for item in report.notes]
    if report.problems:
        lines.append("按「文件:行」逐条改。")
    return ToolResult(ok=not report.problems, content="\n".join(lines))


def verify_file_spec(
    root: Path,
    pending=None,
    read_roots: Sequence[Path] = (),
    timeout: int = DEFAULT_TIMEOUT,
) -> ToolSpec:
    """模型可调用的验证入口：一次调用，按扩展名分派。

    在这之前模型手里**没有**任何「验一个文件」的工具：想验 HTML 只能自己拼命令
    （`node --check x.html`），撞的是 node 的原始报错。所以这个工具的价值不是
    「多一个命令」，而是把「按产出类型验」变成一次调用，并且回灌可照做的结论。
    """

    def handler(args: dict) -> ToolResult:
        try:
            target = resolve_readable(root, args["path"], read_roots)
        except PathOutsideProjectError as exc:
            return ToolResult(ok=False, content=str(exc))

        # 待确认的改动优先于磁盘：它刚写完、还没落盘的文件也必须能验。
        view = WorkspaceView(root, pending)
        if not view.exists(target):
            return ToolResult(
                ok=False,
                content=f"文件不存在: {args['path']}"
                "（刚写完还没落盘的文件也能验：待确认的改动优先于磁盘）",
            )
        if view.is_dir(target):
            return ToolResult(ok=False, content=f"目标是目录而非文件: {args['path']}")
        try:
            text = view.read_text(target)
        except UnicodeDecodeError:
            return ToolResult(
                ok=False,
                content=f"文件不是 UTF-8 文本，无法做静态检查: {args['path']}",
            )
        label = pending.path_of(args["path"]) if pending is not None else args["path"]
        return check_text(
            root,
            label,
            text,
            base_dir=target.parent,
            timeout=timeout,
            exists=view.exists,
        )

    return ToolSpec(
        name="verify_file",
        description=(
            "按产出类型验证一个文件，给「文件:行 + 错在哪 + 怎么改」的结论。"
            "分派：.html 标签配对 / 内联 JS 语法 / id 引用 / 本地资源路径 / 内联 CSS 配对；"
            ".js 用 node --check；.py 语法编译（项目里有测试时提示跑 pytest）；"
            ".json / .toml 解析；.css 括号与注释配对。"
            "其它类型会明确说没有验证器并给可行的替代。"
            "验不了的那一步会明说「查不了」，不会沉默通过。不联网、不引入新依赖"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要验证的文件路径（按扩展名决定验证方式）",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="按类型验产出",
        group="核",
    )


def make_checker(
    root: Path,
    pending,
    timeout: int = DEFAULT_TIMEOUT,
    skip: Sequence[str] = (),
) -> Callable[[Sequence[str]], ToolResult]:
    """自动验证用的按类型检查器。

    看的是**所有还没落盘的改动**（和当年那份 JS 降级验证一致）：旧的缺陷不该
    因为这一步改了别的文件就被放过。只回灌问题与「查不了」——每步都灌一遍
    「通过」是纯噪声，而「没有对应验证器」是能力边界，不是这次改动的问题。
    """

    def check(changed: Sequence[str] = ()) -> ToolResult:
        if pending is None:
            return ToolResult(ok=True, content="")
        changes = [
            change
            for change in pending.items()
            if not Path(change.path).is_absolute()
        ]
        if not changes:
            return ToolResult(ok=True, content="")

        view = WorkspaceView(root, pending)
        problems: list[str] = []
        cannot: list[str] = []
        used = 0
        unchecked = 0
        for change in changes:
            if _suffix(change.path) in skip:
                continue
            if used >= MAX_CHECKED_FILES:
                unchecked += 1
                continue
            used += 1
            target = pending.disk_path(change.path)
            report = analyze_text(
                root,
                change.path,
                change.new_text,
                base_dir=target.parent,
                timeout=timeout,
                exists=view.exists,
            )
            problems += list(report.problems)
            cannot += list(report.cannot)

        if unchecked:
            cannot.append(
                f"{CANNOT_CHECK}还有 {unchecked} 个改动文件没有验"
                "——一次改得太散，验证会比任务本身还贵"
            )
        if not problems and not cannot:
            return ToolResult(ok=True, content="")
        head = (
            f"按产出类型检查 {used} 个文件，发现问题："
            if problems
            else f"按产出类型检查 {used} 个文件，有一条提示："
        )
        body = "\n".join(f"- {item}" for item in (*problems, *cannot))
        return ToolResult(ok=not problems, content=f"{head}\n{body}")

    return check


def make_js_verifier(
    root: Path, pending, timeout: int = DEFAULT_TIMEOUT
) -> Callable[[], ToolResult] | None:
    """对改动的 HTML 文件做验证。

    名字是历史遗留（原先只做 node --check），现在走整套按类型分派：HTML 的
    标签配对、内联 JS 语法、id 引用、本地资源路径、内联 CSS 配对都验。
    node 不在 PATH 里就返回 None（HTML/JS 这一大类确实验不了），不报错。

    这里给的是**详细版**结论（每验过的一步都写出来）——自动验证那条路用的是
    make_checker，只回灌问题，免得每步都往上下文里灌一遍「通过」。
    """
    if shutil.which("node") is None:
        return None

    def verify(changed: Sequence[str] = ()) -> ToolResult:
        html_changes = [
            change
            for change in pending.items()
            if _suffix(change.path) in (".html", ".htm")
        ]
        if not html_changes:
            return ToolResult(ok=True, content="没有 HTML 改动需要验证。")
        view = WorkspaceView(root, pending)
        results: list[str] = []
        all_ok = True
        for change in html_changes[:MAX_CHECKED_FILES]:
            target = pending.disk_path(change.path)
            # 从待确认改动里读内容——改动还没落盘，磁盘上可能没有
            result = check_text(
                root,
                change.path,
                change.new_text,
                base_dir=target.parent,
                timeout=timeout,
                exists=view.exists,
            )
            all_ok = all_ok and result.ok
            results.append(result.content)
        return ToolResult(ok=all_ok, content="\n".join(results))

    return verify


def _has_html_changes(root: Path, pending) -> bool:
    """待确认改动里有没有 HTML 文件。"""
    try:
        return any(
            _suffix(change.path) in (".html", ".htm") for change in pending.items()
        )
    except Exception:
        return False


def _has_checkable_changes(root: Path, pending) -> bool:
    """待确认改动里有没有「有验证器」的文件类型。"""
    if _has_html_changes(root, pending):
        return True
    try:
        return any(
            _suffix(change.path) in CHECKABLE_SUFFIXES for change in pending.items()
        )
    except Exception:
        return False
