"""回灌进提示的文本：把「本次运行专属」的临时/随机信息换成稳定占位符。

## 一条通则

**任何回灌进提示的文本，都不得包含本次运行专属的临时/随机信息。**

它是可证伪的，而且真的发生过：两条 t=0 的 `add-feature`（同一份代码、相隔
约 66 秒）事件流 20 行里 **18 行逐字节相同**，差的只有 `usage` 与 `final`
两行；唯一的分叉点是自动验证正文里那句

    PytestCacheWarning: could not create cache path …\\Temp\\…\\spool-trial-<8 位随机>\\project\\…

——它落在 `body[:400]` 的第 371 字，于是下一次请求的 token 数变成
21740 vs 21738，贪心解码把这个字节的差别放大成了两句不同的收尾。

时间那一半已经收过了（`memory/transcript.prompt_stamp`：按运行起点归一化）。
这里是同一条口径的另一半：**路径、临时目录、随机名**。

## 只清「进提示」的那一份

落盘日志、给人读的收尾报告、`--events` 里人读的正文、`ToolResult.raw`
（自动审查层的证据）**一个字都不动**——排查时要能照着精确路径回现场。
所以清理发生在「文本即将进 `messages`」那一刻，而不是改工具的返回值。

## 以后新增回灌点怎么照做

只要一段文本会回到模型面前，就在它进 `messages` 之前过一遍 `for_prompt()`：

    from spoolkit.prompt_sanitize import for_prompt

    history.append(Message(role="tool", content=for_prompt(工具输出)))

本仓库现有的出口（新增出口时照下面这几处的写法，别另抄一份实现）：

1. `agent/loop.py` 里装配工具输出那一步 —— **所有工具输出共用这一个出口**；
2. `agent/loop.py::_run_verification` 的验证结论；
3. `tools/verify.py` 的验证正文（`condense_test_output` 与 `verify()` 的返回）；
4. `tools/dispatch.py::_render`、`agents/dispatcher.py::_evidence`（派发那条路）；
5. `agent/loop.py::_assemble` 的各个区段（预取 / 教训 / 督导反馈）。

新增一种「本次运行自己生成的名字」形状时，加进下面的 `_OWN_TEMP_DIRS`
（前缀 + 占位符）就够了——判据是：**同一份内容在不同随机名上重跑，
过完 `for_prompt` 必须逐字相同**。
"""

import os
import re
import tempfile
from pathlib import Path

# 占位符。刻意用 `<…>`：它一眼能看出「这里原本是个路径/名字」，而且不含
# 冒号、反斜杠与数字——不会被本模块的第二遍清理再改一次（幂等）。
TEMP_PLACEHOLDER = "<临时目录>"
TRIAL_PLACEHOLDER = "<试跑副本>"
BENCH_PLACEHOLDER = "<试跑工作区>"
PYTEST_TEMP_PLACEHOLDER = "<pytest 临时目录>"
RANDOM_PLACEHOLDER = "<随机名>"

# 本仓库自己生成的临时目录：前缀 -> 占位符。随机段是 `tempfile.mkdtemp`
# 拼上去的，形状（长度、字母表）由标准库定，不在这里钉死，所以前缀之后
# 只吃到「路径分隔符或空白之前」为止，不写死 8 位。
_OWN_TEMP_DIRS: tuple[tuple[str, str], ...] = (
    # 自动验证的试跑副本（`tools/trial.py`）——这次那个分叉点的来源。
    ("spool-trial-", TRIAL_PLACEHOLDER),
    # 跑分器给每个任务建的沙箱工作区（`bench.py`）。
    ("spool-bench-", BENCH_PLACEHOLDER),
    # pytest 自己租的临时目录：受限环境里它建不出来，于是这句警告会带上
    # 本次副本的完整路径；`pytest-of-<user>` 是它的 `tmp_path` 工厂根。
    ("pytest-cache-files-", PYTEST_TEMP_PLACEHOLDER),
    ("pytest-of-", PYTEST_TEMP_PLACEHOLDER),
    # 外壳（DSH 沙箱）给**本次会话**建的临时父目录：`%TEMP%\\dsh-<6 位>`。
    # `for_prompt` 里临时根那一步先跑，通常已经把它整段吃掉了；这里是
    # 「临时根不在候选表里（另一个用户、另一套环境变量）」时的兜底。
    ("dsh-", RANDOM_PLACEHOLDER),
)

# 本次运行自己生成的**随机文件后缀**：`mkstemp(prefix=".env.", suffix=".tmp")`
# 落在工作区里（`envfile.py`），形状是「名字 + `.` + 随机段 + `.tmp`」。
# 只吃那一段随机字符，前后照旧：读目录、看 OSError 时仍然知道是哪个文件的临时件。
_MKSTEMP_SUFFIX = re.compile(r"(\.[^\\/\s]*\.)[A-Za-z0-9_]{6,10}(\.tmp)\b")

# 「运行专属的根路径」候选：系统临时目录，以及外壳从环境变量里告诉本次运行的
# 状态目录。绝对路径落在它们里面，就说明「那是本次运行的临时位置」。
_TEMP_ENV_KEYS = ("TEMP", "TMP", "TMPDIR")
_RUN_ROOT_ENV_KEYS = ("SPOOLKIT_STATE", "SPOOLKIT_CONFIG", "SPOOLKIT_ENV_FILE")

# POSIX 上约定俗成的临时根（环境变量没给时也要认得）。
_FIXED_TEMP_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")

# 「一看就是临时库」的目录名。只用来决定「要不要连父目录一起吃」。
_TEMP_STORE_NAMES = frozenset({"temp", "tmp", "temporary items", "cache", "caches"})


def _clean_root(raw: object) -> str:
    """把一个候选根规范化成可比较的形式：去空白、去尾部斜杠。"""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if len(text) > 3:
        text = text.rstrip("\\/")
    return text


def run_roots() -> tuple[str, ...]:
    """本次运行专属的根路径（去重、长的在前）。

    长的在前是为了先吃更具体的根：`%TEMP%\\dsh-x` 在 `%TEMP%` 之前命中，
    替换结果更短，也不会留下半个随机名。
    """
    candidates: list[str] = []
    for key in _TEMP_ENV_KEYS:
        candidates.append(os.environ.get(key, ""))
    for key in _RUN_ROOT_ENV_KEYS:
        value = _clean_root(os.environ.get(key, ""))
        if value:
            # 状态目录常是一个**文件**（`SPOOLKIT_ENV_FILE`），取它的父目录。
            candidates.append(value if os.path.isdir(value) else os.path.dirname(value))
    try:
        candidates.append(tempfile.gettempdir())
    except (OSError, ValueError):  # 环境坏到连临时目录都问不出来
        pass
    candidates.extend(_FIXED_TEMP_ROOTS)

    # 外壳常把 `%TEMP%` 再套一层（本次会话专属的一个随机子目录），而它报出来的
    # 路径可能是**上一层**的临时库。父目录看起来就是临时库时一并收进来。
    for raw in list(candidates):
        parent = _temp_store_parent(_clean_root(raw))
        if parent:
            candidates.append(parent)

    seen: dict[str, None] = {}
    for raw in candidates:
        text = _clean_root(raw)
        if len(text) < 2:  # 单字符根（比如 `/`）吃下去会把整段文本清空
            continue
        seen[text] = None
    return tuple(sorted(seen, key=len, reverse=True))


def _temp_store_parent(root: str) -> str:
    """`root` 的父目录，**只在父目录一看就是临时库时**才返回（否则空串）。

    判据是父目录的名字，不是深度：`/tmp` 的父目录是 `/`（不吃），
    `C:\\Users\\x\\AppData\\Local\\Temp\\dsh-abc123` 的父目录是 `…\\Temp`（吃）。
    """
    parent = os.path.dirname(root)
    if not parent:
        return ""
    if os.path.basename(parent).lower() not in _TEMP_STORE_NAMES:
        return ""
    return parent


def _root_pattern(root: str) -> re.Pattern[str]:
    """把根路径编译成「路径边界」匹配：后面必须跟分隔符或到头。

    不这么钉的话，`C:\\Temp` 会命中 `C:\\Tempx\\...`，把别人的目录名也吃掉。
    两种分隔符都认（Windows 上工具输出里两种都会出现），大小写不敏感
    （Windows 路径本来就不分大小写）。
    """
    # 先按两种分隔符切开、逐段转义、再用 `[\\/]` 拼回去。**不能**先整体转义
    # 再把 `\\` 换成 `[\\/]`：替换串自己就带斜杠，第二轮会把刚插进去的那一截
    # 再改一次（`[\\/]` -> `[\\[\\/]]`），正则于是永远匹配不上——这个坑踩过。
    parts = re.split(r"[\\/]+", root)
    body = r"[\\/]".join(re.escape(part) for part in parts)
    return re.compile(body + r"(?=[\\/]|$)", re.IGNORECASE)


# 编译好的根路径表 + 它是按哪一组根编的（根的清单来自环境，变了就重编）。
_ROOT_PATTERNS: list[re.Pattern[str]] = []
_ROOT_PATTERNS_KEY: tuple[str, ...] = ()


def _patterns() -> list[re.Pattern[str]]:
    """根路径的正则表。按环境给的根清单缓存：每个字符都重编一次太浪费。"""
    global _ROOT_PATTERNS, _ROOT_PATTERNS_KEY
    roots = run_roots()
    if roots != _ROOT_PATTERNS_KEY:
        _ROOT_PATTERNS = [_root_pattern(root) for root in roots]
        _ROOT_PATTERNS_KEY = roots
    return _ROOT_PATTERNS


def for_prompt(text: str) -> str:
    """把文本里本次运行专属的临时/随机信息换成占位符。

    清四种形态（顺序不能换：先按根路径整段吃，剩下的零散名字再按形状吃）：

    1. 本次运行的临时根/状态根前缀（`%TEMP%`、`/tmp`、`SPOOLKIT_STATE` 等）
       -> `<临时目录>`；
    2. 自己生成的临时目录 `spool-trial-*` -> `<试跑副本>`、
       `spool-bench-*` -> `<试跑工作区>`、`pytest-cache-files-*` /
       `pytest-of-*` -> `<pytest 临时目录>`、`dsh-*` -> `<随机名>`；
    3. `mkstemp` 留下的随机文件后缀（`.env.<随机>.tmp`）里那一段随机字符
       -> `<随机名>`；
    4. 已经换成占位符的文本再进来一次**不再变化**（幂等）——回灌点有嵌套
       （验证正文进循环、循环再拼进提示），这一条保证多层调用不会互相打架。

    不改措辞、不动换行、不做截断：那些是调用方的事，这一层只换字节。
    """
    if not text:
        return text
    body = str(text)
    for pattern in _patterns():
        body = pattern.sub(TEMP_PLACEHOLDER, body)
    for prefix, placeholder in _OWN_TEMP_DIRS:
        body = re.sub(
            re.escape(prefix) + r"[^\\/\s\"'()\[\],;：，。]*",
            placeholder,
            body,
            flags=re.IGNORECASE,
        )
    return _MKSTEMP_SUFFIX.sub(
        lambda match: f"{match.group(1)}{RANDOM_PLACEHOLDER}{match.group(2)}", body
    )


def restore_temp_placeholder(text: str) -> str:
    """把 `<临时目录>` 占位符还原成本次运行的真实临时根。

    fix-bug 探针真机现场：工作区落在 %TEMP% 下的沙箱里，提示词里的根路径
    被抹成了 `<临时目录>`，模型照抄它当 `run_command` 的 cwd——
    「工作目录不存在: <临时目录>\\spoolkit-probes\\…」。抹除是为了可复现性
    （换随机副本名重跑提示词逐字相同），不该以「模型照抄的路径跑不通」
    为代价：照抄它看到的字符串是合理行为，解析层要认得回来。

    还原策略：逐个根拼回去，**拼出来真实存在**的优先；都不存在时用第一个
    根（通常就是 %TEMP% 本身——路径不存在会让工具报「目录不存在」，
    那句报错现在带真实绝对路径与目录清单，比占位符有信息量）。
    `<试跑副本>`/`<随机名>` 这类一对多的占位符无法精确还原，不动——
    它们指向的是验证器内部的临时位置，模型本来就不该直接碰。
    """
    if not text or TEMP_PLACEHOLDER not in text:
        return text
    roots = run_roots()
    if not roots:
        return text
    for root in roots:
        candidate = text.replace(TEMP_PLACEHOLDER, root)
        if Path(candidate).exists():
            return candidate
    return text.replace(TEMP_PLACEHOLDER, roots[0])
