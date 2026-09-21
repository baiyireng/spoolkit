"""单文件新建任务路由：跳过拆解直接执行。

dogfood 暴露：分步改同一文件每步生成 patch 容易死循环（贪吃蛇 9 步第 2 步卡死）。
修复：autonomous 检测到「单个 xxx.html」类目标时跳过拆解，构造单步计划一次写完。
"""
from spoolkit.cli.commands.plan import _looks_like_single_file_task


def test_匹配单个文件目标():
    assert _looks_like_single_file_task("做一个网页版贪吃蛇游戏：单个 index.html（内联 CSS 和 JS）")
    assert _looks_like_single_file_task("写一个 app.py 实现快速排序")
    assert _looks_like_single_file_task("新建单个文件 game.js")


def test_不匹配多文件目标():
    assert not _looks_like_single_file_task("把 src/ 下的测试都修好")
    assert not _looks_like_single_file_task("重构 cli/ 和 web/ 两个模块")
    assert not _looks_like_single_file_task("更新文档")


def test_不匹配无文件名的目标():
    assert not _looks_like_single_file_task("把工作区里的题都做对")
    assert not _looks_like_single_file_task("优化性能")
