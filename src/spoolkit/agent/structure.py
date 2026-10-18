"""停笔之后的结构检查。

不要求模型写结束标记。代码围栏没配对，就再续写一次。
节正文里如果又起了下一个标题，从那里截掉。
"""


def fences_closed(text: str) -> bool:
    """成对的 ``` 才算围栏已经关上。"""
    return text.count("```") % 2 == 0


def cut_before_next_heading(text: str) -> str:
    """从第一个「换行 + ## 」处截断，后面的节不留在这一节里。"""
    marker = "\n## "
    index = text.find(marker)
    if index < 0:
        return text
    return text[:index].rstrip()
