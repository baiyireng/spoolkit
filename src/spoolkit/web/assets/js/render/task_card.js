// spool web · js/render/task_card.js
import "../core/dom.js";
import "./failure.js";
/* 任务卡：挂在助手回复下面。只读 summary，不画 raw。
   running 第一次出现时展开；其余状态收起。人点过之后，重绑不再改开合。 */
const TASK_STATUS_LABEL = {
  done: '完成',
  verify_failed: '验证未过',
  failed: '失败',
  cancelled: '已取消',
  running: '进行中',
};

function taskStatusClass(status) {
  return Object.prototype.hasOwnProperty.call(TASK_STATUS_LABEL, status) ? status : 'running';
}

function taskCardClass(status, open) {
  return 'task-card task-card-' + taskStatusClass(status) + (open ? ' open' : '');
}

function taskStepCount(task) {
  let max = -1;
  for (const event of task.events || []) {
    const step = Number(event.step);
    if (!Number.isNaN(step) && step > max) { max = step; }
  }
  return max < 0 ? 0 : max + 1;
}

function taskVerifyText(task) {
  let found = '';
  for (const event of task.events || []) {
    if (event.phase !== 'verify') { continue; }
    if (event.status === 'error') { found = '验证未过'; }
    else if (event.status === 'ok') { found = '验证通过'; }
    else { found = '已验证'; }
  }
  if (found) { return found; }
  if (task.status === 'verify_failed') { return '验证未过'; }
  return '未验证';
}

function approvalLabel(status) {
  if (status === 'confirmed') { return '已确认'; }
  if (status === 'rejected') { return '已拒绝'; }
  return '';
}

function approvalWaiting(status) {
  return status !== 'confirmed' && status !== 'rejected';
}

function approvalJump(event) {
  const artifacts = event.artifacts || [];
  for (const item of artifacts) {
    if (item && String(item.type || '') === 'diff') { return '查看改动'; }
  }
  const summary = String(event.summary || '');
  if (summary.indexOf('待确认命令') >= 0) { return '去确认'; }
  if (summary.indexOf('待确认') >= 0) { return '查看改动'; }
  return '去确认';
}

function observeStatus(status) {
  if (status === 'ok') { return '完成'; }
  if (status === 'error') { return '失败'; }
  if (status === 'running') { return '进行中'; }
  return String(status || '');
}

function taskRows(task) {
  const events = (task.events || []).slice().sort(function (left, right) {
    return Number(left.seq || 0) - Number(right.seq || 0);
  });
  const rows = [];
  const byCall = {};
  for (const event of events) {
    const phase = String(event.phase || '');
    if (phase === 'understand') { continue; }
    if (phase === 'approval') {
      const call = String(event.tool_call_id || '');
      const waiting = approvalWaiting(event.status);
      const patch = {
        approval: waiting ? '' : approvalLabel(event.status),
        jump: waiting ? approvalJump(event) : '',
        callId: call,
      };
      if (call && byCall[call]) {
        byCall[call].approval = patch.approval;
        byCall[call].jump = patch.jump;
        byCall[call].callId = call;
        continue;
      }
      rows.push({kind: 'approval', text: patch.approval, jump: patch.jump, callId: call});
      continue;
    }
    if (phase === 'observe') {
      const row = {
        kind: 'observe',
        tool: String(event.tool || ''),
        intent: String(event.intent || ''),
        summary: String(event.summary || ''),
        status: observeStatus(event.status),
        bad: event.status === 'error',
        approval: '',
        jump: '',
        callId: String(event.tool_call_id || ''),
      };
      rows.push(row);
      if (event.tool_call_id) { byCall[String(event.tool_call_id)] = row; }
      continue;
    }
    if (phase === 'retrieval') {
      rows.push({kind: 'retrieval', text: String(event.summary || '')});
    } else if (phase === 'verify') {
      rows.push({kind: 'verify', text: String(event.summary || ''), bad: event.status === 'error'});
    } else if (phase === 'error') {
      rows.push({kind: 'error', text: String(event.summary || ''), bad: true});
    } else if (phase === 'final') {
      rows.push({kind: 'final', text: '结论已生成'});
    }
  }
  return rows;
}

function drawTaskHead(box) {
  const task = box._task || {};
  const head = box._head;
  const goalText = redact(String(task.goal || ''));
  head.replaceChildren();
  head.title = goalText;
  head.setAttribute('title', goalText);
  const mark = h('span', 'task-mark task-chip');
  mark.textContent = TASK_STATUS_LABEL[task.status] || TASK_STATUS_LABEL.running;
  const goal = h('span', 'task-goal');
  goal.textContent = goalText;
  goal.title = goalText;
  goal.setAttribute('title', goalText);
  const meta = h('span', 'task-meta');
  const steps = h('span', 'task-steps task-chip');
  steps.textContent = taskStepCount(task) + ' 步';
  const verify = h('span', 'task-verify task-chip');
  verify.textContent = taskVerifyText(task);
  meta.appendChild(steps);
  meta.appendChild(verify);
  const chevron = h('span', 'task-chevron');
  chevron.setAttribute('aria-hidden', 'true');
  head.appendChild(mark);
  head.appendChild(goal);
  head.appendChild(meta);
  head.appendChild(chevron);
}

function focusAsk(callId) {
  const want = String(callId || '');
  const cards = document.querySelectorAll('.ask-card');
  let hit = null;
  for (const node of cards) {
    const ids = (
      String(node.getAttribute('data-tool-call-id') || '') + ' '
      + String(node.getAttribute('data-tool-call-ids') || '')
    ).split(/\s+/);
    if (want && ids.indexOf(want) >= 0) { hit = node; break; }
  }
  if (!hit && cards.length === 1) { hit = cards[0]; }
  if (!hit) { return; }
  if (typeof hit.scrollIntoView === 'function') {
    hit.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
  const apply = hit.querySelector('.ask-apply');
  if (apply && typeof apply.focus === 'function') { apply.focus(); }
}

function appendJump(line, row) {
  if (!row.jump) { return; }
  const jump = document.createElement('button');
  jump.type = 'button';
  jump.className = 'task-jump';
  jump.textContent = row.jump;
  jump.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    focusAsk(row.callId);
  };
  line.appendChild(jump);
}

function drawTaskBody(box) {
  const task = box._task || {};
  box._body.replaceChildren();
  if (task.currentSession === false) {
    const note = h('div', 'task-other');
    note.textContent = '该任务属于其他会话';
    box._body.appendChild(note);
  }
  for (const row of taskRows(task)) {
    const line = h('div', 'task-row task-' + row.kind + (row.bad ? ' bad' : ''));
    if (row.kind === 'observe') {
      const meta = h('div', 'task-row-line');
      const tool = h('span', 'task-tool');
      tool.textContent = redact(row.tool);
      meta.appendChild(tool);
      if (row.intent) {
        const intent = h('span', 'task-intent');
        intent.textContent = redact(row.intent);
        meta.appendChild(intent);
      }
      const status = h('span', 'task-row-status');
      status.textContent = row.status;
      meta.appendChild(status);
      line.appendChild(meta);
      const summary = h('div', 'task-summary');
      const summaryText = redact(row.summary);
      summary.textContent = summaryText;
      summary.title = summaryText;
      summary.setAttribute('title', summaryText);
      line.appendChild(summary);
      if (row.approval) {
        const approval = h('span', 'task-approval');
        approval.textContent = row.approval;
        line.appendChild(approval);
      }
      if (task.canJump !== false) { appendJump(line, row); }
    } else if (row.kind === 'approval') {
      const showJump = row.jump && task.canJump !== false;
      if (!row.text && !showJump) { continue; }
      if (row.text) { line.appendChild(document.createTextNode(row.text)); }
      if (showJump) { appendJump(line, row); }
    } else {
      line.textContent = redact(row.text);
    }
    box._body.appendChild(line);
  }
}

function taskOpen(box) { return !box._body.classList.contains('hidden'); }

function setTaskOpen(box, open, fromUser) {
  if (fromUser) { box._userSet = true; }
  box._body.classList.toggle('hidden', !open);
  box.classList.toggle('open', open);
  box._head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { drawTaskBody(box); }
  else { box._body.replaceChildren(); }
}

function makeTaskCard(task) {
  const box = h('div', taskCardClass(task.status, false));
  box.setAttribute('data-task-id', String(task.task_id || ''));
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'task-head';
  head.setAttribute('aria-expanded', 'false');
  const body = h('div', 'task-body hidden');
  head.onclick = function () { setTaskOpen(box, !taskOpen(box), true); };
  box.appendChild(head);
  box.appendChild(body);
  box._head = head;
  box._body = body;
  box._task = task;
  box._userSet = false;
  drawTaskHead(box);
  if (taskStatusClass(task.status) === 'running') { setTaskOpen(box, true, false); }
  return box;
}

function syncTaskCards(wrap, tasks) {
  if (!wrap) { return; }
  let host = wrap.querySelector('.task-list');
  if (!host) {
    host = h('div', 'task-list');
    wrap.appendChild(host);
  }
  for (const task of tasks || []) {
    const id = String(task.task_id || '');
    let card = null;
    for (const node of host.querySelectorAll('.task-card')) {
      if (node.getAttribute('data-task-id') === id) { card = node; break; }
    }
    if (!card) {
      host.appendChild(makeTaskCard(task));
      continue;
    }
    card._task = task;
    card.className = taskCardClass(task.status, taskOpen(card));
    drawTaskHead(card);
    if (taskOpen(card)) { drawTaskBody(card); }
  }
}
