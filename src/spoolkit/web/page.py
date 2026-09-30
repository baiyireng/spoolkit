"""内嵌的单页 HTML。

做成 Python 字符串而不是外置文件：跑起来只有一个东西要分发，也避免
打包时漏文件。代价是编辑时没有语法高亮，可以接受。

这里刻意做得很薄——不持有状态、不做业务判断，只渲染事件、发四个请求
（`/run`、`/confirm`、`/target`，以及轮 `/state` 取横幅），
以及把快照里的待批准配对码、模型通断、当前目标和"别处还有谁在跑"画出来。
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
  一个假装选中的行上。
- **运行中把发送降级成禁用**：服务端只有 `/run` 与 `/confirm`，没有停止端点
  （`Runner.stop()` 没有对应接口）。所以这里**不摆一个点了就骗人的停止键**，
  而是在横幅上说明"等服务端收尾再发下一条"——本轮不做停止，也不新开端点。
- **流式输出默认跟到底，人往上翻就不抢**：`stick` 在**改动 DOM 之前**读，
  翻上去读到一半不会被新事件弹回底部；那时右下角浮出「回到最新」。
- **"正在思考"由既有的 `wait` / `start` / `final` 驱动**，不新造协议：
  `wait` 心跳（模型生成中）亮，`final` 灭。

主题：默认深色，跟随 `prefers-color-scheme`；颜色全在 `:root` 的那张变量表里，
外加一条浅色的 `@media (prefers-color-scheme: light)` 覆盖，改一处就整体换掉。
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
    --code-bg:#000;
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
  .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
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
  .msg { display:flex; flex-direction:column; }
  .msg.user { align-self:flex-end; max-width:84%; align-items:flex-end; }
  .msg.assistant { align-self:flex-start; max-width:100%; }
  .bubble { padding:9px 14px; border-radius:14px; white-space:pre-wrap;
            overflow-wrap:anywhere; word-break:break-word; }
  .msg.user .bubble { background:var(--bubble-user); border-bottom-right-radius:4px; }
  .msg.assistant .bubble { background:var(--bubble-assistant);
                           border:1px solid var(--line-soft);
                           border-bottom-left-radius:4px; }
  .msg.system { align-self:flex-start; max-width:100%; }
  .sys-line { color:var(--text-dim); font-size:13px; padding-left:4px;
              white-space:pre-wrap; overflow-wrap:anywhere; }
  .sys-line.bad { color:var(--bad); }
  .sys-line.ok { color:var(--ok); }

  /* 代码块：等宽 + 一键复制 */
  .code { margin:10px 0 0; border:1px solid var(--line); border-radius:10px;
          overflow:hidden; background:var(--code-bg); }
  .code-bar { display:flex; align-items:center; justify-content:space-between;
              gap:8px; padding:4px 8px 4px 12px; background:var(--panel-2);
              color:var(--text-dim); font:12px/1.6 var(--mono);
              border-bottom:1px solid var(--line); }
  .copy-btn { padding:3px 10px; border-radius:7px; border:1px solid var(--line);
              background:transparent; color:var(--text-dim); cursor:pointer;
              font:12px/1.6 var(--sans); }
  .copy-btn:hover { color:var(--text); }
  .code pre { margin:0; padding:10px 12px; overflow-x:auto;
              font:13px/1.55 var(--mono); color:var(--text); }
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
let historyDrawn = false;
let busy = false;
// 页面上当前绑定的目标（"工作区|会话"）。它变了就说明这一屏整体属于上一个目标，
// 得清掉重绑——只更新横幅是不够的。
let bound = '';
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

// 助手的话里可能有代码块（``` 围起来）。用 textContent 而不是 innerHTML：
// 模型输出是不可信输入，拼标签等于把页面交给它。
function fillRich(bubble, content) {
  const text = String(content === null || content === undefined ? '' : content);
  for (const block of text.split('```')) {
    const raw = block.replace(/^\\r?\\n/, '').replace(/\\r?\\n$/, '');
    if (raw === '') { continue; }
    const lines = raw.split('\\n');
    const first = lines[0].trim();
    const fenced = lines.length !== 1 && first !== '' && first.indexOf(' ') === -1;
    const code = document.createElement('div');
    code.className = 'code';
    const bar = document.createElement('div');
    bar.className = 'code-bar';
    const label = document.createElement('span');
    label.textContent = fenced ? first : '代码';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'copy-btn';
    copy.textContent = '复制';
    const payload = fenced ? lines.slice(1).join('\\n') : raw;
    copy.onclick = function () { copyText(payload, copy); };
    bar.appendChild(label);
    bar.appendChild(copy);
    const pre = document.createElement('pre');
    const codeEl = document.createElement('code');
    codeEl.textContent = payload;
    pre.appendChild(codeEl);
    code.appendChild(bar);
    code.appendChild(pre);
    bubble.appendChild(code);
  }
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

function drawHistory(messages) {
  if (historyDrawn || !messages || !messages.length) { return; }
  const stick = atBottom();
  historyDrawn = true;
  const sep = document.createElement('div');
  sep.className = 'history-sep';
  sep.textContent = '之前的往来（给人看的，不进模型上下文）';
  appendNode(sep);
  for (const item of messages) {
    const bubble = makeBubble(
      item.role === 'user' ? 'user' : 'assistant',
      ''
    );
    fillRich(bubble, item.content);
  }
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
  sessBox.dispatchEvent(new Event('change'));
}

// 左栏的一行。点它就是切目标（经 `target()` 落到原生控件上）。
function row(label, title, on, onClick) {
  const line = document.createElement('div');
  line.className = 'li grp-name';
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
  line.onclick = onClick;
  return line;
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

function renderSidebar(wrows, srows, key, locked) {
  const live = runningRows(lastState);
  const here = (lastState.target || {}).root || '';
  workspaceList.replaceChildren();
  sessionList.replaceChildren();
  const head = document.createElement('div');
  head.className = 'grp-name dim';
  head.textContent = '工作区';
  workspaceList.appendChild(head);
  for (const one of wrows) {
    if (one.value === '') { continue; }
    const line = row(
      one.name + ((one.ready === false) ? '（没 init）' : ''),
      '',
      (one.value + '|' + session) === key,
      (function (value) {
        return function () { target(value, ''); };
      })(one.value)
    );
    markRunning(line, Boolean(live[one.value + '|' + session]));
    workspaceList.appendChild(line);
  }
  if (!workspaceList.querySelector('.li')) {
    const none = document.createElement('div');
    none.className = 'empty-line';
    none.textContent = '（登记表是空的：先在工作区目录里敲 spool init）';
    workspaceList.appendChild(none);
  }
  // 钉住（--root）时两段列表都只读：`LOCK` 走 CSS 的 pointer-events，
  // 行本身还是画出来（人要看得到"这里有哪几条"），只是点不动。
  workspaceList.classList.toggle(LOCK, locked);
  sessionList.classList.toggle(LOCK, locked);
  const shead = document.createElement('div');
  shead.className = 'grp-name dim';
  shead.textContent = '会话';
  sessionList.appendChild(shead);
  for (const one of srows) {
    if (one.value === '') { continue; }
    const line = row(
      one.text,
      '',
      one.value === session,
      (function (value) {
        return function () { target('', value); };
      })(one.value)
    );
    // 「正在跑」的键是 (工作区, 会话)：别的会话在跑也要标出来
    // （切走不杀任务，见设计 §3.4）。
    markRunning(line, Boolean(live[keyFor(here, one.value)]));
    sessionList.appendChild(line);
  }
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

// 重绑这一屏：目标换了，旧的输出、待确认面板、历史标记都属于**上一个**目标。
function resetView() {
  stream.replaceChildren();
  historyDrawn = false;
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

function showHealth(health) {
  const box = document.getElementById('health');
  const bits = [];
  const model = (health && health.model) || {};
  if (model.ok === false) {
    const where = model.target ? '（' + model.target + '）' : '';
    bits.push('模型连不上' + where + '：' + (model.detail || '连不上'));
  }
  const services = (health && health.services) || [];
  for (const item of services) {
    if (item.ok === false) {
      const where = item.target ? '（' + item.target + '）' : '';
      bits.push((item.name || '外接服务') + '连不上' + where
        + '：' + (item.detail || '连不上'));
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
    add(data.ok ? '完成：' + data.text : '失败：' + data.text,
        data.ok ? 'ok' : 'bad');
    actions.style.display = 'none';
    clearPending();
    return;
  }
  if (data.type === 'error') {
    setThinking(false);
    add('错误：' + data.message, 'bad');
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
async function switchTarget(payload) {
  closeDrawer();
  const next = await post('/target', payload);
  if (next === null) {
    // 没切成（409/400）：把选择栏拨回当前目标，别停在选中态骗人。
    drawTarget(lastState);
    return;
  }
  applyState(next);
}

// 「新建会话」：会话是**按工作区**存的，服务端没有"建会话"端点——会话在第一次
// 真跑起来时才落进会话表（见 web/server.py::_session_rows 的自述）。所以这里
// 只做一件事：把目标切到一个**还没跑过的新名字**上，页面上就是一条空会话。
function newSession() {
  if (readonly || (lastState.target || {}).pinned) { return; }
  const name = window.prompt('新会话的名字（字母 / 数字 / 横线）', 'web-' + Date.now());
  if (name === null) { return; }
  const clean = String(name).trim();
  if (!clean) { return; }
  switchTarget({session: clean});
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
