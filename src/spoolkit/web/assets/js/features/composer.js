// spool web · js/features/composer.js
import "../core/dom.js";
import "../core/store.js";
import "../core/transport.js";
function createComposer(ctx) {
function setRunning(value) {
  const was = ctx.store.get('busy');
  const fields = {running: value, busy: value};
  if (!value) { fields.stopping = false; }
  ctx.store.patch(fields);
  goalBox.readOnly = value || ctx.store.get('readonly');
  syncSend();
  setThinking(value && ctx.store.get('thinking'));
  // 运行中那句提示在状态详情里（见 drawTarget）：这一格变了就要重画一次，
  // 否则它会停在上一帧——"跑完了还写着正在跑"比不写还坏。
  if (was !== ctx.store.get('busy') && ctx.store.get('lastState') && ctx.store.get('lastState').target) { ctx.sidebar.drawTarget(ctx.store.get('lastState')); }
}

// 那一枚圆键的三态：空闲=发送（箭头），运行中=**停止**（方块，可点），
// 停止请求已发出=正在停止…（禁用一下，免得连点发两次）。只读壳里它一律禁用。
//
// 为什么不是"运行中禁用 + 说明"：服务端现在有停止入口了（`POST /run/stop`，
// 复用 `Runner.stop()` 的温和停止语义），再摆一个灰键没有意义。
function syncSend() {
  const mode = ctx.store.get('busy') ? (ctx.store.get('stopping') ? 'stopping' : 'stop') : 'send';
  sendBtn.disabled = ctx.store.get('stopping') || ctx.store.get('readonly');
  sendBtn.dataset.mode = mode;
  sendBtn.title = ctx.store.get('readonly') ? '只读：这条壳绑在非回环地址上，写请求会被拒'
    : ctx.store.get('busy') ? (ctx.store.get('stopping') ? '停止中' : '停止本轮（已产出的内容保留）')
    : '发送';
  // 按钮里只有一个图标：读屏得有个名字（只给 title 是不够稳的）。
  sendBtn.setAttribute('aria-label', sendBtn.title);
  const icon = mode === 'send' ? ICON.send : ICON.stop;
  // 图标只在**换了那一个**的时候重写：`innerHTML` 每次都写会让按钮闪一下。
  if (sendBtn._icon !== icon) { sendBtn.innerHTML = icon; sendBtn._icon = icon; }
}

// 点那一枚键：空闲时是发送，运行中就是**停止**。
// 返回值就是那一条 POST 的 promise：调用方（与假 DOM 那一层）要能 `await` 到
// "请求真的发出去了、回话真的到了"，而不是只看按钮上那一瞬间的样子。
function onSendClick() {
  if (ctx.store.get('busy')) { return requestStop(); }
  return say();
}

// 手动中断这一轮：`POST /run/stop` → 服务端温和地收掉子进程（已产出的内容保留）。
// 点了立刻有反馈（按钮进"正在停止…"），真正回到可发送由 `final` 事件或下一拍
// `/state` 的 `running` 落回假决定；所以这里**不等**它，也不假装已经停了。
async function requestStop() {
  if (ctx.store.get('readonly') || ctx.store.get('stopping')) { return; }
  ctx.store.set('stopping', true);
  syncSend();
  const body = await ctx.transport.post('/run/stop', {});
  if (body === null) {
    // 被拒（409 没有在跑 / 403 只读）：理由已经由 post() 摆进对话区与通知里了。
    ctx.store.set('stopping', false);
    syncSend();
    return;
  }
  ctx.store.set('stoppedRun', true);
  ctx.thread.add('已请求停止本轮：服务端正在收尾，已产出的内容保留。', 'dim');
}

// 「正在思考」：由既有的 wait / start / final 驱动，不新造协议。
function setThinking(value) {
  ctx.store.set('thinking', Boolean(value));
  thinkingBox.classList.toggle('show', ctx.store.get('thinking') && !ctx.store.get('readonly'));
  syncSend();
}

function setMeta(steps) {
  meta.textContent = ctx.sessions.sessionTitle() + ' · ' + (steps || 0) + ' 步';
}

// 草稿按「工作区|会话」分开。内存是当前页的权威，sessionStorage 只为刷新留一份。
// 存储不可用或超限时静默退回内存，不把草稿发给服务端。
let shownKey = '';
const DRAFT_KEPT = '草稿还在输入框里，可以重试';
const DRAFT_STORAGE_PREFIX = 'spool:draft:';

function currentKey() {
  const target = (ctx.store.get('lastState') || {}).target || {};
  const root = target.root || '';
  const name = target.session || ctx.store.get('session') || '';
  if (!root && !name) { return ''; }
  return keyFor(root, name);
}

function draftStorageName(key) {
  return DRAFT_STORAGE_PREFIX + key;
}

function readStoredDraft(key) {
  try {
    if (typeof sessionStorage === 'undefined' || !sessionStorage || typeof sessionStorage.getItem !== 'function') {
      return null;
    }
    const value = sessionStorage.getItem(draftStorageName(key));
    if (value === null || value === undefined) { return null; }
    return String(value);
  } catch (err) {
    return null;
  }
}

function syncStoredDraft(key, text) {
  try {
    if (typeof sessionStorage === 'undefined' || !sessionStorage) { return; }
    const name = draftStorageName(key);
    if (text) {
      if (typeof sessionStorage.setItem === 'function') { sessionStorage.setItem(name, text); }
    } else if (typeof sessionStorage.removeItem === 'function') {
      sessionStorage.removeItem(name);
    }
  } catch (err) {
    // 配额满或隐私模式：这一份留在内存里，输入不受影响。
  }
}

function ensureStoredDraft(key) {
  const prev = ctx.store.get('drafts') || {};
  if (Object.prototype.hasOwnProperty.call(prev, key)) { return; }
  const stored = readStoredDraft(key);
  if (!stored) { return; }
  const drafts = Object.assign({}, prev);
  drafts[key] = stored;
  ctx.store.set('drafts', drafts);
}

function writeDraft(key, text) {
  if (!key) { return; }
  const prev = ctx.store.get('drafts') || {};
  const nextText = String(text || '');
  const had = Object.prototype.hasOwnProperty.call(prev, key);
  if (nextText) {
    if (!had || prev[key] !== nextText) {
      const drafts = Object.assign({}, prev);
      drafts[key] = nextText;
      ctx.store.set('drafts', drafts);
    }
    syncStoredDraft(key, nextText);
    return;
  }
  if (!had) { return; }
  const drafts = Object.assign({}, prev);
  delete drafts[key];
  ctx.store.set('drafts', drafts);
  syncStoredDraft(key, '');
}

function rememberDraft() {
  writeDraft(currentKey(), goalBox.value);
}

function restoreDraft() {
  const key = currentKey();
  if (!key || key === shownKey) { return; }
  // 输入框里还有字才顺手记下：乐观清空（还没成功）时不要用空串把草稿抹掉。
  if (shownKey && goalBox.value !== '') { writeDraft(shownKey, goalBox.value); }
  const leaving = shownKey;
  shownKey = key;
  ensureStoredDraft(key);
  const drafts = ctx.store.get('drafts') || {};
  if (Object.prototype.hasOwnProperty.call(drafts, key)) {
    const next = String(drafts[key] || '');
    if (goalBox.value !== next) {
      goalBox.value = next;
      autoGrow();
    }
    return;
  }
  if (leaving === '') {
    if (goalBox.value) { writeDraft(key, goalBox.value); }
    return;
  }
  if (goalBox.value !== '') {
    goalBox.value = '';
    autoGrow();
  }
}

function noteDraftKept() {
  ctx.thread.add(DRAFT_KEPT, 'dim');
}

function clearUnsent(bubble) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || !wrap.classList) { return; }
  wrap.classList.remove('failed');
  wrap._retrying = false;
  const kids = wrap.children || [];
  for (let index = kids.length - 1; index >= 0; index -= 1) {
    const cls = String(kids[index].className || '');
    if (cls.indexOf('msg-retry') >= 0 || cls.indexOf('unsent') >= 0) {
      wrap.removeChild(kids[index]);
    }
  }
}

function markUnsent(bubble, goal) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || !wrap.classList) { return; }
  wrap.classList.add('failed');
  let noted = false;
  let button = null;
  const kids = wrap.children || [];
  for (let index = 0; index < kids.length; index += 1) {
    const cls = String(kids[index].className || '');
    if (cls.indexOf('unsent') >= 0) { noted = true; }
    if (cls.indexOf('msg-retry') >= 0) { button = kids[index]; }
  }
  if (!noted) {
    const note = document.createElement('div');
    note.className = 'unsent';
    note.textContent = '未发送';
    wrap.appendChild(note);
  }
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'msg-retry';
    button.textContent = '重试';
    wrap.appendChild(button);
  }
  button.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    return retrySend(bubble, goal);
  };
}

async function retrySend(bubble, goal) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || wrap._retrying) { return; }
  wrap._retrying = true;
  const button = wrap.querySelector ? wrap.querySelector('.msg-retry') : null;
  if (button) { button.disabled = true; }
  const sent = await ctx.transport.post('/run', {goal: goal});
  wrap._retrying = false;
  if (button) { button.disabled = false; }
  if (!sent) { return; }
  if (sent.title) {
    ctx.sessions.applySessionTitle(ctx.store.get('session'), sent.title, sent.title_source || 'seed');
  }
  clearUnsent(bubble);
  ctx.thread.setPendingUser(goal);
  if (currentKey() && goalBox.value.trim() === goal) {
    goalBox.value = '';
    autoGrow();
    writeDraft(currentKey(), '');
  }
}

async function say() {
  if (ctx.store.get('readonly')) { return; }
  const original = goalBox.value;
  const goal = original.trim();
  if (!goal || ctx.store.get('busy')) { return; }
  const key = currentKey();
  ctx.sidebar.closeDrawer();
  goalBox.value = '';
  autoGrow();
  // 自己说的话就是自己说的：气泡底色 + 右对齐已经是"这条是你发的"。
  // 这一行原来在 goal 前面拼了一个黑色三角形符号——它什么也没多说，
  // 所以没了，也没有换成别的符号。
  const bubble = ctx.thread.add(goal, 'me');
  // 这一条已经画出来了；等 `/state` 把它送回来时（子进程开工第一件事就是写库）
  // 要认出来是同一句，不许画第二遍。失败则清掉，避免下一帧把没发出去的话认领掉。
  ctx.thread.setPendingUser(goal);
  const sent = await ctx.transport.post('/run', {goal});
  // 服务端在 /run 里先给一个种子标题；本地立刻换上，左栏不必等下一帧 state。
  if (sent && sent.title) {
    ctx.sessions.applySessionTitle(ctx.store.get('session'), sent.title, sent.title_source || 'seed');
  }
  if (sent) {
    if (currentKey() === key && goalBox.value === '') { writeDraft(key, ''); }
    return;
  }
  ctx.thread.setPendingUser('');
  markUnsent(bubble, goal);
  if (currentKey() !== key) {
    writeDraft(key, original);
    return;
  }
  if (goalBox.value === '') {
    goalBox.value = original;
    autoGrow();
    writeDraft(key, original);
    focus(goalBox);
  }
  noteDraftKept();
}

// Enter 发送 / Shift+Enter 换行 / Esc 收起状态详情。
function onKeydown(event) {
  if (event.key === 'Escape') { ctx.status.setStatusOpen(false); return; }
  if (event.key !== 'Enter') { return; }
  if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) { return; }
  event.preventDefault();
  say();
}

function autoGrow() {
  goalBox.style.height = 'auto';
  goalBox.style.height = Math.max(26, Math.min(goalBox.scrollHeight, 190)) + 'px';
}

function setGoal(value) {
  goalBox.value = String(value || '');
  autoGrow();
  rememberDraft();
  focus(goalBox);
}

function useExample(value) {
  setGoal(value);
}

let emptyNode = null;

function stillMounted(node) {
  let cur = node;
  while (cur) {
    if (cur === stream) { return true; }
    const parent = cur.parentNode;
    if (!parent || (parent.children || []).indexOf(cur) < 0) { return false; }
    cur = parent;
  }
  return false;
}

function contentOnStream() {
  const col = stream.querySelector('.col');
  if (!col) { return false; }
  const kids = col.children || [];
  for (let index = 0; index < kids.length; index += 1) {
    if (stillMounted(kids[index])) { return true; }
  }
  return false;
}

function workspaceName(state) {
  const target = (state && state.target) || {};
  const rows = (state && state.workspaces) || [];
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].root === target.root && rows[index].name) { return rows[index].name; }
  }
  return target.name || target.root || '';
}

function sessionLabel(state) {
  const target = (state && state.target) || {};
  if (target.session_title) { return String(target.session_title); }
  const rows = (state && state.sessions) || [];
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].id === target.session && rows[index].title) { return rows[index].title; }
  }
  return target.session || '';
}

function dropEmpty() {
  if (stillMounted(emptyNode)) { emptyNode.parentNode.removeChild(emptyNode); }
  emptyNode = null;
}

function pickEmpty() {
  const box = document.createElement('div');
  box.className = 'empty-state';
  const title = document.createElement('div');
  title.className = 'empty-title';
  title.textContent = '先选一个工作区和会话';
  const note = document.createElement('p');
  note.className = 'empty-note';
  note.textContent = '在左侧选工作区，再选一条会话。选好之后就可以给 Agent 派任务。';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'empty-go';
  button.textContent = '去选择工作区';
  button.onclick = function () { ctx.sidebar.openDrawer(); };
  box.appendChild(title);
  box.appendChild(note);
  box.appendChild(button);
  return box;
}

function readyEmpty(state) {
  const box = document.createElement('div');
  box.className = 'empty-state';
  const title = document.createElement('div');
  title.className = 'empty-title';
  title.textContent = '让 Agent 做点什么';
  const where = document.createElement('p');
  where.className = 'empty-where';
  where.textContent = workspaceName(state) + ' · ' + sessionLabel(state);
  const actions = document.createElement('div');
  actions.className = 'empty-actions';
  const samples = ['解释这个项目结构', '跑测试并修失败', '找我最近改过的文件'];
  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'empty-sample';
    button.textContent = sample;
    button.onclick = function () { useExample(sample); };
    actions.appendChild(button);
  }
  box.appendChild(where);
  box.appendChild(title);
  box.appendChild(actions);
  return box;
}

function syncEmpty() {
  const state = ctx.store.get('lastState') || {};
  const target = state.target || {};
  const live = ctx.thread.getLive();
  const show = !ctx.store.get('running') && !(live && live.done === false) && !contentOnStream();
  if (!show) { dropEmpty(); return; }
  const mode = target.root ? 'ready' : 'pick';
  const key = mode + '|' + (target.root || '') + '|' + (target.session || '')
    + '|' + workspaceName(state) + '|' + sessionLabel(state);
  if (emptyNode && stillMounted(emptyNode) && emptyNode.dataset.key === key) { return; }
  dropEmpty();
  emptyNode = mode === 'pick' ? pickEmpty() : readyEmpty(state);
  emptyNode.dataset.mode = mode;
  emptyNode.dataset.key = key;
  if (stream.firstChild) { stream.insertBefore(emptyNode, stream.firstChild); }
  else { stream.appendChild(emptyNode); }
}

  return { setRunning: setRunning, syncSend: syncSend, onSendClick: onSendClick, requestStop: requestStop, setThinking: setThinking, setMeta: setMeta, say: say, onKeydown: onKeydown, autoGrow: autoGrow, rememberDraft: rememberDraft, restoreDraft: restoreDraft, setGoal: setGoal, useExample: useExample, syncEmpty: syncEmpty };
}
