"""Markdown → 纯文本：QQ / Telegram / 企业微信都是纯文本发送。

模型爱写 Markdown，而这几个通道不渲染它——`**重点**` 在手机上就是两颗星号。
转换做在**发送侧**（`Bridge._send` → `render.to_plain`），模型的输出习惯一个字不改。

这一组用例盯两件事：**该删的标记删掉**，以及**不该动的内容一个字都不许动**
（代码块里的东西、`sum_to` 里的下划线、`a * b` 里的星号）。
"""

from pathlib import Path

from spoolkit.bridge.channel import Incoming
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.render import to_plain


# --- 该删的标记 ---------------------------------------------------------------


def test_粗体斜体删除线都去掉() -> None:
    assert to_plain("**改完了**") == "改完了"
    assert to_plain("*斜的*") == "斜的"
    assert to_plain("~~删掉~~") == "删掉"
    assert to_plain("先 **重点** 再 *次重点*") == "先 重点 再 次重点"


def test_双下划线粗体刻意不处理() -> None:
    """`__x__` 到底是粗体还是 Python 的 dunder？两边都猜不准，所以只认 `**`。

    宁可留一个没删掉的标记，也不要把 `__init__` 改成 `init`——那是在改代码。
    """
    assert to_plain("__改完了__") == "__改完了__"
    assert to_plain("打开 __init__.py") == "打开 __init__.py"


def test_标题与引用只剩内容() -> None:
    assert to_plain("### 步骤一") == "步骤一"
    assert to_plain("# 大标题") == "大标题"
    assert to_plain("> 它说这一条越界了") == "它说这一条越界了"


def test_行内代码与围栏() -> None:
    assert to_plain("跑 `pytest -q` 就行") == "跑 pytest -q 就行"
    text = "改动如下：\n```python\ndef f():\n    return 1\n```\n就这些"
    assert to_plain(text) == "改动如下：\ndef f():\n    return 1\n就这些"


def test_链接与图片() -> None:
    assert to_plain("见 [文档](https://x.dev/a)") == "见 文档（https://x.dev/a）"
    assert to_plain("![图](https://x.dev/a.png)") == "图片：图（https://x.dev/a.png）"


def test_表格去掉分隔行与两侧竖线() -> None:
    text = "| 文件 | 改动 |\n|---|---|\n| calc.py | 修 sum_to |"
    assert to_plain(text) == "文件  |  改动\ncalc.py  |  修 sum_to"


def test_分隔线整行不留() -> None:
    assert to_plain("上面\n---\n下面") == "上面\n下面"


# --- 不该动的内容（这一半更重要）-----------------------------------------------


def test_代码块里的标记一个字不动() -> None:
    text = "```\nx = **2**\nprint(`hi`)\n```"
    assert to_plain(text) == "x = **2**\nprint(`hi`)"


def test_标识符里的下划线不许当斜体() -> None:
    for name in ("sum_to", "a_b_c", "my_var_name", "__init__", "_private"):
        assert to_plain(name) == name
    assert to_plain("调用 sum_to(x) 与 a_b_c") == "调用 sum_to(x) 与 a_b_c"


def test_乘法里的星号不许当斜体() -> None:
    assert to_plain("面积 = a * b") == "面积 = a * b"
    assert to_plain("2 * 3 * 4") == "2 * 3 * 4"
    assert to_plain("glob 用 *.py 匹配") == "glob 用 *.py 匹配"


def test_单个下划线不配对时留着() -> None:
    assert to_plain("路径 D:\\a_b\\c") == "路径 D:\\a_b\\c"


def test_空文本与纯文本原样返回() -> None:
    assert to_plain("") == ""
    assert to_plain("就是把这句话说出来") == "就是把这句话说出来"


# --- 边界：这些最容易把**代码**改坏（宁可少转，也不许改错）----------------------


def test_取下标再调用不是链接() -> None:
    """`parts[0](args)` 与 `[文字](链接)` 长得一模一样。

    这是本节最危险的一条：真按链接渲染就变成 `parts0（args）`——**把代码改了**。
    所以括号里必须像那么回事（含 `/` `.` `:` `#` 之一）才当链接。
    """
    assert to_plain("parts[0](args)") == "parts[0](args)"
    assert to_plain("d[k](v)") == "d[k](v)"
    assert to_plain("表格 rows[i](row) 里取值") == "表格 rows[i](row) 里取值"


def test_中文里紧挨着的链接照旧能转() -> None:
    """别用"前面是不是词字符"来判——中文没有空格，`详见[文档](url)` 是正常写法。"""
    assert to_plain("详见[文档](https://x.dev/a)") == "详见文档（https://x.dev/a）"
    assert to_plain("见[指南](guide.md)") == "见指南（guide.md）"


def test_幂运算与解包里的星号() -> None:
    assert to_plain("2 ** 10 是 1024") == "2 ** 10 是 1024"
    assert to_plain("kwargs 用 **kw 传") == "kwargs 用 **kw 传"
    assert to_plain("a ** b ** c") == "a ** b ** c"


def test_转义过的标记只脱反斜杠() -> None:
    """`\\*` 在 Markdown 里是"这里真的就是个星号"，脱掉反斜杠、别再去当粗体。"""
    assert to_plain(r"这里的 \* 是字面量") == "这里的 * 是字面量"
    assert to_plain(r"\` 不是行内代码") == "` 不是行内代码"


def test_路径与文件名里的方括号圆括号不动() -> None:
    assert to_plain("看 app/[id]/page.tsx") == "看 app/[id]/page.tsx"
    assert to_plain("改 is_even(n) 的返回值") == "改 is_even(n) 的返回值"


def test_标题尾部多余的井号也去掉() -> None:
    assert to_plain("## 步骤一 ##") == "步骤一"


def test_数字与纯符号的方括号不当链接() -> None:
    assert to_plain("[0](2)") == "[0](2)"
    assert to_plain("数组 [1, 2] 里") == "数组 [1, 2] 里"


def test_跨行的粗体标记不硬拼() -> None:
    """逐行处理：`**` 分在两行不该被跨行吃掉（那会把两行粘成一行）。"""
    assert to_plain("**没写完的\n粗体在这**") == "**没写完的\n粗体在这**"


# --- 接到桥上：按通道能力开关 --------------------------------------------------


class _Channel:
    def __init__(self, markdown: bool | None = None) -> None:
        self.sent: list[str] = []
        if markdown is not None:
            self.markdown = markdown

    def send(self, text: str, conversation: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        pass


def test_桥默认按纯文本发送() -> None:
    """我们的三条真通道（QQ / Telegram / 企业微信）都不渲染 Markdown。"""
    channel = _Channel()  # 没写 markdown 属性 → 默认纯文本
    bridge = Bridge(channel, lambda text: "**改完了**\n- 第 1 步", access="open")

    bridge.handle(Incoming(user="u", text="干活", conversation="c"))

    assert channel.sent[-1] == "改完了\n- 第 1 步"


def test_支持_markdown_的通道原样收到() -> None:
    """留了口子：真有渲染 Markdown 的通道，在类上写 markdown = True 就关掉这一层。"""
    channel = _Channel(markdown=True)
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
    assert channel.sent[-1] == "改完了"  # 不是 "**改完了**"
