// spool web · js/features/tasks.js
import "../core/dom.js";
import "../core/store.js";
import "../render/task_card.js";
import "../render/failure.js";
/* 当前会话的任务面板。数据只读 state.task_groups，筛选在本地做。
   详情复用任务卡正文，不画 raw，也不复制应用/拒绝。 */
const TASK_FILTERS = [
  ['all', '全部'],
  ['running', '进行中'],
  ['await', '等审批'],
  ['failed', '失败'],
  ['done', '完成'],
];

function createTasks(ctx) {
  let filter = 'all';
  let selected = '';

  function groupsOf(state) {
    const rows = ((state && state.task_groups) || []).slice();
    rows.reverse();
    return rows;
  }

  function taskWaiting(task) {
    const events = (task && task.events) || [];
    for (let index = 0; index < events.length; index += 1) {
      const event = events[index];
      if (String(event.phase || '') === 'approval' && approvalWaiting(event.status)) {
        return true;
      }
    }
    return false;
  }

  function matchesTask(task, name) {
    if (name === 'running') { return task.status === 'running'; }
    if (name === 'await') { return taskWaiting(task); }
    if (name === 'failed') { return task.status === 'failed' || task.status === 'verify_failed'; }
    if (name === 'done') { return task.status === 'done'; }
    return true;
  }

  function paintCounts(groups) {
    let waiting = 0;
    for (let index = 0; index < groups.length; index += 1) {
      if (taskWaiting(groups[index])) { waiting += 1; }
    }
    if (tasksCount) { tasksCount.textContent = String(groups.length); }
    if (tasksWait) {
      tasksWait.textContent = waiting ? String(waiting) : '';
      tasksWait.classList.toggle('hidden', waiting === 0);
    }
    if (tasksBtn) {
      tasksBtn.setAttribute('aria-label', '任务 ' + groups.length + '，待审批 ' + waiting);
    }
  }

  function paintFilters() {
    if (!tasksFilters) { return; }
    tasksFilters.replaceChildren();
    for (let index = 0; index < TASK_FILTERS.length; index += 1) {
      const pair = TASK_FILTERS[index];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-filter' + (pair[0] === filter ? ' on' : '');
      button.textContent = pair[1];
      button.setAttribute('data-filter', pair[0]);
      button.setAttribute('aria-pressed', pair[0] === filter ? 'true' : 'false');
      button.onclick = (function (name) {
        return function () {
          filter = name;
          renderPanel();
        };
      })(pair[0]);
      tasksFilters.appendChild(button);
    }
  }

  function paintRow(task) {
    const id = String(task.task_id || '');
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'task-line task-card-' + taskStatusClass(task.status)
      + (id === selected ? ' on' : '');
    row.setAttribute('data-task-id', id);
    const mark = h('span', 'task-mark task-chip');
    mark.textContent = TASK_STATUS_LABEL[task.status] || TASK_STATUS_LABEL.running;
    const goalText = redact(String(task.goal || ''));
    const goal = h('span', 'task-goal');
    goal.textContent = goalText;
    goal.title = goalText;
    goal.setAttribute('title', goalText);
    const meta = h('span', 'task-line-meta');
    const steps = h('span', 'task-steps task-chip');
    steps.textContent = taskStepCount(task) + ' 步';
    const verify = h('span', 'task-verify task-chip');
    verify.textContent = taskVerifyText(task);
    meta.appendChild(steps);
    meta.appendChild(verify);
    if (taskWaiting(task)) {
      const wait = h('span', 'task-wait task-chip');
      wait.textContent = '待确认';
      meta.appendChild(wait);
    }
    row.appendChild(mark);
    row.appendChild(goal);
    row.appendChild(meta);
    row.onclick = function () {
      selected = id;
      renderPanel();
    };
    return row;
  }

  function paintDetail(task) {
    if (!tasksDetail) { return; }
    if (!task) {
      tasksDetail.replaceChildren();
      return;
    }
    const id = String(task.task_id || '');
    const existing = tasksDetail.querySelector('.task-card');
    if (existing && existing.getAttribute('data-task-id') === id) {
      existing._task = task;
      existing.className = taskCardClass(task.status, taskOpen(existing));
      drawTaskHead(existing);
      if (taskOpen(existing)) { drawTaskBody(existing); }
      return;
    }
    tasksDetail.replaceChildren();
    const card = makeTaskCard(task);
    tasksDetail.appendChild(card);
    if (!taskOpen(card)) { setTaskOpen(card, true, false); }
  }

  function renderPanel() {
    const groups = groupsOf(ctx.store.get('lastState'));
    paintCounts(groups);
    if (!ctx.store.get('tasksOpen')) { return; }
    paintFilters();
    const visible = [];
    for (let index = 0; index < groups.length; index += 1) {
      if (matchesTask(groups[index], filter)) { visible.push(groups[index]); }
    }
    let current = null;
    for (let index = 0; index < visible.length; index += 1) {
      if (String(visible[index].task_id || '') === selected) { current = visible[index]; }
    }
    if (!current) { selected = ''; }
    if (tasksList) {
      tasksList.replaceChildren();
      if (!visible.length) {
        const empty = h('div', 'tasks-empty');
        empty.textContent = groups.length ? '没有匹配的任务' : '这一会话还没有任务';
        tasksList.appendChild(empty);
      } else {
        for (let index = 0; index < visible.length; index += 1) {
          tasksList.appendChild(paintRow(visible[index]));
        }
      }
    }
    paintDetail(current);
  }

  function setTasksOpen(open) {
    if (!tasksPanel) { return; }
    const want = Boolean(open);
    if (want === ctx.store.get('tasksOpen')) { return; }
    ctx.store.set('tasksOpen', want);
    tasksPanel.classList.toggle('hidden', !want);
    tasksPanel.setAttribute('aria-hidden', want ? 'false' : 'true');
    if (tasksBtn) { tasksBtn.setAttribute('aria-expanded', want ? 'true' : 'false'); }
    if (want) {
      renderPanel();
      if (tasksClose && typeof tasksClose.focus === 'function') { tasksClose.focus(); }
    } else if (tasksBtn && typeof tasksBtn.focus === 'function') {
      tasksBtn.focus();
    }
  }

  function sync(state) {
    paintCounts(groupsOf(state));
    if (ctx.store.get('tasksOpen')) { renderPanel(); }
  }

  if (tasksClose) { tasksClose.innerHTML = ICON.close; }
  if (tasksPanel) {
    tasksPanel.classList.add('hidden');
    tasksPanel.setAttribute('aria-hidden', 'true');
  }
  if (tasksBtn) {
    tasksBtn.setAttribute('aria-expanded', 'false');
    tasksBtn.onclick = function () { setTasksOpen(!ctx.store.get('tasksOpen')); };
  }
  if (tasksClose) {
    tasksClose.onclick = function () { setTasksOpen(false); };
  }
  paintCounts([]);

  return { sync: sync, setTasksOpen: setTasksOpen, renderPanel: renderPanel };
}
