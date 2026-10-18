"""续写拼接和重复检测。

重叠按结尾与开头的最长公共段切掉。12 字片段有一半以上已在前文出现，
就视为整段在复述，调用方应重写而不是再接上去。
"""

OVERLAP_CHARS = 200
NGRAM_SIZE = 12
REPEAT_RATIO = 0.5
REPEAT_WINDOW = 400
REWRITE_FACTOR = 1.5


def merge_continuation(head: str, tail: str, overlap: int = OVERLAP_CHARS) -> str:
    """把续写接在已写正文后面，去掉尾部已经出现过的开头。"""
    if not tail:
        return head
    window = overlap if overlap > 0 else 0
    skip = _suffix_prefix_length(head[-window:], tail[: window * 2])
    return head + tail[skip:]


def looks_repeated(head: str, tail: str) -> bool:
    """续写开头的 12 字片段，有一半以上能在已写结尾里找到。"""
    if len(tail) < NGRAM_SIZE or len(head) < NGRAM_SIZE:
        return False
    return (
        _ngram_ratio(head[-REPEAT_WINDOW:], tail[:REPEAT_WINDOW], NGRAM_SIZE)
        > REPEAT_RATIO
    )


def _suffix_prefix_length(window: str, probe: str) -> int:
    """window 的最长后缀，同时又是 probe 的前缀。返回这段的长度。"""
    limit = min(len(window), len(probe))
    for size in range(limit, 0, -1):
        if window[-size:] == probe[:size]:
            return size
    return 0


def _ngram_ratio(left: str, right: str, size: int) -> float:
    right_grams = _ngrams(right, size)
    if not right_grams:
        return 0.0
    left_grams = set(_ngrams(left, size))
    shared = sum(1 for gram in right_grams if gram in left_grams)
    return shared / len(right_grams)


def _ngrams(text: str, size: int) -> list[str]:
    if len(text) < size:
        return []
    return [text[index : index + size] for index in range(len(text) - size + 1)]
