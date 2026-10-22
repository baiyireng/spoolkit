// spool web · js/features/confirm.js
import "../core/dom.js";
import "../core/store.js";
import "../core/transport.js";
import "../render/diff.js";
/* ── 「撤销这一轮」：把退路做成动作，不是一条要抄的命令（问题④） ──
   服务端在 `final` 事件上只增地给出 `revert_run_id` / `revert_command`
   （`run.py`），页面据此摆一枚按钮，点一下 POST /revert——**与
   `spool revert --run <id>` 走同一份实现**，用户不需要复制命令。
   幂等：同一个 run_id 只摆一次（事件与快照各来一次也只摆一条）。 */
function createConfirm(ctx) {
let undoRow = null;
let undoRunId = '';
let heldCommand = null;

function showUndo(final) {
  const runId = String((final && final.revert_run_id) || '');
  if (!runId || runId === undoRunId) { return; }
  if (undoRow && undoRow.parentNode) { undoRow.parentNode.removeChild(undoRow); }
  undoRunId = runId;
  const stick = ctx.thread.atBottom();
  const row = document.createElement('div');
  row.className = 'undo-row';
  const label = document.createElement('span');
  label.textContent = '这一轮的成果可以退回去：';
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '撤销这一轮';
  button.onclick = async function () {
    if (button.disabled) { return; }
    button.disabled = true;
    const next = await ctx.transport.post('/revert', {run: runId});
    if (next === null) { button.disabled = false; return; }
    row.className = 'undo-row ok';
    row.replaceChildren(document.createTextNode(
      String((next && next.message) || '已撤销这一轮')));
  };
  row.appendChild(label);
  row.appendChild(button);
  appendNode(row);
  undoRow = row;
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

/* ── 对话流里的待确认卡片 ──
   有待确认内容时才建节点；没有的时候这块 UI **在页面上不存在**（HTML 里没有占位、
   也没有可聚焦的空按钮）。点完「应用/拒绝」它就变成一行状态，留在对话流里当记录。
   只读壳里那两个按钮**根本不建**：点它必被 403 拒，而藏起来的空按钮还能被 Tab 到。 */

// 卡片上写的"谁的"：机器级壳服务整台机器，只说"有 1 处改动待确认"，
// 用户根本不知道自己批的是哪个工作区、哪条会话里的内容。
function targetLabel() {
  const t = (ctx.store.get('lastState') || {}).target || {};
  if (!t.root) { return '目标：未选择'; }
  return '工作区 ' + t.root + ' · 会话 '
    + (String(t.session_title || '').trim() || t.session || '');
}


// 建（或复用）那张卡片。它挂在对话流的最后：ChatGPT 那一挂的"工具调用卡片"也是这样，
// 出现在**对话里**，而不是一个永远浮在输入条上方的空框。
function ensureCard() {
  if (ctx.store.get('card')) { return ctx.store.get('card'); }
  const node = document.createElement('div');
  node.dataset.state = 'asking';
  const head = document.createElement('div');
  head.className = 'ask-head';
  const who = document.createElement('span');
  who.className = 'ask-who';
  const kind = document.createElement('span');
  kind.className = 'ask-kind';
  head.appendChild(who);
  head.appendChild(kind);
  const body = document.createElement('div');
  body.className = 'ask-body';
  node.appendChild(head);
  node.appendChild(body);
  node._who = who;
  node._kind = kind;
  node._body = body;
  node._what = '';
  node._foot = null;
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  if (!ctx.store.get('readonly')) {
    const foot = document.createElement('div');
    foot.className = 'ask-actions';
    const apply = document.createElement('button');
    apply.type = 'button';
    apply.className = 'ask-apply';
    apply.textContent = '应用全部改动';
    apply.onclick = function () { decide(true); };
    const reject = document.createElement('button');
    reject.type = 'button';
    reject.className = 'ask-reject';
    reject.textContent = '拒绝全部';
    reject.onclick = function () { decide(false); };
    foot.appendChild(apply);
    foot.appendChild(reject);
    node.appendChild(foot);
    node._foot = foot;
    ctx.store.set('cardApply', apply);
    ctx.store.set('cardReject', reject);
  }
  node.className = 'ask-card';
  node.dataset.kind = 'diff';
  appendNode(node);
  ctx.store.set('card', node);
  ctx.store.set('cardAsking', true);
  ctx.store.set('decided', false);
  return node;
}

// 卡片上问的是什么（结算时那一行状态要用它）。
function cardWhat() {
  return ctx.store.get('card') ? String(ctx.store.get('card')._what || '') : '';
}

// 没有待确认内容了：**把这块 UI 从页面上拿掉**（不是隐藏）。
function removeCard() {
  if (ctx.store.get('card') && ctx.store.get('card').parentNode) { ctx.store.get('card').parentNode.removeChild(ctx.store.get('card')); }
  ctx.store.set('card', null);
  ctx.store.set('cardAsking', false);
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  ctx.store.set('cardSig', '');
  ctx.store.set('decided', false);
}

// 确认完了：卡片**就地**变成一行"已应用 / 已拒绝"，留在对话流里当记录
// （它不是横幅）。同时把"当前那张卡片"这个位置让出来：以后又有待确认项，
// 是另起一张新卡片，而不是把这一行状态改回问句。
function settleCard(outcome) {
  if (!ctx.store.get('card') || !ctx.store.get('cardAsking')) { return; }
  const node = ctx.store.get('card');
  const what = cardWhat();
  const stick = ctx.thread.atBottom();
  ctx.store.set('card', null);
  ctx.store.set('cardAsking', false);
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  ctx.store.set('cardSig', '');
  const line = document.createElement('div');
  line.className = 'ask-done';
  const kind = node.dataset.kind;
  let text;
  if (kind === 'combined') {
    const count = node.dataset.changes;
    const commandText = node.dataset.command;
    text = outcome === 'applied'
      ? ('已应用 ' + count + ' 处改动并允许执行：命令 ' + commandText)
      : ('已拒绝：' + count + ' 处改动 + 命令 ' + commandText);
  } else {
    const command = kind === 'command';
    const prefix = command
      ? (outcome === 'applied' ? '已允许执行：' : '已拒绝：')
      : (outcome === 'applied' ? '已应用：' : '已拒绝：');
    text = prefix + what;
  }
  line.textContent = text + '（' + targetLabel() + '）';
  heldCommand = null;
  node.className = 'ask-row ' + (outcome === 'applied' ? 'ok' : 'bad');
  node._foot = null;
  node.replaceChildren(line);
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

function stampAskCalls(node, items, detail) {
  if (!node) { return; }
  const ids = [];
  function rememberCall(value) {
    const text = String(value || '');
    if (text && ids.indexOf(text) < 0) { ids.push(text); }
  }
  for (const item of items || []) { rememberCall(item && item.tool_call_id); }
  if (detail) {
    rememberCall(detail.tool_call_id);
    const many = detail.tool_call_ids;
    if (many && many.length) { for (const id of many) { rememberCall(id); } }
  }
  if (!ids.length) {
    node.removeAttribute('data-tool-call-id');
    node.removeAttribute('data-tool-call-ids');
    return;
  }
  node.setAttribute('data-tool-call-id', ids[0]);
  node.setAttribute('data-tool-call-ids', ids.join(' '));
}

function showPending(items) {
  if (!items.length) { clearPending(); return; }
  ctx.store.set('pendings', items);
  // 同一份待确认内容（补帧 / 重连后的一帧）只更新，不重建：
  // diff 正文可能有几百行，重建一次没有收益。
  const sig = JSON.stringify(['diffs', items.map(function (one) {
    return [one.path, one.text];
  })]);
  if (ctx.store.get('card') && ctx.store.get('cardAsking') && ctx.store.get('cardSig') === sig) {
    stampAskCalls(ctx.store.get('card'), items, null);
    return;
  }
  const node = ensureCard();
  ctx.store.set('cardSig', sig);
  const stick = ctx.thread.atBottom();
  node.className = 'ask-card';
  node.dataset.kind = 'diff';
  node.dataset.state = 'asking';
  stampAskCalls(node, items, null);
  setAskLabels(false);
  node._what = items.length + ' 处改动';
  node._who.textContent = targetLabel();
  node._kind.textContent = items.length + ' 处改动待确认';
  node._body.replaceChildren();
  for (const item of items) { node._body.appendChild(diffBox(item.path, item.text)); }
  if (node._foot) { node._foot.style.display = 'flex'; }
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

// 服务端说"没有待确认内容了"（或确认完了）。`outcome` 给 'applied' / 'rejected'
// 时卡片变成那一行状态；不给时**把这块 UI 整个拿掉**（页面上不留空框）。
function clearPending(outcome) {
  heldCommand = null;
  ctx.store.set('pendings', []);
  if (!ctx.store.get('card')) { ctx.store.set('decided', false); return; }
  // 已经结算过的那一行状态是记录，留着（它不是横幅，是"这一趟批过什么"）。
  if (!ctx.store.get('cardAsking')) { return; }
  if (outcome === 'applied' || outcome === 'rejected') { settleCard(outcome); return; }
  removeCard();
}
function setAskLabels(mode) {
  const apply = ctx.store.get('cardApply');
  const reject = ctx.store.get('cardReject');
  const command = mode === true;
  const combined = mode === 'combined';
  if (apply) {
    apply.textContent = combined ? '应用改动并允许执行'
      : (command ? '允许执行' : '应用全部改动');
  }
  if (reject) {
    reject.textContent = combined ? '全部拒绝'
      : (command ? '拒绝' : '拒绝全部');
  }
}

function showCombined(items, detail) {
  ctx.store.set('pendings', items);
  const sig = JSON.stringify(['combined', items.map(function (one) {
    return [one.path, one.text];
  }), detail.command, detail.reason || '']);
  if (ctx.store.get('card') && ctx.store.get('cardAsking') && ctx.store.get('cardSig') === sig) {
    stampAskCalls(ctx.store.get('card'), items, detail);
    return;
  }
  const node = ensureCard();
  ctx.store.set('cardSig', sig);
  const stick = ctx.thread.atBottom();
  node.className = 'ask-card combined';
  stampAskCalls(node, items, detail);
  node.dataset.kind = 'combined';
  node.dataset.state = 'asking';
  setAskLabels('combined');
  node.dataset.changes = String(items.length);
  node.dataset.command = String(detail.command);
  node._what = items.length + ' 处改动 + 命令 ' + detail.command;
  node._who.textContent = targetLabel();
  node._kind.textContent = items.length + ' 处改动 + 1 条命令待确认';
  node._body.replaceChildren();
  for (const item of items) { node._body.appendChild(diffBox(item.path, item.text)); }
  const title = document.createElement('div');
  title.className = 'ask-title';
  const mark = document.createElement('span');
  mark.className = 'ask-mark';
  mark.textContent = '⚠';
  title.appendChild(mark);
  title.appendChild(document.createTextNode(' 要执行一条命令'));
  const pre = document.createElement('pre');
  pre.className = 'ask-cmd';
  const code = document.createElement('code');
  code.textContent = String(detail.command || '');
  pre.appendChild(code);
  const why = document.createElement('div');
  why.className = 'ask-reason';
  why.textContent = '原因：' + (detail.reason || '未说明');
  const how = document.createElement('div');
  how.className = 'ask-warn';
  how.textContent = '仅允许这一次；长期允许需写入 .agent/grants.json';
  node._body.appendChild(title);
  node._body.appendChild(pre);
  node._body.appendChild(why);
  node._body.appendChild(how);
  if (node._foot) { node._foot.style.display = 'flex'; }
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

function showApproval(spec) {
  const diffs = (spec && spec.diffs) || [];
  const detail = (spec && spec.commandDetail) || null;
  const command = detail && detail.command ? detail : null;
  heldCommand = command;
  if (diffs.length && command) { showCombined(diffs, command); return; }
  if (command) {
    ctx.status.showCommandAsk(command);
    stampAskCalls(ctx.store.get('card'), [], command);
    return;
  }
  if (diffs.length) { showPending(diffs); return; }
}

function currentCommand() {
  if (!ctx.store.get('card') || !ctx.store.get('cardAsking')) { return null; }
  return heldCommand;
}

// 两个按钮是在建卡片时现绑的（`ensureCard`）——没有待确认内容时
// 页面上根本没有这两个按钮，也就没有可聚焦的空控件。
async function decide(apply) {
  // 点一次就禁用：重复写 stdin 会让后续的 input() 拿到意外的输入。
  if (ctx.store.get('decided') || !ctx.store.get('cardAsking')) { return; }
  ctx.store.set('decided', true);
  if (ctx.store.get('cardApply')) { ctx.store.get('cardApply').disabled = true; }
  if (ctx.store.get('cardReject')) { ctx.store.get('cardReject').disabled = true; }
  const next = await ctx.transport.post('/confirm', {apply});
  if (next === null) {
    // 被拒（409 没有待确认 / 只读 403）：卡片退回"等你点"，别错误地停在"已应用"。
    // 理由已经由 post() 摆进对话区与通知里了。
    ctx.store.set('decided', false);
    if (ctx.store.get('cardApply')) { ctx.store.get('cardApply').disabled = false; }
    if (ctx.store.get('cardReject')) { ctx.store.get('cardReject').disabled = false; }
    return;
  }
  settleCard(apply ? 'applied' : 'rejected');
}

  return { showUndo: showUndo, targetLabel: targetLabel, ensureCard: ensureCard, cardWhat: cardWhat, removeCard: removeCard, settleCard: settleCard, showPending: showPending, showApproval: showApproval, currentCommand: currentCommand, clearPending: clearPending, setAskLabels: setAskLabels, decide: decide };
}
