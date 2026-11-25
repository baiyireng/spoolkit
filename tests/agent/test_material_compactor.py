"""素材压缩器的验收。

分三层：
- 机械层（去重、截断、判据行逐字保留）—— 单独测，不需要预算；
- 模型输出的机械核对（编造判据行/文件名/换对象都会被拦下）—— 单独测；
- 整体（什么时候压、什么时候一次模型都不叫、失败怎么回退）—— 用假模型测。
"""

from __future__ import annotations

from spoolkit.agent.material_compactor import (
    BULK_ITEM_CHARS,
    MaterialNote,
    build_prompt,
    clip_body,
    compact_material,
    is_judgement,
    material_label,
    mechanical_compact,
    notes_from_material,
    verify_model_output,
)
from spoolkit.context.budget import Budget

GOAL = "找出 tests 里那条失败的测试并修掉"

FAILED_LINE = "FAILED tests/services/test_service_ledger.py::test_停掉之后台账里要写明已经停过"


def _note(key: str, tool: str, body: str, why: str = "") -> MaterialNote:
    return MaterialNote(key=key, tool=tool, body=body, why=why)


def _read(path: str, body: str, why: str = "") -> MaterialNote:
    return _note(f'read_file {{"path": "{path}"}}', "read_file", body, why)


class _Model:
    """假模型：记下每次的 prompt，按剧本回答（answer 是异常就抛）。"""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        assert isinstance(self.answer, str)
        return self.answer


# ---- 机械层 ---------------------------------------------------------------


def test_判据行整行逐字保留而且不参与截断() -> None:
    """读到一段又长又有失败原文的输出时，失败原文一个字都不能被压掉。"""
    body = "x" * 300 + "\n" + FAILED_LINE + "\n" + "y" * 3000
    mech = mechanical_compact([_read("big.log", body)])
    assert mech.protected == (f"- [read_file big.log] {FAILED_LINE}",)
    assert f"- [read_file big.log] {FAILED_LINE}" in mech.text
    assert "…（中间省略" in mech.text  # 普通正文确实被截断了
    assert len(mech.bulk) == 1


def test_同一个对象只留最后一次读数() -> None:
    """同一个文件的两次读数只留后一次：旧的那份不是现状。"""
    mech = mechanical_compact(
        [
            _read("a.py", "第一次读到的正文"),
            _read("a.py", "第二次读到的正文"),
        ]
    )
    assert mech.dropped_items == 1
    assert "第二次读到的正文" in mech.text
    assert "第一次读到的正文" not in mech.text


def test_普通正文被压到每条上限以内而对象仍可核对() -> None:
    mech = mechanical_compact([_read("a.py", "z" * (BULK_ITEM_CHARS * 4))])
    line = mech.bulk[0]
    assert line.startswith("- [read_file a.py] ")
    assert "中间省略" in line
    assert len(line) < BULK_ITEM_CHARS * 2


def test_截断时头尾都留因为结论常在末尾() -> None:
    text = clip_body("A" * 100 + "TAIL-结尾", room=60)
    assert text.startswith("A")
    assert text.endswith("TAIL-结尾")
    assert "中间省略" in text


def test_判据记号认得出来() -> None:
    assert is_judgement("FAILED tests/a.py::test_b")
    assert is_judgement("E   AssertionError: assert 1 == 2")
    assert is_judgement("Traceback (most recent call last):")
    assert is_judgement("退出码 2")
    assert not is_judgement("共 3 个文件，12 行")


def test_抬头能从去重键里取到对象() -> None:
    assert material_label("read_file", 'read_file {"path": "src/a.py"}') == "read_file src/a.py"
    assert material_label("list_dir", "list_dir") == "list_dir"
    assert material_label("run_command", 'run_command {"cmd": "pytest"}') == "run_command"


def test_主循环那份记法能转成本模块的记法() -> None:
    notes = notes_from_material([('read_file {"path": "a.py"}', "read_file", "正文")])
    assert notes[0].label == "read_file a.py"
    assert notes[0].body == "正文"


# ---- 模型输出的机械核对 ---------------------------------------------------


def test_核查放行照着素材写的行() -> None:
    source = "- [read_file a.py] 正文里有 a.py 和 12 行"
    kept, dropped = verify_model_output(
        "- [read_file a.py] a.py 有 12 行", source=source, allowed_labels=["read_file a.py"]
    )
    assert kept == ["- [read_file a.py] a.py 有 12 行"]
    assert dropped == 0


def test_核查拦下编造的判据行() -> None:
    source = "- [read_file a.py] 正文"
    kept, dropped = verify_model_output(
        f"- [read_file a.py] {FAILED_LINE}",
        source=source,
        allowed_labels=["read_file a.py"],
    )
    assert kept == []
    assert dropped == 1


def test_核查拦下编造的文件名() -> None:
    source = "- [read_file a.py] 正文里只有 a.py"
    kept, dropped = verify_model_output(
        "- [read_file a.py] 顺手改一下 ghost_module.py",
        source=source,
        allowed_labels=["read_file a.py"],
    )
    assert kept == []
    assert dropped == 1


def test_核查拦下换掉对象() -> None:
    source = "- [read_file a.py] 正文"
    kept, dropped = verify_model_output(
        "- [read_file 别的.py] 正文", source=source, allowed_labels=["read_file a.py"]
    )
    assert kept == []
    assert dropped == 1


def test_核查拦下不像样子的行() -> None:
    kept, dropped = verify_model_output(
        "我觉得这段素材可以压缩。", source="", allowed_labels=[]
    )
    assert kept == []
    assert dropped == 1


# ---- 整体：什么时候压、什么时候叫模型 -------------------------------------


def test_没到线就不压也不叫模型() -> None:
    """8K 窗口下同样的用量不该触发；一次模型调用都不许发生。"""
    model = _Model("- [read_file a.py] x")
    outcome = compact_material(
        [_read("a.py", "x" * 400)],
        goal=GOAL,
        budget=Budget(8192),
        used=2600,
        model=model,
    )
    assert outcome.decision.should is False
    assert outcome.text == ""
    assert model.prompts == []
    assert outcome.used_model is False


def test_机械层够小就不叫模型() -> None:
    """4K 窗口下确实该压，但机械层一次截断就够了——那就不花模型调用。"""
    model = _Model("- [read_file a.py] x")
    outcome = compact_material(
        [_read("a.py", "x" * 6000)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
    )
    assert outcome.decision.should is True
    assert outcome.text.startswith("【素材（已压缩")
    assert model.prompts == []
    assert outcome.used_model is False
    assert outcome.fallback == ""


def test_机械层不够小才叫模型而且只喂干净上下文() -> None:
    """item_chars 放到不截断，机械层压不下去 → 才轮到模型层。"""
    body = "x" * 6000 + "\n" + FAILED_LINE
    model = _Model("- [read_file a.py] 压缩后的正文")
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
        item_chars=100_000,
    )
    assert outcome.used_model is True
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert GOAL in prompt
    assert "【调用声明】" in prompt
    assert "read_file a.py" in prompt
    assert "【待压缩正文】" in prompt
    assert "700" in prompt  # material_compact_model_tokens
    assert FAILED_LINE not in prompt  # 判据行不进模型
    assert outcome.text.count(FAILED_LINE) == 1  # 但它仍在结果里
    assert "- [read_file a.py] 压缩后的正文" in outcome.text


def test_模型漏掉的判据行也丢不了() -> None:
    """模型只回正文、不提失败行——失败行照样在结果里，因为它是直通的。"""
    body = "x" * 6000 + "\n" + FAILED_LINE
    model = _Model("- [read_file a.py] 只有正文")
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
        item_chars=100_000,
    )
    assert FAILED_LINE in outcome.text
    assert outcome.protected_lines == 1


def test_模型编造判据行时整段回退且不注入() -> None:
    body = "x" * 6000
    model = _Model(f"- [read_file a.py] {FAILED_LINE}")
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
        item_chars=100_000,
    )
    assert outcome.used_model is False
    assert outcome.dropped_lines == 1
    assert "编造" not in outcome.text
    assert outcome.fallback
    assert outcome.text == mechanical_compact(
        [_read("a.py", body)], item_chars=100_000
    ).text


def test_模型编造文件名的那一行被拦下其余的留用() -> None:
    body = "x" * 6000 + " a.py"
    model = _Model(
        "- [read_file a.py] 这一行说的是 a.py\n- [read_file a.py] 顺手改一下 ghost_module.py"
    )
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
        item_chars=100_000,
    )
    assert outcome.used_model is True
    assert outcome.dropped_lines == 1
    assert "这一行说的是 a.py" in outcome.text
    assert "ghost_module.py" not in outcome.text


def test_模型抛异常时回退机械层并留下原因() -> None:
    body = "x" * 6000
    model = _Model(RuntimeError("boom"))
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=model,
        item_chars=100_000,
    )
    assert outcome.used_model is False
    assert "压缩模型调用失败：RuntimeError: boom" in outcome.fallback
    assert outcome.text == mechanical_compact(
        [_read("a.py", body)], item_chars=100_000
    ).text


def test_模型没给出内容就回退() -> None:
    outcome = compact_material(
        [_read("a.py", "x" * 6000)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=_Model("   "),
        item_chars=100_000,
    )
    assert outcome.used_model is False
    assert outcome.fallback == "压缩模型没有给出内容"


def test_模型输出没变短就回退() -> None:
    notes = [_read("a.py", "x" * 6000)]
    mech = mechanical_compact(notes, item_chars=100_000)
    outcome = compact_material(
        notes,
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=_Model("\n".join(mech.bulk)),
        item_chars=100_000,
    )
    assert outcome.used_model is False
    assert outcome.fallback == "压缩模型的输出没有变短"


def test_没有压缩模型时只用机械层并如实记下超预算() -> None:
    body = "x" * 6000
    outcome = compact_material(
        [_read("a.py", body)],
        goal=GOAL,
        budget=Budget(4096),
        used=2800,
        model=None,
        item_chars=100_000,
    )
    assert outcome.used_model is False
    assert outcome.fallback == "没给压缩模型，只用机械层"
    assert outcome.over_budget is True


def test_压缩那一层的额度也会进提示() -> None:
    notes = [_read("a.py", "x" * 6000, why="想看清失败上下文")]
    prompt = build_prompt(GOAL, notes, bulk_lines=["- [read_file a.py] 正文"], budget_tokens=700)
    assert GOAL in prompt
    assert "想看清失败上下文" in prompt
    assert "700 token 以内" in prompt


def test_没有素材时不压也不叫模型() -> None:
    model = _Model("- [read_file a.py] x")
    outcome = compact_material([], goal=GOAL, budget=Budget(4096), used=3400, model=model)
    assert outcome.text == ""
    assert model.prompts == []
