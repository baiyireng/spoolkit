// spool web · js/render/task_card.js
import "../core/dom.js";
import "./failure.js";
/* 任务卡：挂在助手回复下面。只读 summary，不画 raw。展开与收起和思考过程同一口径。 */
// 任务卡只读 summary / refs / artifacts。事件上没有 raw，这里也不画。
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
  return '等待';
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
      const label = approvalLabel(event.status);
      if (call && byCall[call]) {
        byCall[call].approval = label;
        continue;
      }
      rows.push({kind: 'approval', text: label});
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
  head.replaceChildren();
  const mark = h('span', 'task-mark');
  mark.textContent = TASK_STATUS_LABEL[task.status] || TASK_STATUS_LABEL.running;
  const goal = h('span', 'task-goal');
  goal.textContent = redact(String(task.goal || ''));
  const steps = h('span', 'task-steps');
  steps.textContent = taskStepCount(task) + ' 步';
  const verify = h('span', 'task-verify');
  verify.textContent = taskVerifyText(task);
  head.appendChild(mark);
  head.appendChild(document.createTextNode(' · '));
  head.appendChild(goal);
  head.appendChild(document.createTextNode(' · '));
  head.appendChild(steps);
  head.appendChild(document.createTextNode(' · '));
  head.appendChild(verify);
}

function drawTaskBody(box) {
  box._body.replaceChildren();
  for (const row of taskRows(box._task || {})) {
    const line = h('div', 'task-row task-' + row.kind + (row.bad ? ' bad' : ''));
    if (row.kind === 'observe') {
      const tool = h('span', 'task-tool');
      tool.textContent = redact(row.tool);
      line.appendChild(tool);
      if (row.intent) {
        const intent = h('span', 'task-intent');
        intent.textContent = redact(row.intent);
        line.appendChild(document.createTextNode(' · '));
        line.appendChild(intent);
      }
      const summary = h('span', 'task-summary');
      summary.textContent = redact(row.summary);
      line.appendChild(document.createTextNode(' · '));
      line.appendChild(summary);
      const status = h('span', 'task-row-status');
      status.textContent = row.status;
      line.appendChild(document.createTextNode(' · '));
      line.appendChild(status);
      if (row.approval) {
        const approval = h('span', 'task-approval');
        approval.textContent = row.approval;
        line.appendChild(document.createTextNode(' · '));
        line.appendChild(approval);
      }
    } else {
      line.textContent = redact(row.text);
    }
    box._body.appendChild(line);
  }
}

function taskOpen(box) { return !box._body.classList.contains('hidden'); }

function setTaskOpen(box, open) {
  box._body.classList.toggle('hidden', !open);
  box._head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { drawTaskBody(box); }
  else { box._body.replaceChildren(); }
}

function makeTaskCard(task) {
  const box = h('div', 'task-card task-card-' + taskStatusClass(task.status));
  box.setAttribute('data-task-id', String(task.task_id || ''));
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'task-head';
  head.setAttribute('aria-expanded', 'false');
  const body = h('div', 'task-body hidden');
  head.onclick = function () { setTaskOpen(box, !taskOpen(box)); };
  box.appendChild(head);
  box.appendChild(body);
  box._head = head;
  box._body = body;
  box._task = task;
  drawTaskHead(box);
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
    card.className = 'task-card task-card-' + taskStatusClass(task.status);
    drawTaskHead(card);
    if (taskOpen(card)) { drawTaskBody(card); }
  }
}
