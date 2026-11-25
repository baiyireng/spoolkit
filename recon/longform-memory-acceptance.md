# L6.3 长文记忆验收

日期：2026-10-24  
分支：`feat/longform-memory`  
验收前 HEAD：`1fe6a60` `feat(web): resume longform checkpoints from task panel`  
范围：验收和发布准备。本轮没有加功能，没有进入 L7，没有 merge，没有 tag，没有 push，没有改 `s10-drop-orchestrate`。

## 验收方式

端到端走生产 `AgentLoop`、`LongFormRunner`、`FileSink`、checkpoint 存储和网页读接口。模型应答由脚本网关按写作提示返回固定节正文。没有启动 `llama-server`。

脚本不入库：`D:\workSpace\agents_dev\.tmp\l63_accept.py`  
产物目录：`D:\workSpace\agents_dev\.tmp\l63-accept`  
结果：37 项检查通过，0 项失败。

Windows 上 pytest 需要先建 `PYTEST_DEBUG_TEMPROOT`。`tests/agent` 与 `tests/tools` 用的是 `D:/workSpace/agents_dev/.tmp/ptroot-l63-agent`。`scripts/web_ci.py` 自己把临时根设到工作树 `.tmp/ptroot-cursor`。

L0.14 的真人模型数字来自 `recon/continuation-spike-12.md`，本轮没有重跑。

## 场景

| 场景 | 结果 |
| --- | --- |
| chat 长文 | 完成。全文 3160 字，三节标题各出现 1 次。artifact `doc-368a6ca3a545ccb8`。会话摘要记了这一份。下一轮第一条提示含 `【会话记忆】` 和标题「窗口」。检索「估算」命中同一份。 |
| file 长文 | 完成。最终回复是 `已生成文档，待确认：docs/out.md`。pending 1 条，路径 `docs/out.md`。目标文件不存在。artifact 1 份。 |
| 中途停止 | `LongFormRunner.stop_after=1` 之后，checkpoint 文件还在，状态 `paused`，已完成节只有第 0 节。 |
| 新 turn resume | `AgentLoop.run(..., resume_checkpoint=key)`。恢复请求里没有「不是控制层」，也没有把「窗口」再当未开始节。完成后全文仍是三节标题各 1 次，checkpoint 文件删除。 |
| 下一轮找回上一份 | artifact 列表、会话摘要、检索和下一轮提示都能指到上一份。提示和检索片段里没有正文标记 `BODY-LEAK-MARKER`。 |

chat 成功之后，对同一工作区发了 GET `/artifacts`、`/session/summary`、`/checkpoints`，三个都是 200。checkpoint 列表是空的，因为这份已经写完。暂停中的 checkpoint 响应形状本轮没有再打一次 GET；那条约束在已提交的读模型测试里。

`both` 交付在 `longform_bridge._delivery` 里写成按 chat 处理、不带路径。本轮没有单开这个场景。

## 指标

计数来自脚本里的 `OfflineTokenCounter`，是各次请求的提示和完成 token 之和。循环日志里有「拿不到 prompt_tokens，按消息长度估算」。这不是 llama-server 的用量。耗时是脚本墙钟，不含模型。

| 场景 | 完成 | 字数 | 请求 | prompt tokens | completion tokens | 秒 | 12 字重复率 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| chat | 是 | 3160 | 6 | 2508 | 3119 | 0.089 | 0.9904 |
| file | 是 | pending 全文 | 6 | 2508 | 3130 | 0.082 | 0.9904 |
| resume（停止加恢复合计） | 是 | 3160 | 5 | 1537 | 3090 | 0.103 | 0.9790 |

12 字重复率高，是因为脚本把「窗口写完。」这类句子重复到约 1000 字来凑节长。三节标题各出现 1 次，恢复时没有重写第一节。这个比率不能拿来和真人模型的重复率比。

L0.14 历史记录（本轮未重测）：技术说明、项目复盘、方案对比真完成 9/9。12 字重叠 0.009、0.000、0.000。105 次请求，40089 token，643.7 秒。接受稿里没有 `<think>`，也没有行首脚手架。

下一轮短答探针让控制层打出「ControlTurn v2 连续收到旧格式，降级到旧协议」。那是探针返回的短 JSON 形状引起的。第一条提示里已经有 `【会话记忆】`。

## 通过的链路

1. 长模式计划进入 `LongFormRunner`，按大纲写完三节。
2. chat 正文进 artifact 旁路文件；元数据是标题、摘要和节摘要。会话摘要记下 artifact。下一轮上下文带 `【会话记忆】`。检索能按标题命中。
3. file 只产生一条 pending，不写目标文件。最终回复和 TaskEvent 是待确认短句。
4. 第一节之后停止会留下 checkpoint。新 turn 用 `resume_checkpoint` 只补剩余节。成功后删除 checkpoint，并另存 artifact。
5. GET `/artifacts`、`/session/summary`、`/checkpoints` 能读到上一份文档的元数据。完成之后 checkpoint 列表为空。

## 质量与安全

脚本节首放了 `API_KEY=sk-test-secret-value`。正文第 400 字之后放了 `BODY-LEAK-MARKER`。

本轮这些表面没有正文标记，也没有密钥原文：

- 加载后的会话摘要，以及摘要 JSON 文件
- TaskEvent
- 下一轮提示
- artifact 检索片段
- GET `/artifacts`、`/session/summary`、`/checkpoints` 的响应

完成稿和上述读模型里没有 `<think>`，也没有行首脚手架标签。

磁盘上的 artifact 元数据 JSON 仍含密钥原文。三节的 `sections[].summary` 各 240 字，节首密钥落在这段摘要里。`BODY-LEAK-MARKER` 不在元数据里，只在旁路正文文件。元数据 JSON 1322 字符，正文文件 9178 字节。读接口会打码，直接读 `.agent/artifacts` 仍能看到密钥。

file 的 pending 正文里，密钥按三节各出现一次。最终回复不含正文标记。

暂停时的 checkpoint 文件要保存节正文才能恢复。本轮在成功删除之前确认过文件存在，没有把暂停文件的 HTTP 再打一遍。

## 界面

Chrome headless，父仓库 `recon/cdp.py`，端口 9365。任务面板里有一条暂停 checkpoint 和「继续」按钮。关掉 Chrome 时 HTTP 线程打出 `WinError 10054`，是连接被断开，页面测量已经拿到。

| 项 | 390×844 | 1440×900 |
| --- | --- | --- |
| 页面 / 面板 / 列表横向溢出 | 0 / 0 / 0 | 0 / 0 / 0 |
| 列表高度 / max-height | 114 / 168px | 114 / 168px |
| 路径 client / scroll / 省略 | 366 / 839 / ellipsis | 375 / 839 / ellipsis |
| 标题行数钳制 | 2 | 2 |
| 按钮在面板内 | 是 | 是 |
| 面板 | 左 0，顶 186，宽 390，高 658 | 左 1040，顶 0，宽 400，高 900 |
| 按钮顶 / 命中元素 | 680 / `checkpoint-resume` | 480 / `checkpoint-resume` |
| 输入框顶 / 高，发送按钮高 | 428 / 33，44 | 467 / 33，36 |
| 按钮矩形与输入框相交 | 否 | 是 |
| 列表出现 checkpoint 正文 | 否 | 否 |

1440 时任务面板是右侧通高 400px。按钮落在输入框同一高度，命中点上的元素是按钮。390 时面板在输入框上方，两者不相交。

## 全量测试

### pytest tests/agent tests/tools

命令带 `PYTHONPATH=src;scripts`、`PYTEST_DEBUG_TEMPROOT`、`-p no:cacheprovider -p pytest_sandbox_compat`。

第一次，147.36 秒，退出码 1：

- 1130 passed
- 2 skipped
- 1 failed：`tests/agent/test_signals.py::test_forget_kind不接受note_kind`
- 失败原文：`AssertionError: mypy 没装，这条类型断言没跑`

跳过：

- `tests/agent/test_task_contract.py:151`：`llama.cpp /tokenize` 连接被拒绝，`WinError 10061`。本轮按要求没有启动 llama-server。
- `tests/tools/test_search.py:168`：本机没有读不了的目录，测试自己跳过。

第二次用同一命令补看跳过原因，142.03 秒，退出码 1：2 failed，1129 passed，2 skipped。新增的失败是 `tests/agent/test_observe_projection.py::test_模型看见卡和摘要看不见整份raw`。这次用了 `--tb=no`，没有留下断言文本。单独再跑这一条，0.49 秒，通过。

`rg` 和 `node` 都在 PATH 上。缺的是 `mypy`，以及工作树里的 `recon/cdp.py`。

### python scripts/web_ci.py

第一次在 `npm install` 之前退出码 1。构建、资源检查和 `node --test` 已通过，停在「还没有 web/node_modules」。随后在 `web/` 执行了 `npm install`（3 个包，本地依赖，不入库），再跑一次。

第二次 146.01 秒，退出码 1。进入 pytest 之前这些步骤退出码都是 0：

- `scripts/build_web_assets.py`：css `app.e0ce79a36b5c.css`，js `app.a6b7de3110dc.js`
- `scripts/check_web_assets.py`：web assets ok
- `node --test tests/web/js/pure.test.mjs`：10 pass，0 fail，68.4374 ms
- `web/build.mjs`：esbuild 解析了 dist
- `tsc -p web/tsconfig.json`
- `git diff --exit-code` 覆盖 dist、`index.html`、`manifest.json`

pytest：5 failed，460 passed，1 skipped。五条都是 `ModuleNotFoundError: No module named 'cdp'`。测试把 `recon` 插进 `sys.path` 后导入 `cdp`，这个工作树的 `recon/` 没有 `cdp.py`。父仓库 `D:\workSpace\agents_dev\recon\cdp.py` 存在。本轮没有把该文件复制进工作树。

失败用例：

- `tests/web/test_assets.py::test_三档宽度下浮层不压输入条`
- `tests/web/test_assets.py::test_390任务卡单列且不溢出`
- `tests/web/test_browser_keyboard.py::test_真实Chrome键盘链路与抽屉动画命中`
- `tests/web/test_task_card_browser.py::test_真实Chrome渲染任务卡并定位审批`
- `tests/web/test_task_card_browser.py::test_深浅色主题在宽屏和窄屏`

`node --test tests/web/js/pure.test.mjs` 在 web_ci 里再跑了一次，10 通过。

## 未解决风险

- `mypy` 未安装，agent 套件有一条类型断言没跑，命令退出码是 1。
- 工作树缺少 `recon/cdp.py`，五条真实浏览器测试进不了页面。本轮自己的 390/1440 测量用的是父仓库那份 `cdp.py`。
- `test_模型看见卡和摘要看不见整份raw` 在整包里失败过一次，单跑通过。没有断言文本，不能判断原因。
- artifact 节摘要以原文落盘。读模型和下一轮提示会打码，直接读 `.agent/artifacts/*.json` 仍能看到放在节首的密钥。
- `both` 目前按 chat、无路径处理。本轮没有单独验收。
- 1440 右侧任务面板通高，盖住输入框右端，「继续」按钮和输入框在同一高度。390 不相交，两边都没有横向溢出。
- 脚本重复率不能代表模型重复率。L0.14 的 9/9 和 0.000–0.009 重叠没有在本轮复测。
- 暂停中的 checkpoint HTTP 本轮没有再采样。成功之后的列表是空的。

## 是否可合并

先不合入 `s10-drop-orchestrate`。

长文这条链路的脚本验收是 37/37。两条要求的全量命令退出码都是 1，原因是环境：没装 `mypy`，工作树没有 `cdp.py`。在这两条命令变绿、以及发布决定下来之前，不合并，不打 tag，不推送。
