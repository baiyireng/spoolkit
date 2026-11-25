from pathlib import Path

from spoolkit.memory.store import init_memory_schema
from spoolkit.memory.transcript import (
    ASSISTANT,
    DEICTIC_REMINDER,
    DIGEST_HEADER,
    PRIOR_HEADER,
    USER,
    conversational_prompt,
    recent_messages,
    record_message,
    render_transcript,
)
from spoolkit.store.db import open_db


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "memory.db")
    init_memory_schema(conn)
    return conn


def test_记录并按时间取回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "问一句")
    record_message(conn, "s", ASSISTANT, "答一句")
    rows = recent_messages(conn, "s")
    assert [row["content"] for row in rows] == ["问一句", "答一句"]
    conn.close()


def test_会话之间互相隔离(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "a", USER, "属于 a")
    record_message(conn, "b", USER, "属于 b")
    rows = recent_messages(conn, "a")
    assert [row["content"] for row in rows] == ["属于 a"]
    conn.close()


def test_只取最近若干条且按正序返回(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(10):
        record_message(conn, "s", USER, f"第{i}条")
    rows = recent_messages(conn, "s", limit=3)
    assert [row["content"] for row in rows] == ["第7条", "第8条", "第9条"]
    conn.close()


def test_元信息随消息一起保存(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", ASSISTANT, "做完了", meta="3 步，输入 1200 token")
    rows = recent_messages(conn, "s")
    assert "3 步" in rows[0]["meta"]
    conn.close()


def test_渲染包含角色与内容(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "给项目加个功能")
    record_message(conn, "s", ASSISTANT, "已完成")
    text = render_transcript(recent_messages(conn, "s"))
    assert "你：" in text
    assert "给项目加个功能" in text
    assert "助手：" in text
    conn.close()


def test_空记录有明确提示(tmp_path: Path) -> None:
    assert "还没有历史" in render_transcript([])


def test_内容首尾空白被去掉(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "  有空白  ")
    assert recent_messages(conn, "s")[0]["content"] == "有空白"
    conn.close()


def test_上一轮对话不含当前这句话(tmp_path: Path) -> None:
    """QQ / 网页每条消息是一次新 run。当前句已经是目标，再塞一遍只浪费窗口。"""
    conn = _conn(tmp_path)
    record_message(conn, "bridge", USER, "厦门")
    record_message(conn, "bridge", ASSISTANT, "厦门近两天多云。")
    record_message(conn, "bridge", USER, "那天气呢")
    text = conversational_prompt(conn, "bridge", "那天气呢")
    assert "厦门" in text
    assert "近两天多云" in text
    assert "当前：那天气呢" in text
    assert text.count("那天气呢") == 1
    conn.close()


def test_没有上一轮就只给当前目标(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "bridge", USER, "查一下天气")
    assert conversational_prompt(conn, "bridge", "查一下天气") == "查一下天气"
    conn.close()


def test_长答复截断不把整段历史塞回去(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "厦门")
    record_message(conn, "s", ASSISTANT, "详" * 800)
    record_message(conn, "s", USER, "那杭州呢")
    text = conversational_prompt(conn, "s", "那杭州呢", max_chars=80)
    assert "厦门" in text
    assert "详" * 800 not in text
    assert "…" in text
    conn.close()


# --- 意图识别那两件（Task 4）：更早那一段的摘要 + 指代词提醒 ---------------


def test_两行抬头各自点明用途与边界() -> None:
    """抬头是发给模型的规格，不是装饰：两段都不是当前任务。

    钉要点，不钉整句：措辞可以再改，但「截断摘要」「指代」「不是当前任务」
    这几个限定丢了，模型就会把旧往来当成这一轮的要求。
    """
    assert "截断摘要" in DIGEST_HEADER
    assert "指代" in DIGEST_HEADER and "承接" in DIGEST_HEADER
    assert "不是当前任务" in DIGEST_HEADER
    assert "指代" in PRIOR_HEADER and "不是当前任务" in PRIOR_HEADER


def test_更早的往来进摘要_最近的仍是原文(tmp_path: Path) -> None:
    """上下文里天然带一段更早的摘要，模型不必自己去检索。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "给项目加个导出功能")
    record_message(conn, "s", ASSISTANT, "导出写完了，用了 csv 模块")
    record_message(conn, "s", USER, "顺手把帮助也改一下")
    record_message(conn, "s", ASSISTANT, "帮助改好了")
    record_message(conn, "s", USER, "那它支持中文吗")
    text = conversational_prompt(
        conn, "s", "那它支持中文吗", turns=2, digest_turns=6, digest_chars=400
    )
    assert DIGEST_HEADER in text
    assert "导出功能" in text and "csv 模块" in text
    assert PRIOR_HEADER in text and "帮助改好了" in text
    assert "当前：那它支持中文吗" in text
    assert text.count("那它支持中文吗") == 1
    conn.close()


def test_摘要是有界的_不把历史原文整段塞回去(tmp_path: Path) -> None:
    """历史再长、单条再长，摘要那一段的总量都不涨（条数 + 字符预算兜住）。"""
    conn = _conn(tmp_path)
    for index in range(12):
        record_message(conn, "s", USER, f"历史第{index}条" + "长" * 300)
        record_message(conn, "s", ASSISTANT, "答" * 300)
    record_message(conn, "s", USER, "读一下 config.py")
    text = conversational_prompt(
        conn, "s", "读一下 config.py", turns=4, max_chars=40,
        digest_turns=12, digest_chars=200,
    )
    head, sep, _ = text.partition(PRIOR_HEADER)
    assert sep  # 两段都在
    header = DIGEST_HEADER + "\n"
    assert len(head) <= len(header) + 200
    assert "长" * 100 not in head  # 单条也截断：没有整段原文
    # 更早那一段排在最前、最近那几轮在后面，次序是时间正序。
    assert text.index(DIGEST_HEADER) < text.index(PRIOR_HEADER)
    conn.close()


def test_摘要单条也按字符预算截断(tmp_path: Path) -> None:
    """摘要不是原文：用户那侧按预算截，助手那侧取一半。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "甲" * 300)
    record_message(conn, "s", ASSISTANT, "乙" * 300)
    record_message(conn, "s", USER, "补一轮")
    text = conversational_prompt(
        conn, "s", "补一轮", turns=1, digest_turns=6, digest_chars=60
    )
    # 摘要那一段只装得下一条：被保留的是最新的那条（你），且按预算截断。
    digest = text.split(PRIOR_HEADER)[0]
    assert "甲" * 61 not in digest
    assert "甲" * 60 in digest
    conn.close()


def test_摘要里不出现密钥值(tmp_path: Path) -> None:
    """摘要会进模型上下文，与检索片段同一层脱敏。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "配置是 SPOOLKIT_QQ_SECRET=abcdef123456 先放着")
    # 中间再补几轮，把上面那条挤出「最近」的窗口——它只能从摘要那一段进来，
    # 于是这条断言才真的盯在摘要的脱敏上。
    record_message(conn, "s", USER, "中间问一句")
    record_message(conn, "s", ASSISTANT, "中间答一句")
    record_message(conn, "s", USER, "那它呢")
    text = conversational_prompt(
        conn, "s", "那它呢", turns=1, digest_turns=3, digest_chars=400
    )
    digest = text.split(PRIOR_HEADER)[0]
    assert DIGEST_HEADER in digest
    assert "SPOOLKIT_QQ_SECRET" in digest
    assert "abcdef123456" not in text
    conn.close()


def test_标定值为零时不带摘要(tmp_path: Path) -> None:
    """`history_digest_turns=0` = 关掉摘要：这是可覆盖的标定值，不许写死。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "更早的一句话")
    record_message(conn, "s", USER, "补一轮")
    text = conversational_prompt(
        conn, "s", "补一轮", turns=1, digest_turns=0, digest_chars=400
    )
    assert DIGEST_HEADER not in text
    assert "补一轮" in text
    conn.close()


def test_指代词命中才加那一行提醒(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "先看 python 那个脚本")
    record_message(conn, "s", USER, "继续刚才那件事")
    text = conversational_prompt(conn, "s", "继续刚才那件事", turns=1)
    # 钉「加了那一行、且只有那一行」：字面措辞由模板自己的用例守。
    assert text.startswith(DEICTIC_REMINDER)
    assert text.count(DEICTIC_REMINDER) == 1
    conn.close()


def test_普通消息不带提醒(tmp_path: Path) -> None:
    """只有命中时才加：对所有消息都加，就是每一轮都付一笔没用的税。"""
    conn = _conn(tmp_path)
    record_message(conn, "s", USER, "给 python 脚本加参数")
    record_message(conn, "s", USER, "读一下 config.py")
    text = conversational_prompt(conn, "s", "读一下 config.py", turns=1)
    assert DEICTIC_REMINDER not in text
    conn.close()


def test_没有上一轮时也要提醒指代(tmp_path: Path) -> None:
    """没有历史可看时更要提醒——「它」指什么，模型这一步并不知道。"""
    conn = _conn(tmp_path)
    text = conversational_prompt(conn, "s", "还是按之前那样做")
    assert text.startswith(DEICTIC_REMINDER)
    assert "还是按之前那样做" in text
    conn.close()

