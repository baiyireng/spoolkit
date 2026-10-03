"""意图（`why`）必须在**语法**里，而且不能混进工具参数。

两件事都是硬要求，而且各自都有一种「看起来能用、实际全错」的写法：

① `why` 只写在提示词里、不进 schema：约束解码（GBNF）只会采样 schema 里
   出现过的字段，模型**根本发不出**这个字段。写进提示词等于写在墙上的告示。
② `why` 放进 `arguments` 里：`arguments` 是「本次可用工具参数的并集」，且
   参数校验走 `additionalProperties: false`——`why` 会被当成某个工具的参数，
   撞上校验，于是**每一次调用都失败**。

所以它必须与 `arguments` 平级。
"""

from pathlib import Path

from spoolkit.agent.protocol import build_turn_schema
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    return registry


def _item_schema(tmp_path: Path) -> dict:
    schema = build_turn_schema(_registry(tmp_path))
    return schema["properties"]["tool_calls"]["items"]


def test_意图在语法里有位置(tmp_path: Path) -> None:
    item = _item_schema(tmp_path)
    assert "why" in item["properties"], "约束解码采样不到 why，模型发不出意图"


def test_意图与参数平级(tmp_path: Path) -> None:
    """挪进 arguments 会让每次调用都因「未知参数」失败。"""
    item = _item_schema(tmp_path)
    assert "why" in item["properties"]
    assert "why" not in item["properties"]["arguments"]["properties"]


def test_意图不进必填项(tmp_path: Path) -> None:
    """必填的话，一个写不出理由的模型会连工具都调不了——那比没有意图更坏。"""
    item = _item_schema(tmp_path)
    assert item["required"] == ["name", "arguments"]


def test_表态字段在回合顶层(tmp_path: Path) -> None:
    schema = build_turn_schema(_registry(tmp_path))
    assert "assessment" in schema["properties"]
    assert "assessment" not in schema["required"]


def test_收窄之后意图仍然在语法里(tmp_path: Path) -> None:
    """收窄（只能写）那条路上也要能写意图，否则最后几步的日志全是无意图的。"""
    registry = _registry(tmp_path)
    schema = build_turn_schema(registry, only=["read_file"])
    item = schema["properties"]["tool_calls"]["items"]
    assert "why" in item["properties"]
    assert item["properties"]["name"]["enum"] == ["read_file"]
