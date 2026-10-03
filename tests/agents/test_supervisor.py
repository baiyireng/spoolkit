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


def test_证据带监测字段() -> None:
    evidence = _evidence(cycle="write_file→run_command", oscillations=1, same_query=3)
    rendered = evidence.render()
    assert rendered["cycle"] == "write_file→run_command"
    assert rendered["oscillations"] == 1
    assert rendered["same_query"] == 3


def test_监测计数旋钮默认值() -> None:
    from spoolkit import limits

    assert limits.resolve("name_cycle_window") == (12.0, "默认")
    assert limits.resolve("name_cycle_at") == (1.0, "默认")
    assert limits.resolve("oscillation_at") == (1.0, "默认")
    assert limits.resolve("same_query_at") == (3.0, "默认")


# ---- 「建议有没有被当成命令」：表态必须回传到督导面前 ----------------------


def test_上次的建议与它的表态都要给督导看() -> None:
    """看不到表态，督导只能看到「我又建议了一次、它没照做」——
    下一个结论就会从建议滑向命令（加长、加重、最后收手）。"""
    evidence = _evidence(
        asking="它已经连续 8 步只看不写",
        suggestion="别再看目录了，直接读 a.py",
        assessment="不接受：任务要求的是先弄清现状，我已经在读 a.py 了",
    )
    asking = evidence.render()["asking"]
    assert "它已经连续 8 步只看不写" in asking
    assert "你上一次给它的建议：别再看目录了，直接读 a.py" in asking
    assert "它上一次的表态：不接受" in asking


def test_没有建议也没有表态时不加料() -> None:
    """没东西可说时不要硬加——那会变成每一个请求都要付的噪声。"""
    assert _evidence(asking="预算用完了").render()["asking"] == "预算用完了"


def test_建议还没被回答时也要说出来() -> None:
    """「它还没走到回答那一步」与「它不理我」是两件事。"""
    rendered = _evidence(asking="步数用完了", suggestion="直接写文件").render()
    assert "它上一次没有表态" in rendered["asking"]
    assert "直接写文件" in rendered["asking"]


def test_占位符集合没变() -> None:
    """表态折进 asking 而不是新开占位符：那一组是冻结的（见
    tests/test_prompt_mechanics.py），改它等于顺手改了一处没有人要求的约定。"""
    from spoolkit.context import templates as T

    assert "{assessment}" not in T.SUPERVISE
    assert "{suggestion}" not in T.SUPERVISE


# ---- 「一边说这是正常探索，一边收手」要被兜住 -------------------------------


def test_说正常探索却收手时改成续期() -> None:
    """复跑「优化一下旧文件的代码」（预算 3 步）的现场：

    它的 reason 是「任务处于正常探索阶段，尚未提出改动……总步数预算充足
    （剩余 21 步）」，action 却是 stop——自己说的和做的是两件事，
    而 stop 把一次尚未开始的探索判死。
    """
    evidence = _evidence(
        step=3, limit=3, ceiling=83,
        asking="这一轮的步数预算用完了，任务还没结束",
        reason="任务处于正常探索阶段，尚未提出改动，下一步应提出具体优化",
    )
    verdict = parse_verdict(
        json.dumps(
            {
                "action": STOP,
                "reason": "任务处于正常探索阶段，尚未提出改动，下一步应提出具体优化",
            }
        ),
        evidence,
    )
    assert verdict is not None
    assert verdict.action == EXTEND
    assert verdict.grants > 0


def test_总上限用尽时它仍然可以收手() -> None:
    """兜底只在「还有步数可给」时生效：没有余量时收手是诚实的。"""
    evidence = _evidence(
        step=80, limit=80, ceiling=80, reason="正常探索但总上限已经用完了"
    )
    verdict = parse_verdict(
        json.dumps({"action": STOP, "reason": "正常探索但总上限已经用完了"}),
        evidence,
    )
    assert verdict is not None
    assert verdict.action == STOP


def test_真的做不下去时收手不被改写() -> None:
    """理由里没有「正常探索」那类话时，它的 stop 原样生效——
    兜底改的是自相矛盾，不是替它做判断。"""
    evidence = _evidence(
        step=30, limit=30, ceiling=80, reason="缺一个读不到的目录，这条路走不通"
    )
    verdict = parse_verdict(
        json.dumps({"action": STOP, "reason": "缺一个读不到的目录，这条路走不通"}),
        evidence,
    )
    assert verdict is not None
    assert verdict.action == STOP


def test_兜底只看它自己写的话不看工具输出() -> None:
    """踩过的坑：判据要在 `Evidence.reason`（它写的理由）与我们的提问里找，
    不在 `trace` 里找。

    两个理由：① `parse_verdict` 里也有一个叫 `reason` 的局部变量，但那是
    **这一次**的结论——`Evidence.reason` 是同一件事，两处来源早晚会有一处
    忘了传（第一次复跑就是这么静默失效的）；② trace 里是工具输出与报错，
    那些字不是它的判断。
    """
    from dataclasses import replace

    from spoolkit.agents.supervisor import _stopping_before_exploration

    evidence = _evidence(
        step=3, limit=3, ceiling=83,
        asking="这一轮的步数预算用完了，任务还没结束",
    )
    assert _stopping_before_exploration(
        replace(evidence, reason="当前处于正常探索阶段，尚未提出改动")
    )
    assert not _stopping_before_exploration(
        replace(evidence, reason="缺一个读不到的目录")
    )
    # trace 里就算写着「先读后计划」也不算数：那不是它的判断。
    assert not _stopping_before_exploration(
        replace(evidence, reason="读不到东西", trace=("step1: 像是先读后计划",))
    )


def test_它说正常探索却收手时走的是它写的理由() -> None:
    """接点：`supervise` 把它写的理由回填进证据，兜底才读得到。

    这一条必须走 `supervise`（而不是直接调 `parse_verdict`）：回填那一步
    就在它里面。少了它，`Evidence.reason` 永远是空串，兜底静默失效——
    第一次复跑踩的就是这个形状。
    """
    from spoolkit.agents.supervisor import supervise
    from spoolkit.llm.fake import FakeModel
    from spoolkit.llm.tokenizer import OfflineTokenCounter

    answer = json.dumps(
        {"action": STOP, "reason": "当前处于正常探索阶段，尚未提出改动"},
        ensure_ascii=False,
    )
    verdict = supervise(
        FakeModel(script=[answer], tokenizer=OfflineTokenCounter()),
        _evidence(
            step=3, limit=3, ceiling=83,
            asking="这一轮的步数预算用完了，任务还没结束",
        ),
    )
    assert verdict is not None
    assert verdict.action == EXTEND, "回填没做：它写的理由没进证据"


def test_它照规矩续期时兜底不插手() -> None:
    """我们只在它自相矛盾时补一手；它做得对时一切原样。"""
    evidence = _evidence(
        step=3, limit=3, ceiling=83, reason="还在正常探索，拿到新信息"
    )
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 4, "reason": "还在正常探索，拿到新信息"}),
        evidence,
    )
    assert verdict is not None
    assert verdict.action == EXTEND
    assert verdict.grants == 4


def test_说了续期却没报步数时按默认额度续() -> None:
    """**复跑里踩出来的那一条**：它写 action=extend、reason 说「剩余 21 步充足」，
    但 payload 里没有 steps 字段（schema 里 steps 不是必填）。

    旧行为把它读成「不续期」→ stop → 一次尚未开始的探索被判死，
    而它自己明明说了要接着走。后果与「边探索边收手」一模一样。
    """
    evidence = _evidence(
        step=3, limit=3, ceiling=83, reason="当前处于正常探索阶段，剩余 21 步预算充足"
    )
    verdict = parse_verdict(
        json.dumps(
            {"action": EXTEND, "reason": "当前处于正常探索阶段，剩余 21 步预算充足"}
        ),
        evidence,
    )
    assert verdict is not None
    assert verdict.action == EXTEND
    assert verdict.grants > 0


def test_它明确报零步时才收手() -> None:
    """「没报」与「报了 0」是两件事：后者是它明确的意思，照办。"""
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "steps": 0, "reason": "没必要再走了"}),
        _evidence(step=3, limit=3, ceiling=83),
    )
    assert verdict is not None
    assert verdict.action == STOP


def test_没报步数但总上限用尽时仍然停() -> None:
    """默认额度也要过安全线：总上限没有余量时，续期无从谈起。"""
    verdict = parse_verdict(
        json.dumps({"action": EXTEND, "reason": "接着走"}),
        _evidence(step=80, ceiling=80, limit=80),
    )
    assert verdict is not None
    assert verdict.action == STOP
