"""第一次跑起来时的配置向导。

为什么要有它：命令行参数齐全，但对第一次用的人来说是**一片空白**——
`agents-dev run --goal "..."` 会回一句"假模型需要 --script"，而真正该说的是
"你还没有配模型，想用哪个？"。这一步不问，用户就得先读文档、再猜参数名，
而多数人会在这一步放弃。

两条纪律：

- **只在交互式终端里问**。管道/脚本/CI 里（stdin 不是终端）绝不弹问题——
  那会挂住别人的自动化；那种场景给一句明确的指路就够了（见 provider_gateway）。
- **密钥不写进配置文件**。配置会被 `agents-dev config` 打印出来，密钥写进去
  就等于每次显示一遍。向导把密钥写进项目根的 `.env`（已在 .gitignore 里），
  那里本来就是文档里说的位置。

每一条都可以**留空跳过**：现在不想填就跳过，回头用 `agents-dev config` 或
改 `.env` 补上，不必为了试用先准备好一切。
"""

import sys
from pathlib import Path
from typing import Callable

from agents_dev import settings
from agents_dev.llm.gemini import load_env_file
from agents_dev.llm.llamacpp import DEFAULT_BASE_URL

CHOICES = {
    "1": ("llamacpp", "llama.cpp（本机，推荐）"),
    "2": ("gemini", "Gemini（云端，需要 API key）"),
    "3": ("fake", "假模型（离线、可复现，用于测试；要配 --script）"),
}

DEFAULT_CHOICE = "1"


def interactive() -> bool:
    """当前是不是交互式终端。管道、重定向、CI 里都不是。"""
    try:
        return sys.stdin.isatty()
    except (ValueError, AttributeError):  # pragma: no cover - 少数被包装过的流
        return False


def needs_setup(argv: list[str] | None = None) -> bool:
    """有没有配过供应商。

    判定只认"配过没有"：命令行给了 `--provider` 也算配过（那次运行自带），
    环境变量给了也算。配置文件里有值当然也算。
    """
    arguments = sys.argv[1:] if argv is None else argv
    if any(item == "--provider" or item.startswith("--provider=") or item == "--engine"
           or item.startswith("--engine=") for item in arguments):
        return False
    return not settings.resolve("provider")[0]


def run_wizard(
    project_root: Path,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> dict[str, str]:
    """问一遍，存起来，返回存进去的值。测试里 ask/say 可注入。"""
    say("还没有配置模型供应商。选一个（回车用默认）：")
    for key, (_, label) in CHOICES.items():
        say(f"  {key}) {label}")
    choice = (ask(f"选择 [{DEFAULT_CHOICE}]：") or DEFAULT_CHOICE).strip()
    provider, _ = CHOICES.get(choice, CHOICES[DEFAULT_CHOICE])

    values: dict[str, str] = {"provider": provider}
    if provider == "llamacpp":
        address = (
            ask(f"服务地址 [{DEFAULT_BASE_URL}]：") or DEFAULT_BASE_URL
        ).strip()
        values["base_url"] = address
        model = ask("模型名（留空用服务端默认，直接回车跳过）：").strip()
        if model:
            values["model"] = model
    elif provider == "gemini":
        key = ask("GEMINI_API_KEY（留空跳过，稍后写进 .env 也行）：").strip()
        if key:
            _write_env_key(project_root, key)
            say(f"密钥已写入 {project_root / '.env'}（已在 .gitignore 里）")
        model = ask("模型名（留空用默认，直接回车跳过）：").strip()
        if model:
            values["model"] = model
    else:
        say("假模型要配 --script（一份应答脚本），例如：")
        say('  agents-dev run --provider fake --script script.json --goal "..."')

    written = settings.save({**settings.load(), **values})
    say(f"已写入 {written}")
    say("以后用 `agents-dev config` 看生效值和来源；这次不再问。")
    return values


def _write_env_key(project_root: Path, key: str) -> None:
    """把密钥写进项目根的 .env，替换已有的同名行。"""
    path = project_root / ".env"
    lines: list[str] = []
    if path.is_file():
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("GEMINI_API_KEY")
        ]
    lines.append(f"GEMINI_API_KEY={key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def has_project_key(project_root: Path) -> bool:
    """项目里有没有 Gemini 密钥（给向导之外的地方判断用）。"""
    return bool(load_env_file(project_root / ".env").get("GEMINI_API_KEY"))
