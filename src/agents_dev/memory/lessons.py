"""教训的主动推送与效果验证。

教训机制分三级台阶，前两级（记下失败、提炼成规则）早就做了，缺的是第三级：
**在进入对应场景时把它推到模型面前**，而不是等检索偶然命中。

光推送还不够，还得能淘汰。小模型总结出的教训可能是错的，而一条错误的
教训会持续误导，比不学习更糟。所以每条教训都要能被验证：

推送 → 记录这次任务成没成 → 按实际成功率调整置信度 → 长期无效的降级。

**这是相关性，不是因果性。** 任务成功不代表是这条教训的功劳。但它是
目前可观测的唯一信号，而且方向是安全的：真正有害的教训会在多次任务中
持续伴随失败，置信度自然下跌；偶尔一次失败不会，因为有最小样本量挡着。

与设计文档的一处偏离：文档把「教训」画在热记忆文件里，这里改成存进
记忆库。原因是教训需要场景触发词、置信度和统计，这些是数据库形状的，
塞进 Markdown 文件就再也拿不出来了。热记忆文件继续承载事实、偏好、
决策、进行中这些人类可读的内容。
"""

import sqlite3
import time

from agents_dev.memory.store import Memory, to_memory

KIND = "lesson"

# 初始置信度。刚好在推送线之上——新教训先给一次机会，让实际效果说话。
INITIAL_CONFIDENCE = 0.5

# 低于这条线就不再推送，只可能被检索命中。
PUSH_THRESHOLD = 0.45

# 低于这条线且样本足够，就从推送池里清出去。
PRUNE_THRESHOLD = 0.35

# 样本少于这个数就不调整置信度：一两次成败说明不了什么，
# 过早调整只会让置信度来回抖，反而不可信。
MIN_SAMPLES = 3

MAX_PUSHED = 3


def recompute_confidence(applied: int, success: int) -> float:
    """按实际效果算置信度。样本不足时保持初始值。"""
    if applied < MIN_SAMPLES:
        return INITIAL_CONFIDENCE
    rate = success / applied
    return round(0.2 + 0.6 * rate, 2)


def record_lesson(
    conn: sqlite3.Connection,
    rule: str,
    trigger: str,
    source: str = "",
    confidence: float = INITIAL_CONFIDENCE,
) -> Memory:
    """写入一条教训。trigger 是「什么时候该用它」的关键词。"""
    cursor = conn.execute(
        "INSERT INTO memory"
        "(kind, text, trigger, source, confidence, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (KIND, rule.strip(), trigger.strip(), source, confidence, time.time()),
    )
    conn.commit()
    return Memory(
        id=cursor.lastrowid,
        kind=KIND,
        text=rule.strip(),
        source=source,
        confidence=confidence,
        trigger=trigger.strip(),
    )


def _keywords(trigger: str) -> list[str]:
    parts = trigger.replace("，", ",").replace("、", ",").split(",")
    return [part.strip().lower() for part in parts if part.strip()]


def match_lessons(
    conn: sqlite3.Connection, context: str, limit: int = MAX_PUSHED
) -> list[Memory]:
    """挑出与当前场景相关的教训，按置信度排序。

    匹配是关键词包含：触发词出现在任务描述里就算命中。规则很土，
    但它是可解释的——推送了不该推的教训，你能一眼看出是哪个词匹配上的。
    """
    if not context.strip():
        return []

    lowered = context.lower()
    rows = conn.execute(
        "SELECT * FROM memory WHERE kind = ? AND confidence >= ?"
        " ORDER BY confidence DESC, id DESC",
        (KIND, PUSH_THRESHOLD),
    ).fetchall()

    hits: list[Memory] = []
    for row in rows:
        words = _keywords(row["trigger"])
        if words and any(word in lowered for word in words):
            hits.append(to_memory(row))
        if len(hits) >= limit:
            break
    return hits


def record_outcome(
    conn: sqlite3.Connection, lesson_ids: list[int], succeeded: bool
) -> int:
    """记录这批被推送的教训这次表现如何。

    推送即计入 applied；任务完成才计入 success。置信度按累计成功率重算。
    """
    if not lesson_ids:
        return 0

    updated = 0
    for lesson_id in lesson_ids:
        row = conn.execute(
            "SELECT applied_count, success_count FROM memory WHERE id = ?",
            (lesson_id,),
        ).fetchone()
        if row is None:
            continue
        applied = row["applied_count"] + 1
        success = row["success_count"] + (1 if succeeded else 0)
        conn.execute(
            "UPDATE memory SET applied_count = ?, success_count = ?,"
            " confidence = ?, last_used_at = ? WHERE id = ?",
            (
                applied,
                success,
                recompute_confidence(applied, success),
                time.time(),
                lesson_id,
            ),
        )
        updated += 1
    conn.commit()
    return updated


def prune_lessons(conn: sqlite3.Connection) -> list[int]:
    """把样本足够但效果很差的教训清出推送池，返回被清理的 id。

    不是删除——它仍然留在库里，检索时还可能被翻出来。直接删掉会丢掉
    「我们试过这条路」这个信息本身。
    """
    rows = conn.execute(
        "SELECT id FROM memory WHERE kind = ? AND applied_count >= ?"
        " AND confidence < ?",
        (KIND, MIN_SAMPLES, PRUNE_THRESHOLD),
    ).fetchall()
    ids = [row["id"] for row in rows]
    for lesson_id in ids:
        conn.execute(
            "UPDATE memory SET confidence = ? WHERE id = ?",
            (PRUNE_THRESHOLD - 0.01, lesson_id),
        )
    conn.commit()
    return ids
