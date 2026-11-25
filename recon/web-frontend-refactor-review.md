# spool web 前端工程化与 UX M1：实施复核

复核对象：`D:\workSpace\agents_dev` 当前工作树。
复核时间：2026-10-20。

## 当前结论

- 前端工程化主路已经走通：`page.py` 是加载器，HTML/CSS/JS 在
  `src/spoolkit/web/assets/`，`/assets` 有独立资源路由。
- 页面状态已收进 `createStore`，feature 改成 `createX(ctx)` 工厂，`main`
  通过 `store.patch/set` 写状态；`check_web_assets.py` 覆盖 store 的全部 32 个 key。
- 生产包已无 `this.store` / `this.app` / `__spoolTest`；测试暴露只在
  `test-bundle.js` 的 `__spoolTest`。
- assets 已 `git add`、未提交；`HEAD` 仍是 `667836d`。
- `python scripts/web_ci.py`：**393 passed**；Node 10；tsc/esbuild/浏览器 smoke 通过。
- UX M1 已落地：P0-1～4 与 P1-7～9、P1-11（浮层/断线/面板通知/键盘/对比度/
  触控/ARIA/浅色气泡）已修。三档宽度实测浮层与输入条重叠 0，停止键中心命中
  `#send`，面板与通知重叠 0。

## M1.5 已完成

- `tests/web/test_browser_keyboard.py` 用真实 Chrome/CDP 发 Tab/Enter/Space/Escape：
  工作区/会话行可键盘切换，无隐藏 select；抽屉 Esc 后焦点回 `#menu`。
- `#st-close` 已是面板 DOM 第一个子节点，删除了 `order:-1`；打开面板后第一次
  Tab 即到关闭键。
- 窄屏关闭态 `#sidebar` 为 `pointer-events:none` + `inert` / `aria-hidden`；
  动画进行中点击输入区命中 `#goal`，桌面侧栏不受影响。
- 仍可选：`thread.js` ~548、`status.js` ~446，后续再拆；非 M1 阻塞。

## 工程残余（不阻塞 M2）

- 自定义 bundler 只支持 side-effect import；named/default import 会抛错，
  这是可接受的约束。
- tsc 只覆盖 store/ctx/format/markdown；全量经典脚本类型检查不现实。
- assets 已暂存；提交后 `web_ci` 的 `git diff --exit-code` 才真正覆盖 dist。
- 运行时仍是经典脚本，不是原生 ESM；当前 `createStore + createX(ctx)` 已满足
  维护需求，不建议再迁移。

## 下一步

1. **M2-C2a（下一步）**：顶栏显示工作区名 + 会话标题（完整路径进 title）、
   侧栏搜索、临时工作区默认隐藏但可搜索找回。
2. M2-C2b：重命名（需新增 `/session/rename` 写接口、只读 403、manual 保护）。
3. M2 不要顺手改标题/归档/IME 行为；IME 保持 Enter 发送 / Shift+Enter 换行。

## 验收命令

```powershell
$env:PYTHONPATH="D:\workSpace\agents_dev\src;D:\workSpace\agents_dev\scripts"
$env:PYTEST_DEBUG_TEMPROOT="D:\workSpace\agents_dev\.tmp\ptroot-review"
$env:PYTEST_ADDOPTS="-p pytest_sandbox_compat"
python scripts/web_ci.py
```

预期：393+ passed；Node 10；tsc/esbuild；生产 `dist/app.js` 无测试钩子。
