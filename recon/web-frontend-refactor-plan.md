# spool web 前端工程化改造方案

> **实施回执（2026-10-20）**：Cursor 已落地 Phase 0/1/2/3 的**文件级拆分**：
> `page.py` 14 行、assets 静态资源、`/assets` 路由、core/render/features/main、
> `node --test`、check/build/wheel 测试。复核见
> [`web-frontend-refactor-review.md`](web-frontend-refactor-review.md)。
>
> 第三轮已把状态收进 `createStore`、feature 改成 `createX(ctx)` 工厂；
> `main.applyState / switchTarget` 不再直接写状态，`web_ci.py` **382 passed**。
> 剩余偏差：
> - 生产包仍有 `this.store` / `this.app` 测试钩子和 `main.js` 的 `var` 全局
>   函数别名（应移进 test-entry）；
> - `check_web_assets.py` 只检查 8 个状态名，store 实际约 30 个 key；
> - assets 仍未纳入 git，`web_ci` 的 `git diff` 还看不见；
> - 未接 ESLint/Prettier/GitHub Actions；`thread.js`/`status.js` 仍高于 400 行。
>
> 下一阶段只做“最后收口”，然后转 UX M1；不要继续 ESM/框架迁移。

## 0. 结论

**建议做，而且应排在 UX 改造的 M1 之前或并行做“低风险拆分”**。当前 `src/spoolkit/web/page.py` 约 3408 行 / 13.8 万字符，HTML、CSS、JS 全在一个 Python 字符串里；`server.py` 约 1314 行，`tests/web/page_dom.js` 约 1093 行。继续在这个文件上叠 Agent 体验功能，边际成本会越来越高：

- 一次改动要跨 CSS、标记、全局 JS 状态和 Python 字符串转义；
- AI/人都很难只加载“会话栏”或“确认卡片”那一块上下文；
- 测试通过 `HTML.find(...)`、`_body("...")` 从大字符串里抽函数，重构即断；
- 无法做静态资源缓存、严格 CSP、独立前端单元测试。

**推荐方案 A：静态资源 + 原生 ES Modules + 开发期 esbuild 打包 + 提交 dist 产物。**
运行时仍是纯本地静态文件，不引入框架、不引 CDN、不改变离线可用；Node 只在开发/构建期需要，用户 `uv sync` 后不需要 Node。

备选方案 B：完全不用打包器，浏览器直接加载 ES Modules + 多个 CSS 文件；工具链最轻，但测试加载与产物一致性要自己维护，CI 更容易漂。除非明确坚持“仓库里不出现 Node”，否则优先 A。

## 1. 目标与非目标

### 目标

1. **源码按职责拆分**：HTML 模板、CSS 主题/布局/组件、JS 核心/功能/渲染各自成文件。
2. **文件有硬上限**：`page.py` < 100 行；JS/CSS 单文件建议 < 400 行，硬上限 600。
3. **行为不变地迁移**：每阶段以现有 366 条测试 + 真实截图回归为准。
4. **打包不漏资源**：wheel 安装后 `spool web` 能读到 assets；不依赖源码树/CWD。
5. **AI 可维护**：模块边界、依赖方向、数据流、测试入口写进 `src/spoolkit/web/README.md`；禁止循环依赖；禁止动态 `innerHTML` 拼模型输出。
6. **保留离线/零 CDN/零框架**：所有资源随包分发，字体继续系统字体，图标继续内联 SVG（或拆成 assets 的 svg）。

### 非目标

- 不借这次改造换框架（React/Vue/Svelte 都不需要）。
- 不改 Python 服务端 API 语义。
- 不在同一批里重做视觉设计；先搬文件、再改体验。
- 不做 SSR、不做多页应用、不引外部 CDN。

## 2. 目标目录

```text
src/spoolkit/web/
  page.py                     # ~50 行：加载 index.html；兼容导出 HTML
  server.py                   # 增加 /assets/<path> 静态路由
  assets/
    index.html                # 只有结构，无内联 <style>/<script>
    manifest.json             # 构建产物哈希（可选）
    css/
      tokens.css              # 主题变量
      base.css                # reset/body/滚动条/字体
      layout.css              # top/body/sidebar/thread
      sidebar.css             # 工作区/会话/设置入口
      thread.css              # 气泡/代码/思考过程/失败块
      composer.css            # 输入条/发送/浮钮
      overlays.css            # corner/toasts/状态面板/断线
      settings.css            # 设置弹层/归档列表
      responsive.css          # 860px/安全区/reduced-motion
    js/
      main.js                 # 唯一入口：装配 store/transport/ui
      core/
        dom.js                # h(), text(), on(), focus helpers
        transport.js          # getState/post/SSE 封装
        store.js              # 页面状态机（lastState/running/chosen/pending…）
        format.js             # 时间、截断、diff 摘要、redact
      render/
        markdown.js           # 标题/列表/粗体/链接/表格（纯函数）
        code.js               # 代码块/复制
        diff.js               # diff 卡片
        failure.js            # 失败说明
      features/
        sidebar.js            # 工作区/会话行/当前项
        sessions.js           # 新建/移除/恢复/标题请求
        thread.js             # 历史/实时卡片/思考过程
        composer.js           # 输入/发送/停止/草稿
        status.js             # 探活/模式/目标/详情面板
        toasts.js             # 通知栈
        settings.js           # 设置弹层/归档
        confirm.js            # diff/命令授权卡片
      test-entry.js           # 仅测试：导出可测函数，不随生产包发布
    dist/                     # 构建产物（提交仓库；不手改）
      app.<hash>.js
      app.<hash>.css
```

### 依赖方向（硬规则）

```text
core/*       不依赖任何 feature / render
render/*     只依赖 core/dom.js；不依赖 feature
features/*   依赖 core + render；不互相 import
main.js      依赖 core + features；负责 wiring
```

- 状态只在 `store.js`；feature 通过 `ctx.store.get()/set()` 或订阅更新，不各自维护全局。
- feature 之间用 store 事件/回调通信，不直接 import 对方。
- `render/markdown.js` 等纯函数可直接 Node 单测，不需要 DOM。
- 任何模型输出都用 `textContent`/DOM API，禁止动态 `innerHTML`。

## 3. 服务端与打包

### 3.1 静态资源路由

`server.py` 增加：

```python
GET /                -> assets/index.html
GET /assets/<path>   -> package 内 assets/<path>
```

要求：

- 只接受 `assets/` 下白名单扩展名（`.html/.css/.js/.svg/.png/.webp/.woff2`）；
- 规范化路径，拒绝 `..`、绝对路径、符号链接逃逸；
- 正确 `Content-Type`；`ETag`/`Last-Modified`；开发 `Cache-Control:no-cache`，生产可用哈希文件名长缓存；
- 404 不泄露目录内容；
- 资源读取用 `importlib.resources`，不依赖 CWD 或源码树。

### 3.2 打包

- `web/package.json` + `pnpm-lock.yaml`，devDependencies 只有 `esbuild`（后续可选 `typescript`/`eslint`/`prettier`）。
- 构建：
  - `js/main.js` → `assets/dist/app.<hash>.js`（IIFE 或 ESM 均可；ESM 更贴近源码）；
  - `css/*.css` 按顺序拼接/压缩 → `assets/dist/app.<hash>.css`；
  - `index.html` 引用 manifest 中的哈希文件；
  - `test-entry.js` → `assets/dist/test-bundle.js`（未压缩，给 `page_dom.js`）。
- **dist 提交到仓库**：这样 `uv sync` / `spool web` 不需要 Node；CI 校验 `pnpm build` 后 `git diff --exit-code`，防止源码改了没重建。
- Hatchling 打包：显式把 `src/spoolkit/web/assets/**` 加入 wheel；新增测试：
  - 构建 wheel；
  - 解包检查 assets 存在；
  - 从 wheel 安装后的路径 `spool web` 能返回 `/` 与 `/assets/app.<hash>.js`。

### 3.3 安全与 CSP

拆出资源后可以加：

```http
Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'
```

注意：当前 JS 大量设置 `element.style.*`，CSP 的 `style-src` 是否阻塞 CSSOM 写法取决于浏览器/指令；上线前用真实浏览器验证。至少去掉内联 `<style>`/`<script>` 后，XSS 面会明显缩小。

## 4. 测试策略

### 4.1 行为测试

`tests/web/page_dom.js` 不直接读内联脚本，改为：

1. 读取 `assets/index.html` 与 `assets/dist/test-bundle.js`；
2. 在假 DOM 上下文中执行 test bundle；
3. 通过 `window.__spoolTest` / `ctx.__spool` 调用 `applyState`、`renderSidebar`、`showToast` 等；
4. 断言仍保持现有 366 条行为用例的覆盖。

`test-entry.js` 只导出测试需要的函数；不进入生产 bundle。

### 4.2 纯函数单测

- `node --test` 直接测 `render/markdown.js`、`format.js`、`diff.js`、`markdown` 的安全转义；
- 覆盖标题/列表/表格、长命令、diff 行统计、时间格式化、redact。

### 4.3 Python 侧测试

- `/assets` 路由：200/MIME/404/路径穿越拒绝/缓存头；
- wheel 包含 assets；
- `page.py` 兼容导出 `HTML`（迁移期）；
- 旧 `test_page.py` 的文本断言逐步迁移为“模块契约 + DOM 行为”，不再依赖大字符串切片。

### 4.4 视觉回归

保留现有无头 Edge 截图脚本作为人工验收：空态、对话、状态面板、通知、待确认、命令授权、断线、抽屉、设置、长路径、多行输入；每阶段对比关键截图。

## 5. 迁移阶段（建议）

### Phase 0：低风险拆分（1–2 天）

- 抽出 `assets/index.html`、`assets/css/*.css`、`assets/js/app.js`（先不拆模块，只搬文件）；
- `page.py` 只做 `HTML = load_index()` 兼容导出；
- `server.py` 增加 `/assets` 路由；
- 更新 `page_dom.js` 读取外部 JS；
- 验收：现有 366 条测试 + 三张关键截图不变。

> 这一步已经能显著降低 `page.py` 的体积和转义成本，但不产生行为变化，风险最低。

### Phase 1：JS 核心/功能拆分（3–5 天）

- 先抽 `core/dom.js`、`core/transport.js`、`core/format.js`、`core/store.js`；
- 再按 feature 抽 `sidebar`、`thread`、`composer`、`status`、`toasts`、`settings`、`confirm`；
- 引入 `test-entry.js` 与 `page_dom.js` 适配层；
- 引入 esbuild 构建、dist 提交与 wheel 资源测试；
- 验收：行为测试全绿；`page.py` < 100 行；单文件行数预算开始生效。

### Phase 2：渲染与状态硬化（3–4 天）

- `render/markdown.js`、`code.js`、`diff.js`、`failure.js` 独立；
- 状态 store 收敛，去掉散落全局变量；
- 加纯函数单测、标题/归档/草稿/失败恢复的专项行为测试；
- 加 `scripts/check_web_assets.py`：文件行数、循环依赖、禁止 CDN/动态 `innerHTML`；
- 验收：Markdown/代码/diff 有独立测试，UI 回归不变。

### Phase 3：工程化收口（2–3 天）

- `src/spoolkit/web/README.md`：模块图、数据流、依赖规则、构建/测试命令、如何加一个 feature；
- CI：`pnpm build` + `git diff --exit-code` + pytest + Node 纯函数测试；
- wheel 构建/安装验收；
- 可选：`tsc --allowJs --checkJs`（JSDoc 渐进类型检查），不强制改成 TypeScript；
- 可选：ESLint/Prettier；
- 验收：新功能只改 < 3 个文件；无文件超 600 行；`page.py` 只是加载器。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 测试依赖 `HTML` 大字符串切片 | Phase 0 先保持 `HTML` 导出；行为测试迁到 test bundle；不要在同一批改断言和改代码 |
| wheel 漏 assets | 显式 package data + wheel 安装测试；CI 解包检查 |
| 静态路由路径穿越 | 白名单扩展名 + `PurePosixPath` 归一 + 拒绝 `..`/绝对路径/符号链接；只从 package root 读 |
| 构建产物与源码漂移 | dist 提交 + CI `pnpm build && git diff --exit-code` |
| 拆模块造成循环依赖 | `core -> render -> features -> main` 单向依赖；`check_web_assets.py` 做 import 图检查 |
| 构建工具链增加维护成本 | Node 只做 devDependency；生产 wheel 已含 dist；不要求用户安装 Node |
| 缓存旧资源 | 哈希文件名 + `manifest.json`；HTML 只引哈希产物 |
| 迁移期间行为回归 | 每阶段跑 366 条测试；关键截图人工对比；一次只搬一类文件 |

## 7. 工作量估算

- Phase 0：1–2 天（最值得先做）
- Phase 1：3–5 天
- Phase 2：3–4 天
- Phase 3：2–3 天
- 总计约 **9–14 个工作日**，可以按阶段独立验收；不建议一次性大重写。

## 8. 与 UX 改造的关系

建议顺序：

```text
Phase 0（拆 HTML/CSS/JS 外壳，不改行为）
  ↓
UX M1（浮层/断线/通知/键盘/对比度/触控）
  ↓
Phase 1–2（JS 模块化 + 渲染模块 + 纯函数测试）
  ↓
UX M2–M3（失败恢复 / Markdown / 授权 / 会话组织）
  ↓
Phase 3（CI/打包/docs/类型检查收口）
```

原因：先拆出 HTML/CSS/JS 外壳，后续每个 UX 修复只改 1–2 个文件；如果反过来先堆 UX 功能，`page.py` 会继续变大，迁移成本更高。
