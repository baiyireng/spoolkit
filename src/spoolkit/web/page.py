"""内嵌的单页 HTML。

做成 Python 字符串而不是外置文件：跑起来只有一个东西要分发，也避免
打包时漏文件。代价是编辑时没有语法高亮，可以接受。

这里刻意做得很薄——不持有状态、不做业务判断，只渲染事件和发三个请求。
前端出错只能靠人看出来，所以它必须小到一眼能看完。

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
</style>
</head>
<body>
<header>
  <span class="meta" id="meta">连接中…</span>
  <input id="goal" placeholder="要 agent 做什么…">
  <button id="start">开始</button>
</header>
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
let running = false, decided = false;
let session = 'cli';
let pendings = [];
let historyDrawn = false;

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
  startBtn.disabled = value;
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
  session = state.session || session;
  const usage = state.usage || {};
  setMeta(usage.steps);
  setRunning(state.running);
  // 断线重连时事件已经漏掉了，只能按快照把待确认面板整个重建出来。
  pendings = state.diffs || [];
  if (pendings.length) { showPending(pendings); }
  if (state.awaiting) { showActions(); } else { actions.style.display = 'none'; }
  drawHistory(state.messages);
}

function showActions() {
  decided = false;
  actions.style.display = 'flex';
  document.getElementById('apply').disabled = false;
  document.getElementById('reject').disabled = false;
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
  if (data.type === 'await') { showActions(); return; }
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

function connect() {
  const source = new EventSource('/events');
  source.onmessage = onEvent;
  source.onerror = () => { conn.style.display = 'block'; };
  source.onopen = () => { conn.style.display = 'none'; };
}

async function post(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    add('请求被拒绝：' + (detail.error || response.status), 'bad');
  }
}

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
</script>
</body>
</html>
"""
