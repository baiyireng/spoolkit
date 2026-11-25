# 网页壳

浏览器打开的那一页，和测试还在读的那份内联字符串，都从 `assets/` 来。

## 模块

```
assets/index.html          浏览器首页，只链 /assets
assets/css/                样式，按原文件的段落拆开
assets/js/core/            DOM、状态、时间与 diff 摘要、请求
assets/js/render/          markdown / 代码 / diff / 失败说明 / 思考过程 / 任务卡
assets/js/features/        侧栏、会话、对话、输入、状态、通知、设置、确认
assets/js/main.js          启动，并把上面的模块接上
assets/js/test-entry.js    只进 dist/test-bundle.js
assets/dist/               已提交的产物。运行时不需要 Node
page.py                    读 INDEX 和兼容用的 HTML
static.py                  路径白名单、拼装
```

依赖只允许沿这个方向走：`core → render → features → main`。
`features` 下面的文件不互相 `import`。跨 feature 的函数调用走 `ctx`，不直接叫全局函数。
`test-entry.js` 只被测试包引用。

## 执行顺序

唯一顺序源是 `static.py` 的 `JS_ORDER`。`main.js` 不写 `import`：打包会删掉那些行，留下只会让人以为改 import 就能改运行顺序。

`scripts/check_web_assets.py` 会拒绝 `main.js` 里的 `import`，也会拒绝 feature 之间的裸函数调用。`strip_module_syntax` 只去掉整行的 `import "./x.js";` 和 `export { a, b };`。named / default import 会直接报错，不会静默留在 dist 里。

## ctx

`js/core/ctx.js` 的 `createApp(store)` 造出**唯一**的那个实例。`main.js` 的 `wireFeatures()` 按依赖顺序调用 `createX(app)`，再把返回的函数挂到实例上。feature 里的函数通过参数 `ctx` 闭包读写，不读全局表。

| feature | 工厂 | 交给别人用的面 |
| --- | --- | --- |
| toasts | `createToasts` | `showToast` `notifyInfo` `notifyFailure` `closeAllToasts` |
| status | `createStatus` | `setStatusOpen` `drawStatus` `refreshStatus` `showReadonly` `showHealth` |
| settings | `createSettings` | `updateArchiveBadge` `renderArchived` `setSettingsOpen` |
| sessions | `createSessions` | `requestRemove` `requestRestore` `newSession` `sessionTitle` `sessionRowTitle` `applySessionTitle` `maybeAgentTitle` |
| thread | `createThread` | `scrollDown` `updateToBottom` `atBottom` `add` `resetView` `drawHistory` `restoreLive` `getLive` `setPendingUser` `getLastDrawn` |
| confirm | `createConfirm` | `showPending` `clearPending` `showUndo` `ensureCard` `targetLabel` |
| sidebar | `createSidebar` | `drawTarget` `renderSidebar` `markArmed` `openDrawer` `closeDrawer` |
| composer | `createComposer` | `setRunning` `syncSend` `say` `onSendClick` |

`main` 自己的 `applyState` / `switchTarget` 也挂在这个实例上。页面状态只在 `createStore()` 那一个对象里：`get` / `set` / `patch` / `update` / `subscribe`。`patch` 一次调用只通知一次。feature 读写走 `ctx.store`。`keyFor` 在 `core/format.js`。对话卡片那一份 `live` 只从 `ctx.thread.getLive()` / `setPendingUser()` / `getLastDrawn()` 进出。

## 数据流

1. 浏览器请求 `GET /`，拿到 `index.html`。
2. 页面再请求 `GET /assets/dist/app.<hash>.css` 和 `.js`。
3. 脚本连 `GET /events`，补帧走 `GET /state`，动作走原来的 `POST`。
4. 模型输出用 `textContent` 和 DOM API 放进页面。图标仍是那几份内联 SVG（`innerHTML = ICON.*`）。

## 为什么产物是一整段脚本

源码按文件拆开，文件头的 `import` 只标谁依赖谁。打包时把这些行去掉，按固定顺序收成一段经典脚本。

页面状态在 `store` 这一个对象里，初始 key 由 `INITIAL_STATE` 列出，`STATE_KEYS` 是 `Object.keys(INITIAL_STATE)`。生产脚本不把 `store` / `app` 挂到全局，也不再为假 DOM 赋同名 `var`。假 DOM 另跑 `dist/test-bundle.js`，从 `globalThis.__spoolTest` 取 `store`、`app` 和要用的函数。`web/build.mjs` 里的 esbuild 只检查产物语法，不负责改写 dist。

`HTML`（`page.py`）是同一段生产脚本再套回 `<style>` 和 `<script>`，给 `tests/web/test_page.py` 用。假 DOM 只从这段 HTML 取元素 id，脚本跑的是 test-bundle。它不带 `<link>`，也不带 `src=`。

## 构建和测试

在仓库根：

```powershell
python scripts/build_web_assets.py
python scripts/check_web_assets.py
node --test tests/web/js/pure.test.mjs
cd web
npm install
node build.mjs
python scripts/web_ci.py
```

改了 `assets/js` 或 `assets/css` 之后要重新跑 `build_web_assets.py`，把 `assets/dist`、`index.html` 和 `manifest.json` 一起留下。`check_web_assets.py` 按字节比对 dist。这些资源已纳入 git 跟踪，`web_ci.py` 里的 `git diff --exit-code` 能看见 dist / index / manifest 漂移。`scripts/gate.py` 跑测试前会先跑 `check_web_assets.py`。

带哈希的 `dist/app.<12位>.js` / `.css` 缓存一年。其余资源是 `Cache-Control: no-cache`。

## 加一个 feature

1. 新建 `assets/js/features/<name>.js`。文件头可以写 `import "./../core/x.js";` 这种整行副作用导入，用来标依赖；不要写 named / default import。
2. 在文件末尾写 `function createName(ctx) { ... return { ... }; }`。函数体只用这个 `ctx`。
3. 调用别的 feature 写成 `ctx.<feature>.<fn>(...)`。页面状态用 `ctx.store.get` / `set` / `patch` / `update`。
4. 把文件加进 `static.py` 的 `JS_ORDER`（放在 `main.js` 前面）。不要在 `main.js` 写 import。
5. 在 `wireFeatures()` 里调用 `createName(app)`，把返回值挂到 `app` 上，并把 `createName` 登记进 `scripts/check_web_assets.py` 的 `_FEATURE_INIT`。假 DOM 要调用的函数写进 `test-entry.js` 的 `globalThis.__spoolTest`，不要赋全局 `var`。
6. 跑 `python scripts/build_web_assets.py` 和 `python scripts/check_web_assets.py`。单文件不超过 600 行。

`HTML` 在第一次被读到时才拼。服务端只引用 `INDEX`。

## 资源路由

`GET /assets/<path>` 只允许 `.html` `.css` `.js` `.svg` `.png` `.webp` `.woff2` `.json`。
`..`、绝对路径、反斜杠、空字节、符号链接一律 404。目录不列举。
