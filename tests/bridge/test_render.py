"""Markdown → 聊天软件真正渲染得出来的样子。

实测（用户在手机 QQ 上看我们的机器人回复）：

- QQ 移动端**渲染一部分 Markdown**：标题、列表、代码块、链接都有格式效果；
- 只有 `**加粗**` 这类**强调标记**原样显示成星号；
- 什么都不渲染的通道（企业微信、不带 parse_mode 的 Telegram）才需要折成纯文本。

所以有两个层次：`fix_markers`（只去强调标记）与 `to_plain`（折成纯文本并按窄屏排版）。

两个模式共有一条硬约束：**只动标记、不改内容**——代码块里的东西、`sum_to` 这类
标识符、`a * b` 的乘法、`parts[0](args)` 的代码，一个字都别动。
"""

from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.render import fix_markers, to_plain


# --- fix_markers：QQ 手机端走这条 ---------------------------------------------


def test_fix_只去强调标记() -> None:
    assert fix_markers("**改完了**") == "改完了"
    assert fix_markers("*斜的*") == "斜的"
    assert fix_markers("~~删掉~~") == "删掉"
    assert fix_markers("先 **重点** 再 *次重点*") == "先 重点 再 次重点"


def test_fix_把标题列表围栏链接原样留着() -> None:
    """这些手机上本来就有格式效果，折掉就等于把排版删了（早先就是这么错的）。"""
    text = "## 改完了\n\n- 第一条\n- 第二条\n\n```python\nx = 1\n```\n\n见 [文档](https://x.dev/a)"
    assert fix_markers(text) == text


def test_fix_代码块里的标记不动() -> None:
    assert fix_markers("```\nx = **2**\n```") == "```\nx = **2**\n```"


def test_fix_不碰标识符与乘法() -> None:
    for name in ("sum_to", "a_b_c", "__init__", "_private"):
        assert fix_markers(name) == name
    assert fix_markers("面积 = a * b") == "面积 = a * b"
    assert fix_markers("parts[0](args)") == "parts[0](args)"


def test_fix_双下划线粗体刻意不处理() -> None:
    assert fix_markers("__改完了__") == "__改完了__"
    assert fix_markers("打开 __init__.py") == "打开 __init__.py"


def test_fix_空文本与纯文本原样() -> None:
    assert fix_markers("") == ""
    assert fix_markers("就是把这句话说出来") == "就是把这句话说出来"


# --- to_plain：什么都不渲染的通道 ----------------------------------------------


def test_plain_标记全去() -> None:
    assert to_plain("**粗**") == "粗"
    assert to_plain("### 步骤一") == "步骤一"
    assert to_plain("> 引用") == "引用"
    assert to_plain("跑 `pytest -q` 就行") == "跑 pytest -q 就行"
    assert to_plain("见 [文档](https://x.dev/a)") == "见 文档（https://x.dev/a）"


def test_plain_代码围栏加看得见的边界() -> None:
    """手机上等宽对齐没了，得让人分得清代码从哪开始、到哪结束。"""
    assert to_plain("```python\nx = 1\n```") == "┌─ python\nx = 1\n└─"


def test_plain_表格逐行() -> None:
    text = "| 文件 | 改动 |\n|---|---|\n| calc.py | 修 sum_to |\n| stats.py | 补空输入 |"
    assert to_plain(text) == "· calc.py：修 sum_to\n· stats.py：补空输入"


def test_plain_列表换成圆点() -> None:
    assert to_plain("- 第一条\n- 第二条") == "· 第一条\n· 第二条"
    assert to_plain("  - 缩进的") == "  · 缩进的"


def test_plain_长段落按标点折行() -> None:
    long = "第一句话写在这里。第二句话也写在这里，而且它比较长一点点。第三句话收尾。"
    folded = to_plain(long, wrap=20).splitlines()
    assert len(folded) > 1
    assert "".join(folded) == long  # 折行不改字
    assert all(len(line) <= 22 for line in folded)


def test_plain_不折行时保持原样() -> None:
    long = "第一句话写在这里。第二句话也写在这里。"
    assert to_plain(long) == long


def test_plain_空行收敛() -> None:
    assert to_plain("上面\n\n\n\n下面") == "上面\n\n下面"


def test_plain_分隔线整行不留() -> None:
    assert to_plain("上面\n---\n下面") == "上面\n下面"


# --- 不该动的内容（两个模式都要守）---------------------------------------------


def test_取下标再调用不是链接() -> None:
    """`parts[0](args)` 与 `[文字](链接)` 长得一样，真按链接渲染就把代码改了。"""
    assert to_plain("parts[0](args)") == "parts[0](args)"
    assert to_plain("d[k](v)") == "d[k](v)"


def test_中文里紧挨着的链接照旧能转() -> None:
    assert to_plain("详见[文档](https://x.dev/a)") == "详见文档（https://x.dev/a）"


def test_幂运算与解包里的星号() -> None:
    assert to_plain("2 ** 10 是 1024") == "2 ** 10 是 1024"
    assert to_plain("kwargs 用 **kw 传") == "kwargs 用 **kw 传"


def test_路径与函数调用里的括号不动() -> None:
    assert to_plain("看 app/[id]/page.tsx") == "看 app/[id]/page.tsx"
    assert to_plain("改 is_even(n) 的返回值") == "改 is_even(n) 的返回值"


def test_数字方括号不当链接() -> None:
    assert to_plain("[0](2)") == "[0](2)"
    assert to_plain("数组 [1, 2] 里") == "数组 [1, 2] 里"


def test_跨行的粗体标记不硬拼() -> None:
    assert to_plain("**没写完的\n粗体在这**") == "**没写完的\n粗体在这**"


# --- 接到桥上：按通道能力选模式 ------------------------------------------------


class _Channel:
    def __init__(self, **attrs) -> None:
        self.sent: list[str] = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def send(self, text: str, conversation: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        pass


def test_qq_通道只去强调标记() -> None:
    """QQ 真的会渲染标题列表——所以桥不该把它们折掉。"""
    from spoolkit.bridge.qqbot import QQBotChannel

    channel = _Channel()
    channel.render_mode = QQBotChannel.render_mode
    bridge = Bridge(channel, lambda text: "## 改完了\n\n- **要点**一\n- 要点二", access="open")

    bridge.handle(Incoming(user="u", text="干活", conversation="c"))

    assert channel.sent[-1] == "## 改完了\n\n- 要点一\n- 要点二"


def test_什么都不渲染的通道折成纯文本() -> None:
    channel = _Channel()  # 没声明能力 → 默认 plain（最保守）
    bridge = Bridge(channel, lambda text: "## 改完了\n\n- **要点**一", access="open")

    bridge.handle(Incoming(user="u", text="干活", conversation="c"))

    assert channel.sent[-1] == "改完了\n\n· 要点一"


def test_真渲染_markdown_的通道一个字不动() -> None:
    channel = _Channel(render_mode="markdown")
    bridge = Bridge(channel, lambda text: "**改完了**", access="open")

    bridge.handle(Incoming(user="u", text="干活", conversation="c"))

    assert channel.sent[-1] == "**改完了**"


def test_补发的那份也是渲染过的() -> None:
    """渲染发生在进 outbox **之前**，补发不会把标记又塞回去。"""

    class _Flaky(_Channel):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def send(self, text: str, conversation: str) -> None:
            if self.fail:
                raise TimeoutError("网络抖了一下")
            super().send(text, conversation)

    channel = _Flaky()
    bridge = Bridge(channel, lambda text: "**改完了**", access="open")

    bridge.handle(Incoming(user="u", text="干活", conversation="c"))
    assert channel.sent == []  # 没发出去，进了 outbox
    channel.fail = False
    assert bridge.flush_outbox() == 1
    assert channel.sent[-1] != "**改完了**"  # 渲染过的版本
