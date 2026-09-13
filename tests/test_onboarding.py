"""第一次跑起来时的向导。

为什么值得一组测试：它决定"第一次用能不能起来"。两条纪律要钉住——
**只在交互式终端里问**（管道/CI 里弹问题会挂住别人的自动化），
**密钥不写进配置文件**（配置会被 `agents-dev config` 打印出来）。
"""

from pathlib import Path

import pytest

from agents_dev import onboarding, settings


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("AGENTS_DEV_CONFIG", str(path))
    for name in ("PROVIDER", "MODEL", "BASE_URL", "PROXY", "SCRIPT"):
        monkeypatch.delenv(f"AGENTS_DEV_{name}", raising=False)
    return path


def _scripted(answers: dict[str, str]):
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        for prefix, answer in answers.items():
            if prompt.startswith(prefix):
                return answer
        return ""

    return ask, asked


def test_没配过就要向导(config_file: Path) -> None:
    assert onboarding.needs_setup([]) is True


def test_命令行给了供应商就不再问(config_file: Path) -> None:
    assert onboarding.needs_setup(["--provider", "fake"]) is False
    assert onboarding.needs_setup(["--provider=llamacpp"]) is False


def test_环境变量配过也不再问(config_file: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "llamacpp")
    assert onboarding.needs_setup([]) is False


def test_配置文件配过就不再问(config_file: Path) -> None:
    settings.save({"provider": "llamacpp"})
    assert onboarding.needs_setup([]) is False


def test_选_llamacpp_记下地址_模型可留空(config_file: Path) -> None:
    ask, _ = _scripted({"选择": "1", "服务地址": "", "模型名": ""})
    values = onboarding.run_wizard(Path("."), ask=ask, say=lambda _line: None)
    assert values["provider"] == "llamacpp"
    assert values["base_url"] == "http://127.0.0.1:8080"
    assert "model" not in values
    assert settings.load()["provider"] == "llamacpp"


def test_选_gemini_密钥写进_env_而不是配置(config_file: Path, tmp_path: Path) -> None:
    """配置会被 `agents-dev config` 打印出来；密钥写进去等于每次显示一遍。"""
    ask, _ = _scripted({"选择": "2", "GEMINI_API_KEY": "sk-secret", "模型名": ""})
    onboarding.run_wizard(tmp_path, ask=ask, say=lambda _line: None)

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-secret" in env_text
    assert "sk-secret" not in settings.config_path().read_text(encoding="utf-8")


def test_密钥可以留空跳过(config_file: Path, tmp_path: Path) -> None:
    ask, _ = _scripted({"选择": "2", "GEMINI_API_KEY": "", "模型名": ""})
    onboarding.run_wizard(tmp_path, ask=ask, say=lambda _line: None)
    assert not (tmp_path / ".env").exists()
    assert settings.load()["provider"] == "gemini"


def test_写密钥不会覆盖_env_里别的内容(config_file: Path, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("其它=1\nGEMINI_API_KEY=old\n", encoding="utf-8")
    ask, _ = _scripted({"选择": "2", "GEMINI_API_KEY": "new", "模型名": ""})
    onboarding.run_wizard(tmp_path, ask=ask, say=lambda _line: None)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "其它=1" in text
    assert "new" in text
    assert "old" not in text


def test_非交互环境下不认为可以问(config_file: Path, monkeypatch) -> None:
    class _NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(onboarding.sys, "stdin", _NotATty())
    assert onboarding.interactive() is False
