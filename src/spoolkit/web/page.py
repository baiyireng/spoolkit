"""内嵌的单页 HTML。

做成 Python 字符串而不是外置文件：跑起来只有一个东西要分发，也避免
打包时漏文件。代价是编辑时没有语法高亮，可以接受。

这里刻意做得很薄——不持有状态、不做业务判断，只渲染事件、发四个请求
（`/run`、`/confirm`、`/target`，以及轮 `/state` 取横幅），
以及把快照里的待批准配对码、模型通断、当前目标和"别处还有谁在跑"画出来。
配对和探活都不进模型上下文，也不能点「应用/拒绝」来批准或重连——那两个按钮是落盘确认。
前端出错只能靠人看出来，所以它必须小到一眼能看完。

顶上那条**目标选择栏**（工作区 / 会话两条原生 `select` + 当前目标全路径）是机器级壳的
门面：一个进程服务整台机器，"我在哪个工作区"必须一眼看得出（批次一踩过绑错工作区、
把 `__init__.py` 写空的坑）。切换走 `POST /target`，成功后**整屏重绑**；事件流不重连
（服务端会补一帧 `state`），但那一帧同样要重画目标与历史，不是只更新横幅。

布局是左右分栏：右侧的待确认面板固定，不随输出滚走。要判断的时候
视线不用移动。
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spool</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.6 ui-monospace,Consolas,monospace;
         background:#0e1116; color:#c9d1d9; height:100vh;
         display:flex; flex-direction:column; }
  header { display:flex; gap:8px; align-items:center; padding:10px 14px;
           border-bottom:1px solid #232a35; background:#131820; }
  header .meta { color:#8b949e; white-space:nowrap; }
  header input { flex:1; padding:7px 10px; border-radius:6px;
                 border:1px solid #30363d; background:#0d1117; color:inherit; }
  header button { padding:7px 16px; border-radius:6px; border:0;
                  background:#2f81f7; color:#fff; cursor:pointer; }
  header button:disabled { background:#21262d; color:#6e7681; cursor:default; }
  #bar { display:flex; gap:8px; align-items:center; padding:8px 14px;
         border-bottom:1px solid #232a35; background:#0f141c; flex-wrap:wrap; }
  #bar select { padding:6px 8px; border-radius:6px; border:1px solid #30363d;
                background:#0d1117; color:inherit; max-width:38ch; }
  #bar select:disabled { color:#6e7681; background:#131820; }
  #where { color:#79c0ff; font-weight:700; word-break:break-all; }
  #where.empty { color:#8b949e; font-weight:400; }
  #target-note { display:none; padding:8px 14px; background:#10202f; color:#9fd0ff;
                 border-bottom:1px solid #1d3a54; }
  #elsewhere { display:none; padding:8px 14px; background:#2a2410; color:#e8d38a;
               border-bottom:1px solid #4a4020; }
  main { flex:1; display:flex; min-height:0; }
  #stream { flex:1.2; overflow-y:auto; padding:12px 14px; }
  #side { flex:1; border-left:1px solid #232a35; background:#101722;
          padding:12px 14px; overflow-y:auto; }
  .line { white-space:pre-wrap; word-break:break-word; }
  .ok { color:#7fa650; } .bad { color:#c96a6a; } .dim { color:#8b949e; }
  .diff { border:1px solid #30363d; border-radius:6px; padding:8px;
          margin:8px 0; background:#0d1117; overflow-x:auto; }
  .diff .add { color:#7fa650; } .diff .del { color:#c96a6a; }
  #actions { display:none; gap:8px; margin-top:10px; }
  #actions button { padding:6px 14px; border-radius:6px; border:0;
                    cursor:pointer; }
  .history { border-bottom:1px solid #232a35; padding-bottom:8px;
             margin-bottom:10px; }
  .me { color:#79c0ff; }
  #apply { background:#238636; color:#fff; }
  #reject { background:#30363d; color:#c9d1d9; }
  #conn { position:fixed; right:12px; bottom:10px; color:#c96a6a;
          display:none; }
  #pairing { display:none; padding:8px 14px; background:#3d2a00; color:#f0c674;
             border-bottom:1px solid #5c4300; }
  #pairing .code { font-weight:700; letter-spacing:0.06em; }
  #health { display:none; padding:8px 14px; background:#3d1515; color:#f0a0a0;
            border-bottom:1px solid #5c2020; }
  #readonly { display:none; padding:8px 14px; background:#2a1f3d; color:#d0bfff;
              border-bottom:1px solid #443066; }
</style>
</head>
<body>
<div id="readonly"></div>
<div id="bar">
  <span class="meta">工作区</span>
  <select id="ws"></select>
  <span class="meta">会话</span>
  <select id="sess"></select>
  <span id="where" class="where empty">连接中…</span>
</div>
<div id="target-note"></div>
<div id="elsewhere"></div>
<header>
  <span class="meta" id="meta">连接中…</span>
  <input id="goal" placeholder="要 agent 做什么…">
  <button id="start">开始</button>
</header>
<div id="health"></div>
<div id="pairing"></div>
<main>
  <div id="stream"></div>
  <div id="side">
    <div id="pending" class="dim">当前没有待确认的改动。</div>
    <div id="actions">
      <button id="apply">应用</button>
      <button id="reject">拒绝</button>
    </div>
  </div>
</main>
<div id="conn">与服务的连接断开了，重连中…</div>
<script>
const stream = document.getElementById('stream');
const pending = document.getElementById('pending');
const actions = document.getElementById('actions');
const meta = document.getElementById('meta');
const conn = document.getElementById('conn');
const startBtn = document.getElementById('start');
const wsBox = document.getElementById('ws');
const sessBox = document.getElementById('sess');
const whereBox = document.getElementById('where');
const noteBox = document.getElementById('target-note');
const elseBox = document.getElementById('elsewhere');
let running = false, decided = false;
// 这条壳绑在非回环地址上时，服务端只放行"看"：/run 与 /confirm 一律 403，
// 页面必须在**点之前**说出来（点一下被拒是最后一秒才告诉人）。
let readonly = false;
let session = 'cli';
let pendings = [];
let historyDrawn = false;
// 页面上当前绑定的目标（"工作区|会话"）。它变了就说明这一屏整体属于上一个目标，
// 得清掉重绑——只更新横幅是不够的。
let bound = '';
// 最近一帧状态：切换失败时用它把选择栏拨回当前目标，别停在选中态骗人。
let lastState = {};

function add(text, cls) {
  const div = document.createElement('div');
  div.className = 'line ' + (cls || '');
  div.textContent = text;
  stream.appendChild(div);
  stream.scrollTop = stream.scrollHeight;
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

// ── 目标选择栏：工作区 / 会话两条下拉 + 当前目标全路径 ──
//
// 「我在哪个工作区」必须一眼看得出：批次一踩过绑错工作区、把 __init__.py 写空的坑，
// 而机器级壳服务的是整台机器（设计 §3.3）。所以全路径单独一处、加粗、不截断，
// 每次状态帧都按 `target` 重画——不能只更新横幅。
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

function drawTarget(state) {
  const t = state.target || {};
  const key = (t.root || '') + '|' + (t.session || '');
  // 第一次画不算"变了"：那时页面上本来就什么都没有，清一遍只会白闪一下。
  const changed = bound !== '' && key !== bound;
  bound = key;

  const workspaces = state.workspaces || [];
  const rows = [{value: '', text: '（选一个工作区）'}];
  for (const row of workspaces) {
    rows.push({value: row.root, text: (row.name || row.root) + '  —  ' + row.root});
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

  // `--root` 起的壳只有一个目标：选择栏禁用，并把原因写出来
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

  const notes = [];
  if (locked) {
    notes.push('这条壳是用 --root 起的：只有这一个目标，切不了。'
      + '去掉 --root 重启就是机器级。');
  }
  if (!t.root) {
    // 空态要**引导人选**，不是摆一堆空白：候选清单就在上面那条工作区下拉里。
    // 但下拉也可能是**空的**（一个工作区都没登记过）——那时说"在上面选一个"就是死路，
    // 所以补一句怎么造出候选来（评审逮到的就是这条）。
    notes.push(t.note || '还没有选工作区：在上面的「工作区」下拉里选一个'
      + '（下拉是空的就先在工作区目录里敲 spool init）。');
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
}

function showPending(items) {
  pending.innerHTML = '';
  pending.className = '';
  for (const item of items) { pending.appendChild(diffBox(item.path, item.text)); }
}

// 会话历史：**给人看的**。它不进模型上下文——页面上看到多少往来，
// 和模型这一步看到多少，是两件事（后者由状态与记忆按需取）。
function drawHistory(messages) {
  if (historyDrawn || !messages || !messages.length) { return; }
  historyDrawn = true;
  const box = document.createElement('div');
  box.className = 'history';
  const head = document.createElement('div');
  head.className = 'dim';
  head.textContent = '── 这个会话之前的往来（给人看的，不进模型上下文）──';
  box.appendChild(head);
  for (const item of messages) {
    const div = document.createElement('div');
    div.className = 'line ' + (item.role === 'user' ? 'me' : 'dim');
    div.textContent = (item.role === 'user' ? '你：' : '助手：') + item.content;
    box.appendChild(div);
  }
  stream.insertBefore(box, stream.firstChild);
}

function setRunning(value) {
  running = value;
  startBtn.disabled = value || readonly;
  startBtn.textContent = value ? '运行中…' : '开始';
}

function setMeta(steps) {
  meta.textContent = session + ' · ' + (steps || 0) + ' 步';
}

function clearPending() {
  pendings = [];
  pending.className = 'dim';
  pending.textContent = '当前没有待确认的改动。';
}

function applyState(state) {
  lastState = state;
  // 目标先画：它变了就**整屏重绑**（服务端在目标变了时会补一帧 state，
  // 那一帧也要走这条重绑，不能只更新横幅）。
  if (drawTarget(state)) { resetView(); }
  // 只读必须在 setRunning / showActions **之前**定下来：那两处都按它决定按钮长什么样。
  showReadonly(state);
  const target = state.target || {};
  session = target.session || state.session || session;
  const usage = state.usage || {};
  setMeta(usage.steps);
  setRunning(state.running);
  // 断线重连时事件已经漏掉了，只能按快照把待确认面板整个重建出来。
  pendings = state.diffs || [];
  if (pendings.length) { showPending(pendings); }
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
  if (data.type === 'start') { setRunning(true); clearPending(); return; }
  if (data.type === 'step') { add('── 第 ' + data.n + ' 步', 'dim'); return; }
  if (data.type === 'tool') {
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
  if (data.type === 'diff') {
    pendings.push({path: data.path, text: data.text});
    showPending(pendings);
    return;
  }
  if (data.type === 'await') {
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
    add(data.ok ? '完成：' + data.text : '失败：' + data.text,
        data.ok ? 'ok' : 'bad');
    setRunning(false);
    actions.style.display = 'none';
    clearPending();
    return;
  }
  if (data.type === 'error') { add('错误：' + data.message, 'bad'); }
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
  const next = await post('/target', payload);
  if (next === null) {
    // 没切成（409/400）：把选择栏拨回当前目标，别停在选中态骗人。
    drawTarget(lastState);
    return;
  }
  applyState(next);
}

wsBox.onchange = () => switchTarget({workspace: wsBox.value});
sessBox.onchange = () => switchTarget({session: sessBox.value});

startBtn.onclick = () => {
  const goal = document.getElementById('goal').value.trim();
  if (!goal || running) { return; }
  add('▶ ' + goal);
  post('/run', {goal});
};

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

connect();
setInterval(refreshBanners, 3000);
</script>
</body>
</html>
"""
