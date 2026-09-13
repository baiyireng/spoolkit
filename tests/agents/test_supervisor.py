"""督导的结论怎么解析，以及它的越权怎么被夹住。

督导是一次模型调用，所以它会给错东西：给一堆步数、给个没听过的动作、
给一段不能解析的文本。这里断言的是**我们不信它**的部分——
续期的上限由循环说了算，不是它说了算。
"""

import json

from spoolkit.agents.supervisor import (
    EXTEND,
    MAX_GRANT,
    REDIRECT,
    STOP,
    Evidence,
    parse_verdict,
)


def _evidence(**kwargs) -> Evidence:
    settings = {"goal": "把 a.py 里的 bug 修掉", "step": 10, "limit": 10, "ceiling": 80}
    settings.update(kwargs)
    return Evidence(**settings)


def test_解析续期() -> None:
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 6, "reason": "每步都在读新的文件"}),
        _evidence(),
    )
    assert verdict is not None
    assert verdict.action == EXTEND
    assert verdict.grants == 6
    assert verdict.reason == "每步都在读新的文件"


def test_给再多也不能超过单次上限() -> None:
    """它想给 999 步不算数。越权一次，总上限就等于不存在。"""
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 999, "reason": "还需要很多步"}),
        _evidence(),
    )
    assert verdict is not None
    assert verdict.grants == MAX_GRANT


def test_不能超过总上限剩下来的部分() -> None:
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 50, "reason": "继续"}),
        _evidence(step=77, ceiling=80),
    )
    assert verdict is not None
    assert verdict.grants == 3


def test_没有余量时续期等于收手() -> None:
    """总上限用完了，续期就无从谈起——这时候含糊地「继续」最危险。"""
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 5, "reason": "继续"}),
        _evidence(step=80, ceiling=80),
    )
    assert verdict is not None
    assert verdict.action == STOP
    assert verdict.grants == 0


def test_改道会带上要发给它的话() -> None:
    verdict = parse_verdict(
        json.dumps(
            {
                "action": REDIRECT,
                "steps": 4,
                "reason": "连着四次读同一个目录",
                "message": "别再看目录了，直接读 a.py 的第 12 行",
            }
        ),
        _evidence(),
    )
    assert verdict is not None
    assert verdict.action == REDIRECT
    assert verdict.grants == 4
    assert verdict.message == "别再看目录了，直接读 a.py 的第 12 行"


def test_解析不出来就不续期() -> None:
    assert parse_verdict("我觉得还行吧", _evidence()) is None
    assert parse_verdict("[1, 2]", _evidence()) is None
    assert parse_verdict(json.dumps({"action": "继续"}), _evidence()) is None


def test_证据只带最近的那些行() -> None:
    """它要判断的是「这些动作有没有进展」，把整条轨迹塞进去只是烧预算。"""
    evidence = _evidence(trace=tuple(f"step{i}" for i in range(500)), trace_tail=40)
    rendered = evidence.render()
    assert rendered["trace"].count("step") == 40
    assert "step499" in rendered["trace"]
    assert "step459" not in rendered["trace"]
