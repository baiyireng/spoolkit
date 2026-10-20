# 本轮变更：会话标题、归档收敛与侧栏展示

## 按你的反馈调整的前提

1. **中文输入法回车不改**：保留“Enter 发送 / Shift+Enter 换行”的既有约定。
   本轮没有动 `onKeydown`，也没有加 `isComposing` 守卫。
2. **归档会话默认不展示**：左栏不再出现“已移除（N）”那一段；只保留页脚
   「设置」上的数量，归档列表在设置弹层里主动打开才建节点。
3. **会话 id 可以是任意形状**，展示以可读标题为主、id 为次级信息；标题由 Agent
   在首轮结束后生成，失败时保留第一句目标的种子标题。

## 具体改动

### 1. 会话标题：种子 + Agent 生成 + 来源标记

- `kernel/sessions.py`
  - 新增 `TITLE_SEED` / `TITLE_AGENT` / `TITLE_MANUAL` 与 `clean_title()`。
  - `title_from_goal()` 去掉“请/帮我/麻烦…”等口令、Markdown 前缀和句尾标点，
    仍限制在 `TITLE_LIMIT`。
  - `ensure_title()` 只在没标题时写**种子标题**，返回最终标题，并写
    `title_source="seed"`。
  - 新增 `set_agent_title()`：只覆盖 seed / agent 标题；人手工命名的标题
    （`title_source="manual"`，或来源不明的老标题）**一律不覆盖**。
  - `create_session(title=...)` / `rename_session()` 写 `title_source="manual"`。
  - `Session` 增加只增字段 `title_source`。

- `web/titles.py`（新增）
  - `TitleNamer` 从网页壳的 `extra_args` / 用户配置装载模型，发一条最多 48 token
    的命名请求；失败返回空串，不抛错、不阻塞任何 UI。
  - 只在模型空闲时调用（`generate_title` 里检查 `runner.running`）。

- `web/server.py`
  - `/state` 的 `target` 增加 `session_title` / `session_title_source`；
    `sessions` 每行增加 `title_source`。
  - `POST /run` 先写种子标题，并在响应里返回 `title` / `title_source`；
    页面立刻把左栏标题换掉。
  - 新增 `POST /session/title`：让 Agent 给**当前会话**起名，写 meta 并
    `bump()` 版本号，让 SSE 补一帧 `/state`。进 `WRITE_PATHS`，只读壳 403。
  - 生成失败时保留种子标题，接口返回 `generated:false`，不报错。

- `web/page.py`
  - final 事件后调用 `maybeAgentTitle(true)`；有历史但标题为空/seed 时，首帧也会
    自动请求一次。同一条会话每页只请求一次；409（进程刚发完 final 还没完全退出）
    最多补试 3 次。
  - `/run` 返回的种子标题通过 `applySessionTitle()` 立刻落到左栏。
  - 手工标题（`title_source=manual`）不会触发二次生成。

### 2. 左栏会话区：标题优先、id 次之

- 会话行改成两行：
  - 第一行：标题（没有标题时显示“未命名会话”）；
  - 第二行：会话 id（10.5px、次级颜色），长 uuid 省略；
  - 行本身带 `title`，悬停能看到完整标题 + id。
- 顶栏从“完整路径 + 会话 id”改为“完整路径 + 会话标题”，完整 id 放 `title`
  和状态详情；底部步数也用标题。
- 当前会话即使不在 `/state.sessions` 里（例如 `cli` 还没跑过），也会补进左栏
  并高亮，不再出现“顶部写着 cli、列表里找不到它”的状态。
- 去掉了左栏里重复的“工作区”标题（`.side-head` 已有一份）。

### 3. 归档收敛到设置

- 左栏不再渲染归档行，默认看不到任何已归档会话。
- 页脚新增「设置」按钮，带归档数量角标（0 时隐藏）。
- 设置弹层：
  - 标题「设置」+ 关闭键；
  - “已归档会话”段：标题、id、恢复按钮；
  - 没有归档时显示“没有已归档的会话。”；
  - 移动端是底部 sheet，桌面居中卡片；
  - Esc、点遮罩、关闭键都能关；打开时焦点进关闭键，Tab 在弹层内循环；
  - 只读壳里恢复按钮禁用（服务端也会 403）。
- 恢复成功后走既有 `/session/restore` → `applyState`，设置里的列表与页脚数量
  即时更新。

### 4. 新建会话不再先让人起名

- 去掉 `window.prompt`：`newSession()` 直接 `POST /session/new {}`，由服务端
  自动生成 id（时间戳或任意合法 id 都行），再切过去。
- 新会话在首轮之前显示“未命名会话”；第一轮 `/run` 后先有种子标题，final 后
  Agent 标题覆盖种子。手工命名仍可走 `spool session new/rename`。

## 验证

- 新增 `tests/web/test_session_titles.py`：种子/Agent/手工标题、模型失败回退、
  `/session/title` HTTP 写入、只读 403、`TitleNamer` 清洗。
- 更新 `tests/kernel/test_sessions.py`、`tests/web/test_page.py`、
  `tests/web/test_page_dom.py`、`tests/web/page_dom.js`。
- 回归命令（用仓库自带的沙箱 tmpdir 插件）：

```powershell
$env:PYTHONPATH="D:\workSpace\agents_dev\src;D:\workSpace\agents_dev\scripts"
$env:PYTEST_ADDOPTS="-p pytest_sandbox_compat"
python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat `
  tests/web tests/kernel/test_sessions.py tests/mcp/test_server.py
```

结果：**366 passed**。

## 真实渲染

- `shots/new2-desktop.png`：桌面左栏（标题 + id 两行、当前会话高亮、设置数量）。
- `shots/new2-mobile-drawer.png`：移动端抽屉。
- `shots/new2-mobile-settings.png`：移动端设置底部 sheet（已归档会话）。
- `shots/new-desktop-settings.png`：桌面设置弹层（旧一轮两行布局前的版本，
  弹层结构相同）。
- `shots/new-desktop.png` / `new-mobile-drawer.png`：两行布局前的中间版本，仅作对比。

注意：你当前跑着的 `spool web`（8765）是在改动前启动的，页面 HTML 内嵌在
Python 进程里；**要看到这些改动，需要重启一次 `spool web`**。上面的截图来自
一个独立 demo 服务（8767/8768，工作区 `recon/demo-ws`）。
