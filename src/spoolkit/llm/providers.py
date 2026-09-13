"""模型供应商装载器。

每个供应商的构造要求都不一样：llama.cpp 只要一个地址，Gemini 要密钥，
假模型要一份脚本。把这层差异收敛在装载器里，命令行与主循环就只面对
统一的 ModelGateway 接口，加一个供应商不需要改任何上层代码。

新增供应商只需两步：写一个实现 ModelGateway 的类，然后在这里登记。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from spoolkit.llm.fake import FakeModel
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.gemini import DEFAULT_MODEL as GEMINI_MODEL
from spoolkit.llm.gemini import GeminiGateway, load_env_file
from spoolkit.llm.llamacpp import DEFAULT_BASE_URL
from spoolkit.llm.llamacpp import DEFAULT_MODEL as LOCAL_MODEL
from spoolkit.llm.llamacpp import LlamaCppGateway
from spoolkit.llm.tokenizer import OfflineTokenCounter


class ProviderError(RuntimeError):
    """供应商无法初始化。"""


@dataclass(frozen=True)
class ProviderConfig:
    """构造网关所需的全部外部输入。"""

    project_root: Path
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    proxy: str | None = None
    script: tuple[str, ...] = ()
    timeout: float = 120.0
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Provider:
    """一个供应商的登记信息。"""

    name: str
    description: str
    default_model: str
    requires_api_key: bool
    build: Callable[[ProviderConfig], ModelGateway]


def _resolve_key(config: ProviderConfig) -> str:
    """密钥来源优先级：显式传入 > 环境变量 > 项目根 .env。"""
    if config.api_key:
        return config.api_key
    if config.env.get("GEMINI_API_KEY"):
        return config.env["GEMINI_API_KEY"]
    return load_env_file(config.project_root / ".env").get("GEMINI_API_KEY", "")


def _build_fake(config: ProviderConfig) -> ModelGateway:
    return FakeModel(
        script=list(config.script), tokenizer=OfflineTokenCounter()
    )


def _build_gemini(config: ProviderConfig) -> ModelGateway:
    key = _resolve_key(config)
    if not key:
        raise ProviderError(
            "缺少 GEMINI_API_KEY：请在项目根目录的 .env 中配置，或设置同名环境变量"
        )
    return GeminiGateway(
        key,
        model=config.model or GEMINI_MODEL,
        timeout=config.timeout,
        proxy=config.proxy,
    )


def _build_llamacpp(config: ProviderConfig) -> ModelGateway:
    return LlamaCppGateway(
        base_url=config.base_url or DEFAULT_BASE_URL,
        model=config.model or LOCAL_MODEL,
        timeout=config.timeout,
        proxy=config.proxy,
    )


PROVIDERS: dict[str, Provider] = {
    "fake": Provider(
        name="fake",
        description="脚本化假模型，离线可用，用于测试与无 GPU 环境",
        default_model="",
        requires_api_key=False,
        build=_build_fake,
    ),
    "llamacpp": Provider(
        name="llamacpp",
        description="本地 llama.cpp 服务，默认 http://127.0.0.1:8080",
        default_model=LOCAL_MODEL,
        requires_api_key=False,
        build=_build_llamacpp,
    ),
    "gemini": Provider(
        name="gemini",
        description="Google Gemini，需要 GEMINI_API_KEY，用于验证 agent 循环",
        default_model=GEMINI_MODEL,
        requires_api_key=True,
        build=_build_gemini,
    ),
}


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDERS))


def describe_providers() -> str:
    return "\n".join(
        f"  {name}: {PROVIDERS[name].description}" for name in provider_names()
    )


def load_gateway(name: str, config: ProviderConfig) -> ModelGateway:
    """按名称装载网关。未知供应商直接报错，不静默回退。"""
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(
            f"未知供应商: {name}；可用: {', '.join(provider_names())}"
        )
    return provider.build(config)

