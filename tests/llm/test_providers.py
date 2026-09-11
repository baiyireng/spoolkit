from pathlib import Path

import pytest

from agents_dev.llm.gemini import GeminiGateway
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.llamacpp import LlamaCppGateway
from agents_dev.llm.providers import (
    PROVIDERS,
    ProviderConfig,
    ProviderError,
    load_gateway,
    provider_names,
)


def _config(tmp_path: Path, **kwargs) -> ProviderConfig:
    return ProviderConfig(project_root=tmp_path, **kwargs)


def test_登记了三个供应商() -> None:
    assert provider_names() == ("fake", "gemini", "llamacpp")


def test_未知供应商报错并列出可选项() -> None:
    with pytest.raises(ProviderError) as excinfo:
        load_gateway("openai", _config(Path(".")))
    assert "openai" in str(excinfo.value)
    assert "llamacpp" in str(excinfo.value)


def test_装载假模型并消费脚本(tmp_path: Path) -> None:
    gateway = load_gateway("fake", _config(tmp_path, script=("一条",)))
    assert isinstance(gateway, FakeModel)
    assert gateway.remaining == 1


def test_装载本地模型使用默认地址与模型名(tmp_path: Path) -> None:
    gateway = load_gateway("llamacpp", _config(tmp_path))
    assert isinstance(gateway, LlamaCppGateway)
    assert gateway.model == PROVIDERS["llamacpp"].default_model
    gateway.close()


def test_本地模型可覆盖地址与模型名(tmp_path: Path) -> None:
    gateway = load_gateway(
        "llamacpp", _config(tmp_path, base_url="http://127.0.0.1:9000", model="qwen")
    )
    assert gateway.model == "qwen"
    gateway.close()


def test_缺少密钥时给出可操作提示(tmp_path: Path) -> None:
    with pytest.raises(ProviderError) as excinfo:
        load_gateway("gemini", _config(tmp_path, env={}))
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_密钥可从环境变量取得(tmp_path: Path) -> None:
    gateway = load_gateway("gemini", _config(tmp_path, env={"GEMINI_API_KEY": "k"}))
    assert isinstance(gateway, GeminiGateway)
    gateway.close()


def test_密钥可从项目env文件取得(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")
    gateway = load_gateway("gemini", _config(tmp_path, env={}))
    assert isinstance(gateway, GeminiGateway)
    gateway.close()


def test_显式传入的密钥优先(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")
    gateway = load_gateway("gemini", _config(tmp_path, api_key="explicit"))
    assert isinstance(gateway, GeminiGateway)
    gateway.close()


def test_可覆盖模型名(tmp_path: Path) -> None:
    gateway = load_gateway("gemini", _config(tmp_path, api_key="k", model="custom"))
    assert gateway.model == "custom"
    gateway.close()

