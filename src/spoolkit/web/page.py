"""内嵌的单页 HTML。

做成 Python 字符串而不是外置文件：跑起来只有一个东西要分发，也避免
打包时漏文件。代价是编辑时没有语法高亮，可以接受。

这里刻意做得很薄——不持有状态、不做业务判断，只渲染事件、发请求
（`/run`、`/confirm`、`/target`、`/session/delete|restore`、`/session/new`，
以及轮 `/state` 取横幅），
以及把快照里的待批准配对码、模型通断、当前目标、"已移除的会话"和"别处还有谁在跑"画出来。
配对和探活都不进模型上下文，也不能点「应用/拒绝」来批准或重连——那两个按钮是落盘确认。
前端出错只能靠人看出来，所以它必须小到一眼能看完。

**外观是 ChatGPT 那一挂**（左侧会话栏 + 中间对话流 + 底边输入条），
但这一层只做**布局与视觉**：所有行为判据都在服务端，页面只是把 `/state` 与
`/events` 已经有的东西摆对位置。零新依赖：不引 CDN、不引外部字体、不引图标
库、不引框架，桌面上是对话面、手机上是抽屉 + 满宽气泡，**断网也能用**
（图标是内联 SVG，字体是系统字体栈，主题色是一张 CSS 变量表）。

几处刻意的取舍（都会被上面"薄"这条约束推出来）：

- **两段式左栏**（工作区 → 它会话）背后仍然是那两条原生 `select`（隐藏，键鼠与
  读屏可用）：可见的行点一下 = 改控件 + 派发 `change`（`target()`），而 `change`
  的归宿仍然只有 `switchTarget` 一处——"切目标 = `POST /target`"没有第二条路径。
  切换失败（409/400）时 `drawTarget` 会把面板拨回当前目标，页面上不会停在
  一个假装选中的行上。**点下去先亮**（`chosen`）：回帧是一趟服务端往返，而
  "点了没反应"最容易被当成壳坏了。探活**不在**那一趟里（服务端挪到后台了，
  见 `web/probe.py`），所以现在这一步是几十毫秒的事——但那不是因为"探活慢了"，
  乐观选中本来就不该让高亮落后于点击。
- **只在数据真变了才重绘**：左栏按 spec 逐行复用（`syncGroup` + 签名），
  两条下拉同样按签名跳过（`fillSelect`），历史只**追加**没画过的那一段
  （`drawn`）。同一帧状态重复到达（事件流补帧、运行状态变化、3 秒轮询）时
  一个节点都不新建——这条有行为用例（`tests/web/page_dom.js`）钉着。
- **运行中把发送降级成禁用**：服务端只有 `/run` 与 `/confirm`，没有停止端点
  （`Runner.stop()` 没有对应接口）。所以这里**不摆一个点了就骗人的停止键**，
  而是在横幅上说明"等服务端收尾再发下一条"——本轮不做停止，也不新开端点。
- **会话的「移除」是两次点击**：第一下只把那一行变成确认态，第二下才发请求；
  服务端把它按**归档**处理（数据不动、可恢复），已移除的那几条在左栏单独一段里
  点「恢复」就回来。
- **失败要说人话**：`final` 失败与 `error` 事件都走 `showFailure`——把
  "连模型那一步失败了"翻译成「哪一步 / 哪个地址 / 可能为什么 / 下一步敲什么」，
  而不是甩一行原始异常。线索优先用服务端**只增**的 `error: {stage, url, kind}`，
  没有就按错误文本自己认；页面上的错误文本一律先过 `redact` 遮蔽密钥。
- **代码与正文分开画**：围栏（```）才是代码块（有底色、边框、内边距、自己滚，
  复制键固定在右上角），行内 `` ` `` 是带底色的等宽小片，其余是正文。
- **流式输出默认跟到底，人往上翻就不抢**：`stick` 在**改动 DOM 之前**读，
  翻上去读到一半不会被新事件弹回底部；那时右下角浮出「回到最新」。
- **"正在思考"由既有的 `wait` / `start` / `final` 驱动**，不新造协议：
  `wait` 心跳（模型生成中）亮，`final` 灭。

主题：默认深色，跟随 `prefers-color-scheme`；颜色全在 `:root` 的那张变量表里，
外加一条浅色的 `@media (prefers-color-scheme: light)` 覆盖，改一处就整体换掉。
滚动条按平台各给一套（WebKit 的 `::-webkit-scrollbar` 与标准的
`scrollbar-width` / `scrollbar-color`），只做美化不做隐藏——`display:none`
会让鼠标用户没有抓手。
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="color-scheme" content="dark light">
<title>spool</title>
<style>
  /* ══ 主题变量表：一处定义、整体可换 ══
     默认深色（ChatGPT 观感），跟随系统；浅色只覆盖这份表，选择器与结构都不动。 */
  :root {
    --bg:#212121;
    --panel:#171717;
    --panel-2:#0d0d0d;
    --line:#2f2f2f;
    --line-soft:#242424;
    --text:#ececec;
    --text-dim:#9b9b9b;
    --text-faint:#6f6f6f;
    --accent:#3b82f6;
    --accent-soft:#1e3a5f;
    --bubble-user:#2f2f2f;
    --bubble-assistant:#0d0d0d;
    --code-bg:#0b0b0b;
    --code-line:#3a3a3a;
    --code-bar:#1c1c1c;
    --inline-bg:#2b2b2b;
    --inline-line:#3d3d3d;
    --scroll-thumb:#4a4a4a;
    --scroll-thumb-hover:#6a6a6a;
    --ok:#6fb36f;
    --bad:#e07777;
    --warn-bg:#2a2410;
    --warn-line:#4a4020;
    --warn-text:#e8d38a;
    --info-bg:#10202f;
    --info-line:#1d3a54;
    --info-text:#9fd0ff;
    --ro-bg:#2a1f3d;
    --ro-line:#443066;
    --ro-text:#d0bfff;
    --danger-bg:#3d1515;
    --danger-line:#5c2020;
    --danger-text:#f0a0a0;
    --pair-bg:#3d2a00;
    --pair-line:#5c4300;
    --pair-text:#f0c674;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
    --mono:ui-monospace,SFMono-Regular,Consolas,"Cascadia Mono","Liberation Mono",monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#fff;
      --panel:#f9f9f9;
      --panel-2:#f4f4f4;
      --line:#e3e3e3;
      --line-soft:#ececec;
      --text:#0d0d0d;
      --text-dim:#5d5d5d;
      --text-faint:#8f8f8f;
      --accent:#2563eb;
      --accent-soft:#dbeafe;
      --bubble-user:#f0f0f0;
      --bubble-assistant:#fff;
      --code-bg:#f6f8fa;
      --code-line:#d8dee4;
      --code-bar:#eef1f4;
      --inline-bg:#eef1f4;
      --inline-line:#d8dee4;
      --scroll-thumb:#c9cdd3;
      --scroll-thumb-hover:#a8aeb6;
      --ok:#2f7a3f;
      --bad:#b42318;
      --warn-bg:#fff6d6;
      --warn-line:#e6d491;
      --warn-text:#6b4e00;
      --info-bg:#e8f1fb;
      --info-line:#b9d3ec;
      --info-text:#1b4a75;
      --ro-bg:#f0eaff;
      --ro-line:#d6c8f5;
      --ro-text:#4b2f8a;
      --danger-bg:#fdecec;
      --danger-line:#f3c2c2;
      --danger-text:#8f1d1d;
      --pair-bg:#fff3d6;
      --pair-line:#e6cf94;
      --pair-text:#6b4e00;
    }
  }
  * { box-sizing:border-box; }
  /* ══ 滚动条：细、跟随主题，深色浅色各一档 ══
     两套写法都要给：WebKit（Chrome / Edge / Safari）认 `::-webkit-scrollbar`，
     Firefox 认标准的 `scrollbar-width` / `scrollbar-color`。**只做美化不做隐藏**：
     thumb 平时低调、悬停才亮，"看不见"不等于"不能滚"——真的 `display:none`
     会让鼠标用户没有任何抓手（触屏与键盘还能滚，但那是另一个故事）。 */
  * { scrollbar-width:thin; scrollbar-color:var(--scroll-thumb) transparent; }
  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-track { background:transparent; }
  ::-webkit-scrollbar-thumb {
    background:var(--scroll-thumb); border-radius:8px;
    border:2px solid transparent; background-clip:content-box;
  }
  ::-webkit-scrollbar-thumb:hover { background:var(--scroll-thumb-hover);
    border:2px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-corner { background:transparent; }
  html, body { height:100%; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.6 var(--sans);
    display:flex; flex-direction:column; overflow:hidden;
    -webkit-text-size-adjust:100%;
  }
  button, input, textarea, select { font:inherit; color:inherit; }
  .dim { color:var(--text-dim); }
  .ok { color:var(--ok); }
  .bad { color:var(--bad); }

  /* ── 横幅区（只读 / 目标说明 / 别处在跑 / 模型不通 / 配对码） ── */
  .banner { display:none; padding:7px 14px; font-size:13px;
            border-bottom:1px solid var(--line-soft); }
  #readonly { background:var(--ro-bg); color:var(--ro-text);
              border-bottom-color:var(--ro-line); }
  #target-note { background:var(--info-bg); color:var(--info-text);
                 border-bottom-color:var(--info-line); }
  #elsewhere { background:var(--warn-bg); color:var(--warn-text);
               border-bottom-color:var(--warn-line); }
  #health { background:var(--danger-bg); color:var(--danger-text);
            border-bottom-color:var(--danger-line); }
  #pairing { background:var(--pair-bg); color:var(--pair-text);
             border-bottom-color:var(--pair-line); }
  #pairing .code { font-weight:700; letter-spacing:.06em; }
  .hidden { display:none !important; }

  /* ── 顶条：抽屉开关 + 当前目标（"我在哪儿"必须一眼看得出） ── */
  #top {
    display:flex; align-items:center; gap:10px; padding:8px 12px;
    border-bottom:1px solid var(--line-soft); background:var(--panel);
  }
  #menu { display:none; width:34px; height:34px; border-radius:8px; border:0;
          background:transparent; color:var(--text); cursor:pointer;
          align-items:center; justify-content:center; }
  #menu:hover { background:var(--bubble-user); }
  #menu svg { width:18px; height:18px; }
  #where { color:var(--text); font-weight:600; font-size:13.5px;
           word-break:break-all; }
  #where.empty { color:var(--text-dim); font-weight:400; }

  /* ── 主体：左栏 + 对话面 ── */
  #body { flex:1; display:flex; min-height:0; overflow:hidden; }
  #scrim { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:11; }
  #scrim.show { display:block; }
  #sidebar {
    width:260px; flex:0 0 260px; border-right:1px solid var(--line-soft);
    background:var(--panel); display:flex; flex-direction:column;
    min-height:0; z-index:12;
  }
  /* 两条原生选择器：留着当左栏两段的控件层（键鼠 / 读屏都能用），视觉上让位给行。 */
  .sr-only { position:absolute; width:1px; height:1px; margin:-1px; padding:0;
             border:0; overflow:hidden; clip-path:inset(50%); white-space:nowrap; }
  .side-head { padding:10px 10px 2px; }
  .side-head .meta { font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
  .new { display:flex; align-items:center; gap:8px; width:100%; margin:8px 0 2px;
         padding:8px 10px; border-radius:10px; border:1px solid var(--line);
         background:transparent; color:var(--text); cursor:pointer;
         font-size:13.5px; text-align:left; }
  .new:hover { background:var(--bubble-user); }
  .new svg { width:16px; height:16px; }
  .new.locked { opacity:.45; cursor:default; }
  .new.locked:hover { background:transparent; }
  #panel { flex:1; overflow-y:auto; padding:6px 8px 12px; }
  .grp { margin:6px 0 12px; }
  .grp-name { display:flex; align-items:center; gap:8px; padding:6px 8px;
              border-radius:8px; cursor:pointer; font-weight:600; font-size:13.5px; }
  .grp-name:hover { background:var(--bubble-user); }
  .grp-name .caret { color:var(--text-dim); font-size:11px; width:10px; }
  .grp-name .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grp-name .src { color:var(--text-faint); font-size:11px; }
  .grp-name.on, #sessions .grp-name.on { background:var(--accent-soft); }
  .empty-line { color:var(--text-faint); font-size:12.5px; padding:2px 8px 8px; }
  #sessions .grp-name { font-weight:400; padding-left:24px; font-size:13.5px; }
  .li { display:flex; align-items:center; gap:8px; }
  .li .nm { flex:1 1 auto; }
  .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  /* 行上那个小按钮（移除 / 恢复）。一直看得见，不藏在 hover 里——触屏没有 hover。
     第一次点只是**变成确认态**，第二次点才真的发请求（一点就删太容易误伤）。 */
  .row-act { flex:0 0 auto; padding:1px 7px; border-radius:7px; cursor:pointer;
             border:1px solid var(--line); background:transparent;
             color:var(--text-faint); font-size:11.5px; }
  .row-act:hover { color:var(--text); border-color:var(--text-dim); }
  .row-act.armed { background:var(--danger-bg); color:var(--danger-text);
                   border-color:var(--danger-line); }
  .row-act.back { color:var(--accent); border-color:var(--accent-soft); }
  /* 「已移除」那一行：只是摆在那里等「恢复」，点它是没有用的。 */
  .li.gone { opacity:.6; cursor:default; }
  .grp-sub { padding:6px 8px 2px; color:var(--text-faint); font-size:11.5px; }
  .dot { width:7px; height:7px; flex:0 0 7px; border-radius:50%;
         background:var(--ok); animation:pulse 1.6s ease-in-out infinite; }
  .lock { opacity:.5; }
  .lock .grp-name, .lock .new { pointer-events:none; cursor:default; }
  #side-foot { padding:8px 12px; border-top:1px solid var(--line-soft); font-size:12px; }

  /* ── 对话面 ── */
  #thread { flex:1; position:relative; display:flex; flex-direction:column; min-width:0; }
  #stream { flex:1; overflow-y:auto; overflow-x:hidden;
            padding:20px 18px 26px; scroll-behavior:smooth; }
  #stream .col { max-width:760px; margin:0 auto;
                 display:flex; flex-direction:column; gap:12px; }
  /* 消息成块：用户在右、助手在左（宽屏留白更像文档流） */
  .msg { display:flex; flex-direction:column; min-width:0; }
  .msg.user { align-self:flex-end; max-width:84%; align-items:flex-end; }
  .msg.assistant { align-self:flex-start; max-width:100%; }
  .bubble { padding:9px 14px; border-radius:14px; white-space:pre-wrap;
            overflow-wrap:anywhere; word-break:break-word; min-width:0;
            max-width:100%; }
  /* 普通正文与代码必须看得出是两种东西：正文是一段话（保留换行），
     行内代码是带底色的等宽小片——一眼就能分开。 */
  .prose { white-space:pre-wrap; }
  .prose + .prose { margin-top:9px; }
  .inline-code { font:0.92em/1.5 var(--mono); background:var(--inline-bg);
                 border:1px solid var(--inline-line); border-radius:5px;
                 padding:1px 5px; overflow-wrap:anywhere; word-break:break-word; }
  .msg.user .bubble { background:var(--bubble-user); border-bottom-right-radius:4px; }
  .msg.assistant .bubble { background:var(--bubble-assistant);
                           border:1px solid var(--line-soft);
                           border-bottom-left-radius:4px; }
  .msg.system { align-self:flex-start; max-width:100%; }
  .sys-line { color:var(--text-dim); font-size:13px; padding-left:4px;
              white-space:pre-wrap; overflow-wrap:anywhere; }
  .sys-line.bad { color:var(--bad); }
  .sys-line.ok { color:var(--ok); }
  /* 连不上模型这类失败：一行原始异常谁也读不出下一步。做成一块带左边框的说明。 */
  .sys-line.failure { margin-top:2px; padding:8px 12px; border-left:3px solid var(--danger-line);
                      background:var(--danger-bg); color:var(--danger-text);
                      border-radius:0 8px 8px 0; }
  .sys-line.failure .fail-head { font-weight:600; }
  .sys-line.failure .fail-line { margin-top:2px; }
  .sys-line.failure .fail-raw { margin-top:6px; color:var(--text-dim);
                                font:12px/1.5 var(--mono); overflow-wrap:anywhere; }

  /* 代码块：**明显**的一块——底色、边框、圆角、内边距；自己横向/纵向滚，
     不撑破气泡（`.bubble` 与 `.msg` 上的 min-width:0 是这条能成立的前提）。 */
  .code { margin:10px 0 0; border:1px solid var(--code-line); border-radius:10px;
          overflow:hidden; background:var(--code-bg); max-width:100%; }
  .code-bar { display:flex; align-items:center; gap:8px; padding:5px 8px 5px 12px;
              background:var(--code-bar); color:var(--text-dim);
              font:12px/1.6 var(--mono); border-bottom:1px solid var(--code-line); }
  /* 语言标签可以让位（长语言名不许把复制键挤走），复制键位置固定在最右。 */
  .code-lang { flex:1 1 auto; min-width:0; overflow:hidden;
               text-overflow:ellipsis; white-space:nowrap; }
  .copy-btn { flex:0 0 auto; padding:3px 10px; border-radius:7px;
              border:1px solid var(--line); background:transparent;
              color:var(--text-dim); cursor:pointer; font:12px/1.6 var(--sans); }
  .copy-btn:hover { color:var(--text); border-color:var(--text-dim); }
  .code pre { margin:0; padding:11px 13px; max-height:360px; overflow:auto;
              font:13px/1.55 var(--mono); color:var(--text);
              scrollbar-width:thin; scrollbar-color:var(--scroll-thumb) transparent; }
  .msg.user .code { border-color:var(--line); }

  /* 历史分隔：给人看的，不进模型上下文 */
  .history-sep { display:flex; align-items:center; gap:10px;
                 color:var(--text-faint); font-size:12px; }
  .history-sep::before, .history-sep::after { content:""; flex:1;
                 border-top:1px solid var(--line-soft); }

  /* 待确认 / 待批准：出现时才浮出来，浮在输入条上方（不随输出滚走） */
  #tray { display:flex; flex-direction:column; gap:8px; padding:0 18px;
          max-width:800px; width:100%; margin:0 auto; }
  #tray:empty { display:none; }
  #actions { display:none; gap:8px; justify-content:flex-end; }
  #actions button { padding:7px 16px; border-radius:9px; border:0; cursor:pointer; }
  #apply { background:var(--ok); color:#fff; }
  #reject { background:transparent; color:var(--text); border:1px solid var(--line); }
  #actions button:disabled { opacity:.5; cursor:default; }
  #pending { color:var(--text-dim); font-size:13px; }
  #pending .diff { border:1px solid var(--line); border-radius:10px; padding:8px 10px;
                   margin:6px 0 0; background:var(--code-bg); overflow-x:auto;
                   font:12.5px/1.55 var(--mono); }
  .diff .add { color:var(--ok); } .diff .del { color:var(--bad); }
  .diff .dim { color:var(--text-dim); }

  /* ── 底部输入条：浮动观察 + 外部依赖，不抢输入 ── */
  #thinking { position:absolute; right:22px; bottom:104px; z-index:5;
              display:none; align-items:center; gap:8px; padding:7px 13px;
              border-radius:999px; background:var(--panel-2);
              border:1px solid var(--line); color:var(--text-dim);
              font-size:13px; box-shadow:0 6px 20px rgba(0,0,0,.28); }
  #thinking.show { display:flex; }
  .thinking-dots { display:inline-flex; gap:3px; }
  .thinking-dots i { width:5px; height:5px; border-radius:50%;
                     background:var(--text-dim); animation:think 1.2s infinite; }
  .thinking-dots i:nth-child(2) { animation-delay:.18s; }
  .thinking-dots i:nth-child(3) { animation-delay:.36s; }
  @keyframes think {
    0%, 60%, 100% { opacity:.25; transform:translateY(0); }
    30% { opacity:1; transform:translateY(-3px); }
  }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }

  /* 「回到最新」：人往上翻时才浮出来，别抢滚动 */
  #toBottom { position:absolute; right:22px; bottom:64px; z-index:6;
              width:38px; height:38px; border-radius:50%; cursor:pointer;
              border:1px solid var(--line); background:var(--panel-2);
              color:var(--text); display:none; align-items:center;
              justify-content:center; box-shadow:0 6px 20px rgba(0,0,0,.28); }
  #toBottom.show { display:flex; }
  #toBottom svg { width:18px; height:18px; }

  #composer { padding:10px 18px 14px; }
  .box { display:flex; align-items:flex-end; gap:8px; max-width:800px;
         margin:0 auto; padding:8px 8px 8px 16px; border-radius:22px;
         border:1px solid var(--line); background:var(--panel-2); }
  .box:focus-within { border-color:var(--accent); }
  #goal { flex:1; min-height:26px; max-height:190px; resize:none; border:0;
          outline:none; background:transparent; padding:4px 0; line-height:1.5; }
  #send { flex:0 0 auto; width:36px; height:36px; border-radius:50%; border:0;
          background:var(--accent); color:#fff; cursor:pointer;
          display:flex; align-items:center; justify-content:center; }
  #send svg { width:18px; height:18px; }
  #send:disabled { background:var(--line); color:var(--text-faint); cursor:default; }
  .hint { max-width:760px; margin:8px auto 0; color:var(--text-faint);
          font-size:11.5px; text-align:right; }
  #conn { position:fixed; left:50%; transform:translateX(-50%); bottom:10px;
          color:var(--danger-text); background:var(--danger-bg);
          border:1px solid var(--danger-line); border-radius:999px;
          padding:4px 14px; font-size:12px; display:none; z-index:20; }

  /* ── 手机：左栏收成抽屉、气泡占满、输入条不被键盘顶掉 ── */
  @media (max-width:860px) {
    #menu { display:flex; }
    #sidebar { position:fixed; top:0; bottom:0; left:0; width:282px;
               transform:translateX(-100%); transition:transform .18s ease;
               box-shadow:0 0 40px rgba(0,0,0,.5); }
    body.drawer #sidebar { transform:none; }
    #stream { padding:14px 12px 20px; }
    .msg.user { max-width:92%; }
    #composer { padding:8px 10px calc(10px + env(safe-area-inset-bottom)); }
    #tray { padding:0 10px; }
    #thinking { right:12px; bottom:96px; }
    #toBottom { right:12px; bottom:56px; }
  }
  @media (prefers-reduced-motion: reduce) {
    #stream { scroll-behavior:auto; }
    .dot, .thinking-dots i { animation:none; }
  }
</style>
</head>
<body>
<div id="readonly" class="banner"></div>
<div id="target-note" class="banner"></div>
<div id="elsewhere" class="banner"></div>
<div id="health" class="banner"></div>
<div id="pairing" class="banner"></div>
<div id="top">
  <button id="menu" title="会话列表" aria-label="会话列表"></button>
  <span id="where" class="empty">连接中…</span>
</div>
<div id="scrim"></div>
<div id="body">
  <aside id="sidebar">
    <div class="side-head">
      <button class="new" id="new-session"><span>＋ 新建会话</span></button>
      <div class="meta dim">工作区</div>
      <div class="sr-only">
        <select id="ws"></select>
        <select id="sess"></select>
      </div>
    </div>
    <div id="panel">
      <div id="workspaces"></div>
      <div id="sessions"></div>
    </div>
    <div id="side-foot"><span id="meta" class="dim">连接中…</span></div>
  </aside>
  <div id="thread">
    <div id="stream"></div>
    <div id="thinking"><span class="thinking-dots"><i></i><i></i><i></i></span>正在思考</div>
    <button id="toBottom" title="回到最新" aria-label="回到最新"></button>
    <div id="tray">
      <div id="pending" class="dim">当前没有待确认的改动。</div>
      <div id="actions">
        <button id="apply">应用</button>
        <button id="reject">拒绝</button>
      </div>
    </div>
    <div id="composer">
      <div class="box">
        <textarea id="goal" rows="1" placeholder="给 agent 发消息…"></textarea>
        <button id="send" title="发送"></button>
      </div>
      <div class="hint">Enter 发送 · Shift+Enter 换行</div>
    </div>
  </div>
</div>
<div id="conn">与服务的连接断开了，重连中…</div>
<script>
/* 内联 SVG：零外链图标。
   下面这几行里有 "<"，但它长在**字符串里面**，不是 HTML 解析器看得见的位置：
   能把这段脚本提前收掉的只有"小于号 + 斜杠 + script"那一个序列，而它一次都不出现
   —— `tests/web/test_page.py` 用 node --check 与那两条钉住这件事。 */
const ICON = {
  menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></svg>',
  send: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 12l15-7-6 7 6 7z"></path></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v13"></path><path d="M6 13l6 6 6-6"></path></svg>',
};

const stream = document.getElementById('stream');
const pending = document.getElementById('pending');
const actions = document.getElementById('actions');
const meta = document.getElementById('meta');
const conn = document.getElementById('conn');
const sendBtn = document.getElementById('send');
const goalBox = document.getElementById('goal');
const whereBox = document.getElementById('where');
const noteBox = document.getElementById('target-note');
const elseBox = document.getElementById('elsewhere');
// 两条原生选择器：HTML 里它们是给左栏两段兜底的可访问控件，值也由 drawTarget
// 一起填（fillSelect）——切换只走这一条路径（行点击最终也落到 switchTarget 上）。
const wsBox = document.getElementById('ws');
const sessBox = document.getElementById('sess');
const workspaceList = document.getElementById('workspaces');
const sessionList = document.getElementById('sessions');
const newBtn = document.getElementById('new-session');
const thinkingBox = document.getElementById('thinking');
const toBottomBtn = document.getElementById('toBottom');
const scrim = document.getElementById('scrim');
let running = false, decided = false;
// 这条壳绑在非回环地址上时，服务端只放行"看"：/run 与 /confirm 一律 403，
// 页面必须在**点之前**说出来（点一下被拒是最后一秒才告诉人）。
let readonly = false;
let session = 'cli';
let pendings = [];
// 当前这一屏已经画了几条历史（只往后补，已经画好的不重建）。
let drawn = 0;
let busy = false;
// 页面上当前绑定的目标（"工作区|会话"）。它变了就说明这一屏整体属于上一个目标，
// 得清掉重绑——只更新横幅是不够的。
let bound = '';
// 左栏上一次画出来的内容签名。**只有它变了才重绘**：同一帧状态重复到达
// （事件流补帧、运行状态变化、3 秒轮询）时，一个节点都不该新建。
let sidebarSig = '';
// 人刚点下去、服务端还没回话的那一行（乐观选中）。点了就先亮它，
// 不等回帧——回帧那一趟里带着探活，模型不通时要好几秒。
let chosen = {root: '', session: ''};
// 值了"确认移除"的那一行（二次确认的第一下）。
let armed = '';
// 最近一帧状态：切换失败时用它把选择栏拨回当前目标，别停在选中态骗人。
let lastState = {};
// 模型是不是正在生成。"正在思考"与发送键的可用性都由它推。
let thinking = false;

// 等待时那一行里**不许**出现探活横幅的名字：红色 #health 说的是"连不上"，
// 不是"还在生成"（两者混在一起，人会以为模型挂了）。
const WAIT_PREFIX = '模型生成中，已等待';
// 左栏里"在跑"的标记：它自己会消失，所以必须随每一帧状态重画。
const RUN_MARK = '正在跑';
// 钉住（--root）时两段列表都只读。用类而不是 disabled：这里是 div 行，不是控件。
const LOCK = 'lock';

function buildColumn() {
  const col = document.createElement('div');
  col.className = 'col';
  stream.appendChild(col);
  return col;
}

// 挂到对话列上。返回挂进去的那一块，调用方自己决定要不要滚。
function appendNode(node) {
  let col = stream.querySelector('.col');
  if (col === null) { col = buildColumn(); }
  col.appendChild(node);
  return node;
}

function makeMsg(kind) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + kind;
  appendNode(wrap);
  return wrap;
}

function makeBubble(kind, text) {
  const wrap = makeMsg(kind);
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  return bubble;
}

// 步骤 / 工具 / 用量这些"系统说的一行"——左对齐的细字，不是气泡：
// 它们不是"助手说的话"，混进气泡里会让人以为模型在自言自语。
function makeSystem(text, cls) {
  const wrap = makeMsg('system');
  const line = document.createElement('div');
  line.className = 'sys-line ' + (cls || '');
  line.textContent = text;
  wrap.appendChild(line);
  return wrap;
}

// 助手的话里有三样东西要分开画：**正文**、**围栏代码块**（``` 围起来）、
// **行内代码**（` 围起来）。全都用 textContent 装配而不是拼 innerHTML：
// 模型输出是不可信输入，拼标签等于把页面交给它。
//
// 分成"奇数块 = 代码"是 `split('```')` 的性质：围栏一定成对，割出来就是
// [正文, 代码, 正文, 代码, …]。以前每一块都被当成代码块画（连正文也是），
// 于是整条消息看上去全是等宽的代码——这正是"代码与对话分不开"的根。
function fillRich(bubble, content) {
  const text = String(content === null || content === undefined ? '' : content);
  const parts = text.split('```');
  for (let index = 0; index < parts.length; index += 1) {
    if (index % 2 === 1) {
      bubble.appendChild(codeBlock(parts[index]));
      continue;
    }
    appendProse(bubble, parts[index]);
  }
}

// 正文：按 ` 再切成"普通字"与"行内代码"，一句一段（换行保留）。
function appendProse(bubble, raw) {
  const text = raw.replace(/^\\r?\\n/, '').replace(/\\r?\\n$/, '');
  if (text === '') { return; }
  const para = document.createElement('div');
  para.className = 'prose';
  const bits = text.split('`');
  for (let index = 0; index < bits.length; index += 1) {
    const piece = bits[index];
    if (piece === '') { continue; }
    if (index % 2 === 1) {
      const inline = document.createElement('code');
      inline.className = 'inline-code';
      inline.textContent = piece;
      para.appendChild(inline);
      continue;
    }
    para.appendChild(document.createTextNode(piece));
  }
  if (para.childNodes.length) { bubble.appendChild(para); }
}

// 围栏代码块：自己滚（横向 + 纵向），复制键固定在右上角，语言标签可选。
function codeBlock(raw) {
  const body = raw.replace(/^\\r?\\n/, '').replace(/\\r?\\n$/, '');
  const lines = body.split('\\n');
  const first = lines.length > 1 ? lines[0].trim() : '';
  // 第一行是个"一个词"（没空格、不太长）就当语言名：```python / ```powershell。
  const fenced = first !== '' && first.indexOf(' ') === -1 && first.length <= 24;
  const payload = fenced ? lines.slice(1).join('\\n') : body;
  const code = document.createElement('div');
  code.className = 'code';
  const bar = document.createElement('div');
  bar.className = 'code-bar';
  const label = document.createElement('span');
  label.className = 'code-lang';
  label.textContent = fenced ? first : '代码';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'copy-btn';
  copy.textContent = '复制';
  copy.onclick = function () { copyText(payload, copy); };
  bar.appendChild(label);
  bar.appendChild(copy);
  const pre = document.createElement('pre');
  const codeEl = document.createElement('code');
  codeEl.textContent = payload;
  pre.appendChild(codeEl);
  code.appendChild(bar);
  code.appendChild(pre);
  return code;
}

// 复制走 navigator.clipboard；失败就选中那段代码让人自己按 Ctrl+C——
// 不弹第二个对话框（本地壳上"复制"点一下就该完事）。
function copyText(text, button) {
  const done = function (ok) { button.textContent = ok ? '已复制' : '复制失败'; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function () { done(true); },
      function () { done(false); }
    );
    return;
  }
  done(false);
}

/* ── 失败要说人话（尤其"连不上模型"这一种） ──
   一行原始异常（`LlamaCppError: …`、`ConnectTimeout`、一截回溯）谁也读不出
   下一步该干什么。这里只做**展示层**：把错误文本分类成人能读的四件事——
   哪一步失败、地址是哪个、可能为什么、下一步敲什么命令。

   线索从哪来：① 服务端在 final 事件上**只增**的那一条结构化字段
   `error: {stage, url, kind}`（别的一概没动）；② 没有它时按错误文本里的词
   自己认（老服务端、别的失败路径）。地址还能退回 `/state` 的探活结果。

   页面上的东西一律先过 `redact`：密钥、Bearer 串、KEY=VALUE 不许出现在页面上。 */
const FAIL_KINDS = [
  {kind: 'connect', marks: ['无法连接 llama-server', 'ConnectError', 'ConnectTimeout',
    'Connection refused', '连接被拒绝', '连接失败']},
  {kind: 'slow', marks: ['秒内没返回', 'ReadTimeout', 'Read timed out', 'timed out',
    'timeout', '超时']},
  {kind: 'server', marks: ['服务返回', 'tokenize 返回', 'LlamaCppError', 'HTTP 5']},
];

// 页面上的兜底遮蔽：密钥那几类串一律换成 ***（不碰别的字，原文照旧可读）。
function redact(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/g, '$1***')
    .replace(/(Bearer\\s+)[A-Za-z0-9._~+/-]{4,}/gi, '$1***')
    .replace(/((?:api[-_]?key|token|secret|password|passwd|authorization)\\s*[=:]\\s*)\\S+/gi,
      '$1***');
}

// 这条错误是"哪一步"坏了。认不出来就是空串（不当成模型故障，别乱指路）。
function failureKind(text) {
  const line = String(text || '');
  for (const item of FAIL_KINDS) {
    for (const mark of item.marks) {
      if (line.indexOf(mark) >= 0) { return item.kind; }
    }
  }
  return '';
}

function failureCause(kind) {
  if (kind === 'connect') { return '模型服务没起 / 端口不对 / 代理挡了'; }
  if (kind === 'slow') { return '这次要生成的内容太长 / 模型太慢 / 服务卡住'; }
  if (kind === 'server') { return '服务端参数不对 / 上下文超限 / 进程刚重启'; }
  return '';
}

function failureNext(kind) {
  if (kind === 'connect') {
    return '`spool service list` 看本机模型配方在不在跑'
      + '（配方表里那条叫 llama 的话，`spool service start llama` 就能起它）；'
      + '`spool config` 看 provider 与 base_url 是从哪来的；把它起好再发一次';
  }
  if (kind === 'slow') {
    return '把活拆小再发；`spool service list` 看模型进程还在不在';
  }
  if (kind === 'server') {
    return '看下面那行原始信息；`spool service list` 看服务状态';
  }
  return '';
}

// 模型地址：以这一次运行的配置为准（服务端探活接口给的），没有就说"配置里那个"。
function modelAddress(extra) {
  const info = extra || {};
  if (info.url) { return String(info.url); }
  const model = ((lastState || {}).health || {}).model || {};
  return String(model.target || '');
}

// 从一堆行里挑出**最有信息量**的那一行：报错那行通常带 Error / 连接 / 超时这些词。
function usefulLine(text) {
  const lines = String(text || '').split('\\n')
    .map(function (one) { return one.trim(); })
    .filter(function (one) { return one !== ''; });
  if (!lines.length) { return ''; }
  for (const one of lines) {
    if (/Error|错误|连不上|连接|超时|timed out|拒绝|返回/.test(one)) { return one; }
  }
  return lines[lines.length - 1];
}

// 认出来就返回 {head, lines, raw}；认不出来返回 null（调用方走原来的画法）。
function explainFailure(text, extra) {
  const info = extra || {};
  const kind = info.kind || failureKind(text);
  if (kind === '') { return null; }
  const where = modelAddress(info);
  const stage = kind === 'connect' ? '连接模型'
    : kind === 'slow' ? '等模型生成' : '模型服务回话';
  const lines = [
    '模型地址：' + (where === '' ? '（按配置里的地址）' : where),
    '可能原因：' + failureCause(kind),
    '下一步：' + failureNext(kind),
  ];
  const raw = redact(usefulLine(text)).slice(0, 300);
  return {
    head: '失败环节：' + stage + '（这一步没过去，后面的活一件都没做）',
    lines: lines,
    raw: raw,
  };
}

// 对话流里的那一块。认不出来的错误仍按**原来的画法**画（不假装懂）：
// `head` 就是原来那个词——`final` 是「失败：」、`error` 事件是「错误：」。
function showFailure(head, text, extra) {
  const parts = explainFailure(text, extra);
  if (parts === null) { add(head + redact(text), 'bad'); return; }
  const stick = atBottom();
  const wrap = makeMsg('system');
  const box = document.createElement('div');
  box.className = 'sys-line failure';
  const title = document.createElement('div');
  title.className = 'fail-head';
  title.textContent = parts.head;
  box.appendChild(title);
  for (const item of parts.lines) {
    const line = document.createElement('div');
    line.className = 'fail-line';
    line.textContent = item;
    box.appendChild(line);
  }
  if (parts.raw) {
    const raw = document.createElement('div');
    raw.className = 'fail-raw';
    raw.textContent = '原始信息：' + parts.raw;
    box.appendChild(raw);
  }
  wrap.appendChild(box);
  if (stick) { scrollDown(); }
  updateToBottom();
}

// 历史**增量追加**：同一个目标上，/state 的快照可能比上一帧多出几条
// （一次运行跑完之后库里的往来就长了）。以前是"画过就不再画"，于是多出来的
// 那几条永远不出现；现在只补**还没画过**的那一段，已经画好的一条都不重建。
function drawHistory(messages) {
  const items = messages || [];
  if (drawn > items.length) {
    // 列表变短了（换了库、清过历史）：只能重来一遍。
    stream.replaceChildren();
    drawn = 0;
  }
  if (!items.length || drawn === items.length) { return; }
  const stick = atBottom();
  if (drawn === 0) {
    const sep = document.createElement('div');
    sep.className = 'history-sep';
    sep.textContent = '之前的往来（给人看的，不进模型上下文）';
    appendNode(sep);
  }
  for (let index = drawn; index < items.length; index += 1) {
    const item = items[index];
    const bubble = makeBubble(
      item.role === 'user' ? 'user' : 'assistant',
      ''
    );
    fillRich(bubble, item.content);
  }
  drawn = items.length;
  if (stick) { scrollDown(); }
}

/* ── 滚动：默认跟到底，人往上翻就不抢 ── */
function atBottom() {
  return (stream.scrollHeight - stream.scrollTop - stream.clientHeight) <= 48;
}

function scrollDown() {
  // 显式 `behavior:'auto'`：CSS 里的 `scroll-behavior:smooth` 在学习用，但**跟到底**
  // 必须是瞬时的——平滑滚动期间 `scrollTop` 还在路上，一串快速事件会让
  // `atBottom()` 判成"人不在底部"，右下角那个「回到最新」就会一闪一闪。
  if (stream.scrollTo) {
    stream.scrollTo({top: stream.scrollHeight, behavior: 'auto'});
    return;
  }
  stream.scrollTop = stream.scrollHeight;
}

// 悬浮按钮只有"人不在底部"时才摆出来。它同时管两件事：别抢滚动，和让人能回去。
function updateToBottom() {
  const away = !atBottom();
  toBottomBtn.classList.toggle('show', away);
}

function add(text, cls) {
  const stick = atBottom();
  const kind = cls === undefined ? '' : String(cls);
  if (kind.indexOf('me') >= 0) {
    makeBubble('user', text);
  } else if (kind.indexOf('say') >= 0) {
    const bubble = makeBubble('assistant', '');
    fillRich(bubble, text);
  } else {
    makeSystem(text, kind);
  }
  if (stick) { scrollDown(); }
  updateToBottom();
}

/* ── 顶上的目标选择（原来是两条下拉，现在是左栏两段） ──
   「我在哪个工作区」必须一眼看得出：批次一踩过绑错工作区、把 __init__.py 写空的坑，
   而机器级壳服务的是整台机器（设计 §3.3）。 */
function fillSelect(box, rows, value) {
  // 备选与当前值都没变就别动它：`replaceChildren` 一次就是几十个节点，
  // 而每一帧状态都会走到这里（同一帧重复到达时纯属白干）。
  const sign = JSON.stringify([rows, value]);
  if (box.dataset.fill === sign) { box.value = value; return; }
  box.dataset.fill = sign;
  box.replaceChildren();
  for (const row of rows) {
    const item = document.createElement('option');
    item.value = row.value;
    item.textContent = row.text;
    item.selected = row.value === value;
    box.appendChild(item);
  }
}

// 左栏那两段（可见）背后就是两条原生选择器（隐藏）：点一行 = 改控件 + 派发 change，
// 于是"切目标"这条路上仍然只有 `switchTarget` 一处（原生控件的 change 监听）。
// 为什么要这层：可见的行是 div，键盘与读屏够不着它；真正能操作的是 select。
function pick(box, value) {
  // 目标名**不一定**已经是控件里的一项（当前会话没跑过就不在会话表里）。
  // 直接赋值会被浏览器丢掉（value 落到空串），于是"点一行"就变成"不改会话"。
  let found = false;
  for (const item of box.options) {
    if (item.value === value) { found = true; }
  }
  if (!found) {
    const item = document.createElement('option');
    item.value = value;
    item.textContent = value;
    box.appendChild(item);
  }
  box.value = value;
}

// 左栏的行最终落到这两个控件上，`change` 的归宿仍然只有 `switchTarget` 一处。
function target(workspace, name) {
  if (workspace !== '') { pick(wsBox, workspace); }
  if (name !== '') { pick(sessBox, name); }
  // 两条都派发。**原先只派发会话那一条**，于是点工作区那一列什么都不会发生
  // （ws 那条 change 监听永远收不到事件），而页面看起来"点了没反应"。
  // 两条监听各自有一道"值没变就不发请求"的守卫，所以多派发一次不会多发一次请求。
  wsBox.dispatchEvent(new Event('change'));
  sessBox.dispatchEvent(new Event('change'));
}

// ── 左栏的"行复用" ──
// 每一行先描述成一份 spec（文字 / 高亮 / 在不在跑 / 那个小按钮），
// **只有 sig 变了才建节点**：同一帧状态重复到达时，一个节点都不新建、也不替换。
function lineSpec(sig, make) { return {sig: sig, make: make}; }

function actSpec(label, kind, value, onClick) {
  return lineSpec(JSON.stringify(['act', label, kind, value]), function () {
    return rowAction(label, kind, value, onClick);
  });
}

function rowSpec(label, title, on, onClick, key, kind, act, running) {
  // 签名里**不含**"亮不亮"：切一行的高亮只改类，不重建节点（`markChosen` 收尾）。
  return lineSpec(
    JSON.stringify(['row', label, title, key || '', kind || '', running ? 1 : 0,
      act ? act.sig : '']),
    function () { return row(label, title, on, onClick, key, kind, act, running); }
  );
}

// 左栏的一行。点它就是切目标（经 `target()` 落到原生控件上）。
// `key`/`kind` 是给"乐观选中"用的：点下去立刻按它们把这一行点亮，不等回帧。
function row(label, title, on, onClick, key, kind, act, running) {
  const line = document.createElement('div');
  line.className = 'li grp-name';
  line.dataset.key = key || '';
  line.dataset.kind = kind || '';
  // 「已移除」那一行不是可点的目标：它只是摆在那里等「恢复」。
  if (kind === 'gone') { line.classList.add('gone'); }
  if (on) { line.classList.add('on'); }
  const mark = document.createElement('span');
  mark.className = 'caret';
  mark.textContent = on ? '▸' : '·';
  const name = document.createElement('span');
  name.className = 'nm';
  name.textContent = label;
  line.appendChild(mark);
  line.appendChild(name);
  if (title) {
    const src = document.createElement('span');
    src.className = 'src';
    src.textContent = title;
    line.appendChild(src);
  }
  markRunning(line, Boolean(running));
  if (act) { line.appendChild(act.make()); }
  line.onclick = onClick;
  return line;
}

// 行上那个小按钮（移除 / 恢复）。点第一下只是**变成确认态**，第二下才发请求：
// 一点就删太容易误伤（这一行和"切到这条会话"只差几个像素）。
function rowAction(label, kind, value, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'row-act' + (kind === 'restore' ? ' back' : '');
  button.dataset.act = kind;
  button.dataset.value = value;
  button.textContent = label;
  button.onclick = function (event) {
    event.stopPropagation();   // 别把"点按钮"当成"切到这一条"
    onClick();
  };
  return button;
}

// 确认态（`armed`）就地改文字与类，不重建行——重建会把整段列表推倒重来。
function markArmed() {
  for (const button of document.querySelectorAll('.row-act')) {
    if (button.dataset.act !== 'remove') { continue; }
    const ready = button.dataset.value === armed;
    button.textContent = ready ? '确认移除' : '移除';
    button.classList.toggle('armed', ready);
  }
}

// 一段列表（段名 + 若干行）按 spec 同步：同 sig 的那一行原样留着。
function syncGroup(box, specs) {
  for (let index = 0; index < specs.length; index += 1) {
    const spec = specs[index];
    const have = box.children[index];
    if (have && have._sig === spec.sig) { continue; }
    const node = spec.make();
    node._sig = spec.sig;
    if (have) { box.replaceChild(node, have); } else { box.appendChild(node); }
  }
  while (box.children.length > specs.length) { box.removeChild(box.lastChild); }
}

function headSpec(text) {
  return lineSpec('head|' + text, function () {
    const head = document.createElement('div');
    head.className = 'grp-name dim';
    head.textContent = text;
    return head;
  });
}

// 「正在跑」的标记。`state.running_targets` 是**唯一**能说出这件事的地方
// （别的目标上的任务，在本目标的事件流上一个字都不会出现，而 `running` 只讲当前那个）。
function runningRows(state) {
  const rows = state.running_targets || [];
  const found = {};
  for (const one of rows) {
    found[(one.root || '') + '|' + (one.session || '')] = true;
  }
  return found;
}

function markRunning(line, on) {
  if (!on) { return; }
  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.title = RUN_MARK;
  line.appendChild(dot);
}

// 页面上"选中的那一行"：优先人刚点的那个（乐观），否则是服务端确认的目标。
function selectedKeys() {
  if (chosen.root || chosen.session) { return chosen; }
  const t = lastState.target || {};
  return {root: t.root || '', session: t.session || ''};
}

// 只改类的"点亮/熄灭"，不碰结构：点一行的那一下必须**立刻**看得见选中态
// （回帧要等服务端，模型不通时那一趟能有好几秒，人只会以为壳卡死了）。
function markChosen() {
  const want = selectedKeys();
  for (const box of [workspaceList, sessionList]) {
    for (const line of box.children) {
      if (!line.dataset || !line.dataset.kind) { continue; }
      const kind = line.dataset.kind;
      // 「已移除」那几行不是目标（点了也没用），不参与点亮。
      const mine = kind === 'ws'
        ? line.dataset.key === want.root
        : (kind === 'sess' && line.dataset.key === want.session);
      line.classList.toggle('on', mine);
      const caret = line.querySelector('.caret');
      if (caret) { caret.textContent = mine ? '▸' : '·'; }
    }
  }
}

function renderSidebar(wrows, srows, sremoved, key, locked) {
  const live = runningRows(lastState);
  const here = (lastState.target || {}).root || '';
  // 同键跳过：这一份签名没变就**一个节点都不动**。签名里带上"谁在跑"与当前目标，
  // 所以该重画的时候（数据真变了）一定会重画。
  const sig = JSON.stringify([wrows, srows, sremoved, key, locked, here, session, live]);
  if (sig === sidebarSig) { return; }
  sidebarSig = sig;

  const wsSpecs = [headSpec('工作区')];
  for (const one of wrows) {
    if (one.value === '') { continue; }
    const on = (one.value + '|' + session) === key;
    wsSpecs.push(rowSpec(
      one.name + ((one.ready === false) ? '（没 init）' : ''),
      '',
      on,
      (function (value) {
        return function () { target(value, ''); };
      })(one.value),
      one.value,
      'ws',
      null,
      Boolean(live[one.value + '|' + session])
    ));
  }
  if (wsSpecs.length === 1) {
    wsSpecs.push(lineSpec('empty-line', function () {
      const none = document.createElement('div');
      none.className = 'empty-line';
      none.textContent = '（登记表是空的：先在工作区目录里敲 spool init）';
      return none;
    }));
  }
  syncGroup(workspaceList, wsSpecs);
  // 钉住（--root）时两段列表都只读：`LOCK` 走 CSS 的 pointer-events，
  // 行本身还是画出来（人要看得到"这里有哪几条"），只是点不动。
  workspaceList.classList.toggle(LOCK, locked);

  const sessSpecs = [headSpec('会话')];
  for (const one of srows) {
    if (one.value === '') { continue; }
    const on = one.value === session;
    const removable = !locked && !readonly;
    sessSpecs.push(rowSpec(
      one.text,
      '',
      on,
      (function (value) {
        return function () { target('', value); };
      })(one.value),
      one.value,
      'sess',
      removable
        ? actSpec('移除', 'remove', one.value, (function (value) {
            return function () { requestRemove(value); };
          })(one.value))
        : null,
      // 「正在跑」的键是 (工作区, 会话)：别的会话在跑也要标出来
      // （切走不杀任务，见设计 §3.4）。
      Boolean(live[keyFor(here, one.value)])
    ));
  }
  if (sremoved.length) {
    sessSpecs.push(lineSpec('sub|' + sremoved.map(function (row) { return row.id; }).join(','),
      function () {
        const sub = document.createElement('div');
        sub.className = 'grp-sub';
        sub.textContent = '已移除（' + sremoved.length + '）— 点「恢复」放回来';
        return sub;
      }));
    for (const one of sremoved) {
      sessSpecs.push(rowSpec(
        one.id + (one.title ? '  —  ' + one.title : ''),
        '',
        false,
        null,
        one.id,
        'gone',
        readonly ? null : actSpec('恢复', 'restore', one.id, (function (value) {
          return function () { requestRestore(value); };
        })(one.id)),
        false
      ));
    }
  }
  syncGroup(sessionList, sessSpecs);
  sessionList.classList.toggle(LOCK, locked);
  markArmed();
  // 收尾点亮选中态：复用的那几行（签名没变、节点没重建）也要跟着高亮走。
  markChosen();
}

function drawTarget(state) {
  const t = state.target || {};
  const key = (t.root || '') + '|' + (t.session || '');
  // 第一次画不算"变了"：那时页面上本来就什么都没有，清一遍只会白闪一下。
  const changed = bound !== '' && key !== bound;
  bound = key;

  const workspaces = state.workspaces || [];
  const rows = [{value: '', text: '（选一个工作区）'}];
  for (const row of workspaces) {
    rows.push({
      value: row.root,
      text: (row.name || row.root) + '  —  ' + row.root
    });
  }
  // 当前目标不在登记表里（指针指着一个后来被删掉的工作区）：也得显示得出来，
  // 否则下拉会显示成"没选"，而人明明在某个工作区里。
  if (t.root && !workspaces.some(function (row) { return row.root === t.root; })) {
    rows.push({value: t.root, text: t.root + '  ——  当前（不在登记表里）'});
  }
  fillSelect(wsBox, rows, t.root || '');

  const sessions = (state.sessions || []).slice();
  const srows = [{value: '', text: '（不改会话）'}];
  for (const row of sessions) {
    srows.push({
      value: row.id,
      text: row.title ? row.id + '  —  ' + row.title : row.id,
    });
  }
  // 当前会话**不一定**在会话表里：那张表按"跑过没有"记，`cli` 这种还没跑过的名字
  // 不会出现。权威值是 `target.session`，表只是备选。
  if (t.session && !sessions.some(function (row) { return row.id === t.session; })) {
    srows.push({value: t.session, text: t.session + '  ——  当前'});
  }
  fillSelect(sessBox, srows, t.session || '');

  // `--root` 起的壳只有一个目标：两段列表都只读，并把原因写出来
  // （不给原因，人只会对着两个灰下拉发呆）。
  const locked = Boolean(t.pinned);
  wsBox.disabled = locked;
  sessBox.disabled = locked;

  if (t.root) {
    whereBox.textContent = t.root + '  ·  会话 ' + t.session;
    whereBox.className = 'where';
  } else {
    whereBox.textContent = '还没有目标';
    whereBox.className = 'where empty';
  }

  // 左栏那两段：工作区在前、它会话在后。点一下就是一次 POST /target。
  renderSidebar(
    workspaces.map(function (w) {
      return {value: w.root, name: w.name || w.root, ready: w.ready};
    }),
    sessions.map(function (s) {
      return {value: s.id, text: s.title || s.id};
    }),
    // 已移除（归档）的那几条单独一段：删了还能放回来，才算"移除"而不是"毁掉"。
    (state.sessions_removed || []).map(function (s) {
      return {id: s.id, title: s.title || ''};
    }),
    key,
    locked
  );
  // 「新建会话」也归这两条判据管：钉住的壳只有一个目标，只读的壳点了会被拒。
  const dead = locked || readonly;
  newBtn.classList.toggle(LOCK, dead);
  newBtn.disabled = dead;

  const notes = [];
  if (locked) {
    notes.push('这条壳是用 --root 起的：只有这一个目标，切不了。'
      + '去掉 --root 重启就是机器级。');
  }
  if (!t.root) {
    // 空态要**引导人选**，不是摆一堆空白：候选清单就在左边那条工作区段里。
    // 但那张表也可能是**空的**（一个工作区都没登记过）——那时说"在左边选一个"
    // 就是死路，所以补一句怎么造出候选来（评审逮到的就是这条）。
    notes.push(t.note || '还没有选工作区：在左边的「工作区」里选一个'
      + '（表是空的就先在工作区目录里敲 spool init）。');
  }
  if (busy) {
    // 后端没有停止接口（只有 /run 与 /confirm），所以发送键在运行中只能禁用。
    // 不解释的话，那个灰掉的圆钮就是"坏了"。
    notes.push('这条正在跑：等服务端收尾再发下一条（这个壳没有停止运行接口）。');
  }
  if (notes.length) {
    noteBox.style.display = 'block';
    noteBox.textContent = notes.join(' ');
  } else {
    noteBox.style.display = 'none';
    noteBox.replaceChildren();
  }
  return changed;
}

function short(text, limit) {
  const line = String(text || '');
  return line.length > limit ? line.slice(0, limit) + '…' : line;
}

// 「别处还有一条在跑」：切走不杀任务（设计 §3.4），页面就必须**说得出**这件事，
// 不能让它看起来只有一条。这一行不走事件流——别的目标上起来的任务，在本目标的
// 事件流上什么都没有，只能按 `/state` 的 `running_targets` 轮出来。
function showElsewhere(state) {
  const others = (state.running_targets || []).filter(function (row) {
    return !row.current;
  });
  elseBox.replaceChildren();
  if (!others.length) {
    elseBox.style.display = 'none';
    return;
  }
  elseBox.style.display = 'block';
  for (const row of others) {
    const line = document.createElement('div');
    line.textContent = '别处还有一条在跑：' + (row.name || row.root)
      + '（会话 ' + row.session + '）— ' + short(row.goal, 60)
      + (row.awaiting ? '（在等你确认）' : '');
    elseBox.appendChild(line);
  }
}

// 重绑这一屏：目标换了，旧的输出、待确认面板、历史进度都属于**上一个**目标。
function resetView() {
  stream.replaceChildren();
  drawn = 0;
  armed = '';
  clearPending();
  actions.style.display = 'none';
  updateToBottom();
}

function showPending(items) {
  if (!items.length) { clearPending(); return; }
  pendings = items;
  pending.innerHTML = '';
  pending.className = '';
  for (const item of items) { pending.appendChild(diffBox(item.path, item.text)); }
}

function diffBox(path, text) {
  const box = document.createElement('div');
  box.className = 'diff';
  const head = document.createElement('div');
  head.className = 'dim';
  head.textContent = '待确认：' + path;
  box.appendChild(head);
  for (const raw of text.split('\\n')) {
    const line = document.createElement('div');
    line.className = raw.startsWith('+') ? 'add'
      : raw.startsWith('-') ? 'del' : 'dim';
    line.textContent = raw;
    box.appendChild(line);
  }
  return box;
}

function setRunning(value) {
  const was = busy;
  running = value;
  busy = value;
  goalBox.readOnly = value || readonly;
  syncSend();
  setThinking(value && thinking);
  // 运行中那句提示在横幅上（见 drawTarget）：这一格变了就要重画一次，
  // 否则它会停在上一帧——"跑完了还写着正在跑"比不写还坏。
  if (was !== busy && lastState && lastState.target) { drawTarget(lastState); }
}

// 发送键的两态：运行中 / 只读就是禁用（后端没有停止接口，见 drawTarget 里的说明）。
function syncSend() {
  sendBtn.disabled = busy || readonly;
  sendBtn.title = busy ? '运行中：服务端还没给出停止接口' : '发送';
}

// 「正在思考」：由既有的 wait / start / final 驱动，不新造协议。
function setThinking(value) {
  thinking = Boolean(value);
  thinkingBox.classList.toggle('show', thinking && !readonly);
  syncSend();
}

function setMeta(steps) {
  meta.textContent = session + ' · ' + (steps || 0) + ' 步';
}

function clearPending() {
  pendings = [];
  pending.className = 'dim';
  pending.textContent = '当前没有待确认的改动。';
}

function keyFor(root, name) {
  return (root || '') + '|' + (name || '');
}

function applyState(state) {
  lastState = state;
  // 只读与"在不在跑"必须在 drawTarget **之前**定下来：横幅里那两句
  // （只读 / 运行中）与发送键的两态都按它们算，晚一步就会画成上一帧的样子。
  showReadonly(state);
  running = Boolean(state.running);
  busy = running;
  // 会话名要先定：左栏那一列的高亮按它算（晚一步就会高亮**上一条**会话）。
  const target = state.target || {};
  session = target.session || state.session || session;
  // 目标先画：它变了就**整屏重绑**（服务端在目标变了时会补一帧 state，
  // 那一帧也要走这条重绑，不能只更新横幅）。
  if (drawTarget(state)) { resetView(); }
  // 高亮跟着服务端确认的目标走（乐观那一下到这里就交棒了）。**只有当它兑现了**
  // （回帧就是它）或压根没有时才清：人手快、连点了两行时，第一行的回帧不该
  // 把第二行刚点亮的选择抹掉。
  const settled = (chosen.root === '' && chosen.session === '')
    || keyFor(chosen.root, chosen.session) === keyFor(target.root, target.session);
  if (settled) { chosen = {root: '', session: ''}; }
  markChosen();
  const usage = state.usage || {};
  setMeta(usage.steps);
  setRunning(running);
  // 断线重连时事件已经漏掉了，只能按快照把待确认面板整个重建出来。
  // 空表**不动**面板：等命令授权时这里没有 diff，而那条"要执行：…"正摆在那里。
  const pendingItems = state.diffs || [];
  if (pendingItems.length) { showPending(pendingItems); }
  // 等命令授权时**没有 diff**：不把那条命令显示出来，按钮就是盲操作。
  const asking = state.awaiting_detail || {};
  if (state.awaiting && asking.command) { showCommandAsk(asking); }
  if (state.awaiting) { showActions(); } else { actions.style.display = 'none'; }
  drawHistory(state.messages);
  showPairings(state.pairings);
  showHealth(state.health);
  showElsewhere(state);
}

// 配对码给人看，不进模型上下文。批准发生在另一个进程
// （本机敲 spool approve 加码），所以还要短轮询 /state，这一行才会自己消失。
// 不能复用「应用/拒绝」：点应用会走 /confirm 去落盘改动。
function showPairings(items) {
  const box = document.getElementById('pairing');
  if (!items || !items.length) {
    box.style.display = 'none';
    box.replaceChildren();
    return;
  }
  box.style.display = 'block';
  box.replaceChildren();
  const head = document.createElement('div');
  head.textContent = '有 ' + items.length +
    ' 个待批准的通道配对请求（来自手机/聊天通道）。在本机终端批准；点「应用」不会批准配对。';
  box.appendChild(head);
  for (const item of items) {
    const line = document.createElement('div');
    const cmd = document.createElement('span');
    cmd.className = 'code';
    cmd.textContent = 'spool approve ' + item.code;
    line.appendChild(cmd);
    line.appendChild(document.createTextNode('  ←  ' + item.user));
    box.appendChild(line);
  }
}

// 「这是什么时候探的」必须说得出：探活挪到后台之后，`/state` 给的是**最近一次**
// 结果，可能是几秒前那一份。陈旧值可以接受，**静默的**陈旧值不行——不然一个
// 几分钟前的结论看起来就像刚刚测的。用服务端算好的 `age_s`（两边的墙钟不保证
// 同步，页面拿它换算成"3 秒前"就行），页面只负责说人话。
function probedAgo(health) {
  const info = health || {};
  const age = info.age_s;
  if (age === null || age === undefined) {
    return info.probed_at ? '' : '（还没探过）';
  }
  const seconds = Math.max(0, Math.round(Number(age)));
  if (seconds < 2) { return '（刚探过）'; }
  if (seconds < 60) { return '（' + seconds + ' 秒前探过）'; }
  return '（' + Math.round(seconds / 60) + ' 分钟前探过）';
}

// 探活不通那一行的开头。`slow`（连上了、只是没在超时内回话）与别的不通对人的
// 下一步不一样（去看模型是不是在加载 vs 去把服务起起来），所以不合成一句
// "连不上"——两句话都由 `failureCause` / `failureNext` 往下说。
function modelDownLine(kind, where, ago) {
  if (kind === 'slow') { return '本机模型没在超时内回话' + where + ago; }
  return '本机模型连不上' + where + ago;
}

function showHealth(health) {
  const box = document.getElementById('health');
  const bits = [];
  const info = health || {};
  const model = info.model || {};
  if (model.ok === false) {
    // 横幅上也**说人话**：光摆一句 `timed out`，人只会以为壳坏了。
    // 什么时候探的 + 可能原因 + 下一步，与对话流里那一块同一套说法（`failure*`），
    // 但这里的"下一步"给的是**探活**该做的事：没人在等生成，所以不套用
    // "把活拆小再发"（那是生成超时的下一步）。分类优先用服务端**只增**的 `kind`。
    const where = model.target ? '（' + model.target + '）' : '';
    const kind = model.kind || failureKind(model.detail) || 'connect';
    const why = kind === 'slow'
      ? '模型服务在、但没在超时内回话（在加载模型 / 太慢 / 卡住）'
      : kind === 'server'
        ? '服务在、但回话不是 200（参数不对 / 上下文超限 / 刚重启）'
        : failureCause('connect');
    bits.push(modelDownLine(kind, where, probedAgo(info))
      + '：可能原因：' + why
      + '；下一步：' + failureNext(kind === 'server' ? 'server' : 'connect'));
  } else if (info.state === 'unknown') {
    // 后台探活刚起来：说"还没探过"，**不画成"通了"**（那是静默的假话）。
    bits.push('本机模型：还没探过（后台正在探，几秒后这里会自己更新）。');
  }
  const services = (health && health.services) || [];
  for (const item of services) {
    if (item.ok === false) {
      const where = item.target ? '（' + item.target + '）' : '';
      bits.push((item.name || '外接服务') + '连不上' + where
        + '：' + redact(item.detail || '连不上'));
    }
  }
  const bridge = (health && health.bridge) || {};
  if (bridge.name && bridge.ok === false) {
    const where = bridge.target ? '（' + bridge.target + '）' : '';
    bits.push('通道已退出' + where);
  }
  if (!bits.length) {
    box.style.display = 'none';
    box.replaceChildren();
    return;
  }
  box.style.display = 'block';
  box.replaceChildren();
  for (const text of bits) {
    const line = document.createElement('div');
    line.textContent = text;
    box.appendChild(line);
  }
}

// 短轮询 /state。这几样都**不一定伴随事件**：配对码是另一个进程在批，
// 而「别处还有一条在跑」说的是别的目标上的任务——它在当前目标的事件流上
// 一个字都不会出现，只轮不推的话这条提示永远不会亮。
async function refreshBanners() {
  try {
    const response = await fetch('/state');
    if (!response.ok) { return; }
    const state = await response.json();
    showReadonly(state);
    showPairings(state.pairings);
    showHealth(state.health);
    showElsewhere(state);
  } catch (err) { /* 轮询失败就等下一拍，横幅停在上一帧 */ }
}

// 「它想跑一条白名单外的命令」——把这句和被问的东西一起摆在按钮上面。
// 授权和落盘是两件事：一个是问"能不能跑"，一个是问"要不要写入"，
// 混成一句「有 1 处改动待确认」，人根本不知道自己在批什么。
function showCommandAsk(detail) {
  showPending([{
    path: '要执行：' + detail.command,
    text: '原因：' + (detail.reason || '未说明') +
          '\\n（点「应用」= 允许这一次；要长期允许，去 .agent/grants.json）'
  }]);
}

function showActions() {
  // 只读壳里没有可点的东西。按钮摆出来、点下去被 403 拒，是骗人：
  // 人只会以为是坏了，然后去关掉那个"看起来多余"的检查（服务端那道才是权威）。
  if (readonly) { return; }
  decided = false;
  actions.style.display = 'flex';
  document.getElementById('apply').disabled = false;
  document.getElementById('reject').disabled = false;
}

// 非回环绑定 = 只读（设计 §3.7）。这一句要说全三件事：为什么、能看什么、
// 想驱动怎么办——只说"403"会让人以为壳坏了。
function showReadonly(state) {
  readonly = Boolean(state.readonly);
  const box = document.getElementById('readonly');
  if (!readonly) {
    box.style.display = 'none';
    box.replaceChildren();
    return;
  }
  const where = state.host ? '（绑在 ' + state.host + ' 上）' : '';
  box.style.display = 'block';
  box.textContent = '这条壳绑在非回环地址上' + where + '：只能看，不能驱动——'
    + '发任务与确认改动会被拒。要驱动它，就在本机开一个网页壳'
    + '（或把 --host 绑回 127.0.0.1）。工作区与会话仍可切换，那只是换"在看谁"。';
}

function onEvent(event) {
  const data = JSON.parse(event.data);
  if (data.type === 'state') { applyState(data); return; }
  // 点下「开始」到服务回话之间按钮还是可点的，再点一次会被拒（409）。
  // 拿事件里的 start 当权威信号，按钮就不会骗人。
  if (data.type === 'start') {
    setRunning(true);
    setThinking(true);
    clearPending();
    return;
  }
  if (data.type === 'step') {
    add('── 第 ' + data.n + ' 步', 'dim');
    return;
  }
  if (data.type === 'tool') {
    // 工具回来就说明模型那一口气过去了，思考指示先灭；下一拍 wait 会再亮。
    setThinking(false);
    add('  ' + data.name + ' → ' + (data.ok ? '成功' : '失败'),
        data.ok ? 'ok' : 'bad');
    return;
  }
  // 系统说给用户听的一句话（比如数字核对的结果）。工具那行只有「成功/失败」，
  // 而这类事件的理由才是全部内容——不显示出来，页面上就只剩一个没有理由的红字。
  if (data.type === 'note') {
    add(data.text, data.ok ? 'dim' : 'bad');
    return;
  }
  // 模型等待心跳（既有协议，见 docs/superpowers/specs/2026-09-21-*）。
  // 这一行**只是"还在生成"**，不占探活横幅：红色 #health 说的是连不上。
  // 同时把「正在思考」点亮——同一件事的两种画法，不新造事件。
  if (data.type === 'wait') {
    setThinking(true);
    var text = WAIT_PREFIX + ' ' + data.elapsed + 's';
    if (data.limit) { text += ' / 上限 ' + data.limit + 's'; }
    add(text, 'dim');
    return;
  }
  if (data.type === 'diff') {
    pendings.push({path: data.path, text: data.text});
    showPending(pendings);
    return;
  }
  if (data.type === 'await') {
    setThinking(false);
    if (data.command) { showCommandAsk(data); }
    showActions();
    return;
  }
  if (data.type === 'confirm') {
    // auto 策略下改动是静默落盘的，界面上必须留一句话，
    // 否则「它自己改了文件」这件事只有翻 git 才知道。
    if (data.auto) {
      add('自动应用 ' + data.count + ' 处改动（策略 auto 且范围内）', 'dim');
    }
    clearPending();
    return;
  }
  if (data.type === 'usage') {
    setMeta(data.steps);
    add('用量：' + (data.steps || 0) + ' 步 · ' + (data.calls || 0) + ' 次调用 · '
        + (data.prompt_tokens || 0) + '+' + (data.completion_tokens || 0) + ' token',
        'dim');
    return;
  }
  if (data.type === 'final') {
    setThinking(false);
    setRunning(false);
    // 失败的结局**不走**"一行原始异常"：那正是"模型连不上"时人看到的东西，
    // 而它没说清下一步。认得出是模型那一步坏的就画成人话（showFailure）。
    if (data.ok) {
      add('完成：' + data.text, 'ok');
    } else {
      showFailure('失败：', data.text, data.error);
    }
    actions.style.display = 'none';
    clearPending();
    return;
  }
  if (data.type === 'error') {
    setThinking(false);
    showFailure('错误：', data.message, data.error);
  }
}

// 事件流只连**一次**。切换目标**不重连**：服务端发现目标变了会退订旧的、
// 订阅新的、并补一帧 `state`（设计 §3.3），页面要做的只是把这一屏重绑到新目标上。
function connect() {
  const source = new EventSource('/events');
  source.onmessage = onEvent;
  source.onerror = () => { conn.style.display = 'block'; };
  source.onopen = () => { conn.style.display = 'none'; };
}

// 一个 POST：成功返回响应体（`/target` 的响应体就是新的完整状态），
// 失败**说人话**——`error` 直接摆到对话区里，不往控制台一丢了事
// （本地单机壳没人开着 devtools，"点了一下什么都没发生"是最坏的那种失败）。
async function post(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const detail = await response.json().catch(() => ({}));
  if (!response.ok) {
    add('请求被拒绝：' + (detail.error || response.status), 'bad');
    return null;
  }
  return detail;
}

// 切换当前目标：`POST /target`。成功后**立刻按返回体重绑**（它就是新的 /state），
// 不等 SSE 补帧——补帧还会来，但页面不该有那一秒的旧内容。
//
// 点了就先亮选中态：回帧是一趟服务端往返，高亮不该落后于点击。探活**不在**那一趟里
// （服务端挪到后台了），所以这一步现在是几十毫秒——见 `web/probe.py`。
async function switchTarget(payload) {
  closeDrawer();
  chosen = wantedKey(payload);
  markChosen();
  const next = await post('/target', payload);
  if (next === null) {
    // 没切成（409/400）：把选中态拨回当前目标，别停在选中态骗人。
    chosen = {root: '', session: ''};
    drawTarget(lastState);
    markChosen();
    return;
  }
  applyState(next);
}

// 这一次点的是谁（用在乐观选中上）。工作区那一列给的是**根路径**（下拉的值就是它）。
function wantedKey(payload) {
  const t = lastState.target || {};
  if (payload.workspace) { return {root: payload.workspace, session: payload.session || session}; }
  if (payload.session) { return {root: t.root || '', session: payload.session}; }
  return {root: '', session: ''};
}

// 「移除」这一段会话：**两次点击**才算数（第一次只是把这一行变成确认态）。
// 服务端把它按归档处理（数据不动、可恢复），正在跑的那一条会被拒。
function requestRemove(value) {
  if (readonly) { return; }
  if (armed !== value) {
    armed = value;
    markArmed();
    return;
  }
  armed = '';
  markArmed();
  act('/session/delete', {session: value}, '已移除会话 ' + value + '（归档：数据没动）');
}

// 「恢复」：把归档过的那一条放回列表（同一个端点，另一个方向）。
function requestRestore(value) {
  if (readonly) { return; }
  act('/session/restore', {session: value}, '已恢复会话 ' + value);
}

// 会话这一类动作的公共落地：成功就按返回体重绑（它就是新的 /state）。
async function act(url, payload, note) {
  const next = await post(url, payload);
  if (next === null) {
    // 被拒（409 正在跑 / 只读 403 / 400 查无此会话）：把确认态收回去，
    // 理由已经由 post() 摆进对话区了。
    armed = '';
    drawTarget(lastState);
    markArmed();
    return;
  }
  armed = '';
  applyState(next);
  add(note, 'dim');
}

// 「新建会话」：会话**按工作区**存，而服务端有了一条真的建会话的路
// （`POST /session/new` → `kernel_sessions.create_session`）。以前这里只做
// "把目标切到一个还没跑过的新名字"：`/target` 不新建会话（`resolve_session`
// 不认识就报错、且**绝不静默新建**），于是顶部那个「＋」点下去只有 400。
//
// 两步是刻意的：① 先让服务端**真把会话建出来**（重名 / 非法名在那里被拦下，
// 说人话）；② 再走**同一条**切换路径（`switchTarget` → `POST /target`）切过去
// ——"切目标"在这条壳上仍然只有一处。点完立刻亮：`chosen` 先点亮那一行，
// 不等下一轮 `/state`（也不必等，`/target` 的响应体就是新的完整状态）。
async function newSession() {
  if (readonly || (lastState.target || {}).pinned) { return; }
  const name = window.prompt('新会话的名字（字母 / 数字 / 横线）', 'web-' + Date.now());
  if (name === null) { return; }
  const clean = String(name).trim();
  if (!clean) { return; }
  const made = await post('/session/new', {session: clean});
  if (made === null) { return; }
  // `await`：这一趟做完（回帧已经重绑到新会话）才算"新建完了"，调用方
  // （与 `tests/web/page_dom.js` 那个假 DOM）据此判断页面有没有切过去。
  await switchTarget({session: made.session});
}

async function say() {
  if (readonly) { return; }
  const goal = goalBox.value.trim();
  if (!goal || busy) { return; }
  closeDrawer();
  goalBox.value = '';
  autoGrow();
  add('▶ ' + goal, 'me');
  await post('/run', {goal});
}

// Enter 发送 / Shift+Enter 换行。
function onKeydown(event) {
  if (event.key !== 'Enter') { return; }
  if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) { return; }
  event.preventDefault();
  say();
}

function autoGrow() {
  goalBox.style.height = 'auto';
  goalBox.style.height = Math.max(26, Math.min(goalBox.scrollHeight, 190)) + 'px';
}

function openDrawer() { document.body.classList.add('drawer'); scrim.classList.add('show'); }
function closeDrawer() { document.body.classList.remove('drawer'); scrim.classList.remove('show'); }

document.getElementById('menu').innerHTML = ICON.menu;
sendBtn.innerHTML = ICON.send;
toBottomBtn.innerHTML = ICON.down;

// 两条原生控件是"切目标"的唯一入口（左栏的行经 `target()` 落到它们身上）。
// 守卫盯着**当前目标**：派发 change 不会把同一个目标再切一次，也不会在
// 切换失败（409/400）之后被`drawTarget` 拨回时**再发一次**请求。
wsBox.addEventListener('change', function () {
  if (wsBox.value !== (lastState.target || {}).root) {
    switchTarget({workspace: wsBox.value});
  }
});
sessBox.addEventListener('change', function () {
  if (sessBox.value !== (lastState.target || {}).session) {
    switchTarget({session: sessBox.value});
  }
});
newBtn.onclick = newSession;
sendBtn.onclick = say;
goalBox.oninput = autoGrow;
goalBox.onkeydown = onKeydown;
stream.onscroll = updateToBottom;
toBottomBtn.onclick = function () {
  scrollDown();
  updateToBottom();
};
document.getElementById('menu').onclick = function () {
  if (document.body.classList.contains('drawer')) { closeDrawer(); } else { openDrawer(); }
};
scrim.onclick = closeDrawer;

function decide(apply) {
  // 点一次就禁用：重复写 stdin 会让后续的 input() 拿到意外的输入。
  if (decided) { return; }
  decided = true;
  document.getElementById('apply').disabled = true;
  document.getElementById('reject').disabled = true;
  post('/confirm', {apply});
}

document.getElementById('apply').onclick = () => decide(true);
document.getElementById('reject').onclick = () => decide(false);

buildColumn();
syncSend();
connect();
setInterval(refreshBanners, 3000);
</script>
</body>
</html>
"""
