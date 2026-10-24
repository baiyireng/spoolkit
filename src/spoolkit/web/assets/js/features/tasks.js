// spool web · js/features/tasks.js
import "../core/dom.js";
import "../core/store.js";
import "../render/task_card.js";
import "../render/artifact_list.js";
import "../render/session_summary.js";
import "../render/checkpoint_list.js";
import "../render/failure.js";
/* 任务面板。当前会话来自 state.task_groups；跨会话只读 GET /tasks。
   产物、会话记忆和未完成文档只在首次打开时各读一次，按 root+session 缓存。
   搜索、状态筛选、会话范围都在本地做。详情复用任务卡正文，不画 raw，
   也不复制应用/拒绝。产物、记忆和未完成文档都不提供打开或继续。 */
const TASK_FILTERS = [
  ['all', '全部'],
  ['running', '进行中'],
  ['await', '待审批'],
  ['failed', '失败'],
  ['done', '完成'],
];
const TASK_SCOPES = [
  ['current', '当前会话'],
  ['all', '全部会话'],
];
function createTasks(ctx) {
  let filter = 'all';
  let scope = 'current';
  let selected = '';
  let query = '';
  let historyState = 'idle';
  let historyKey = '';
  let historyGroups = [];
  let historySessions = [];
  let artifactState = 'idle';
  let artifactKey = '';
  let artifactRows = [];
  let summaryState = 'idle';
  let summaryKey = '';
  let summaryView = null;
  let summaryOpen = true;
  let checkpointState = 'idle';
  let checkpointKey = '';
  let checkpointRows = [];

  function groupsOf(state) {
    const rows = ((state && state.task_groups) || []).slice();
    rows.reverse();
    return rows;
  }

  function taskWaiting(task) {
    return taskStillWaiting(task);
  }

  function matchesTask(task, name) {
    if (name === 'running') { return task.status === 'running'; }
    if (name === 'await') { return taskWaiting(task); }
    if (name === 'failed') { return task.status === 'failed' || task.status === 'verify_failed'; }
    if (name === 'done') { return task.status === 'done'; }
    return true;
  }

  function taskPaths(items) {
    const paths = [];
    const rows = items || [];
    for (let index = 0; index < rows.length; index += 1) {
      const item = rows[index];
      if (item && item.path) { paths.push(String(item.path)); }
    }
    return paths;
  }

  function currentSessionId() {
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    return String(target.session || state.session || '');
  }

  function targetKey() {
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    return String(target.root || '') + '\n' + currentSessionId();
  }

  function sessionTitleFor(id) {
    const sessionId = String(id || '');
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    if (sessionId && sessionId === currentSessionId() && target.session_title) {
      return String(target.session_title);
    }
    for (let index = 0; index < historySessions.length; index += 1) {
      const row = historySessions[index];
      if (row && String(row.id || '') === sessionId && row.title) {
        return String(row.title);
      }
    }
    const listed = (state.sessions) || [];
    for (let index = 0; index < listed.length; index += 1) {
      const row = listed[index];
      if (row && String(row.id || '') === sessionId && row.title) {
        return String(row.title);
      }
    }
    return sessionId;
  }

  function copyTask(task) {
    const copy = {};
    const source = task || {};
    const keys = Object.keys(source);
    for (let index = 0; index < keys.length; index += 1) {
      copy[keys[index]] = source[keys[index]];
    }
    return copy;
  }

  function mergeGroups(live, history) {
    const session = currentSessionId();
    const liveById = {};
    const liveList = [];
    for (let index = 0; index < live.length; index += 1) {
      const task = copyTask(live[index]);
      if (!task.session) { task.session = session; }
      liveById[String(task.task_id || '')] = task;
      liveList.push(task);
    }
    const historyList = history || [];
    const historyIds = {};
    for (let index = 0; index < historyList.length; index += 1) {
      const group = historyList[index];
      if (!group) { continue; }
      historyIds[String(group.task_id || '')] = true;
    }
    const seen = {};
    const merged = [];
    for (let index = 0; index < liveList.length; index += 1) {
      const id = String(liveList[index].task_id || '');
      if (historyIds[id] || seen[id]) { continue; }
      seen[id] = true;
      merged.push(liveList[index]);
    }
    for (let index = 0; index < historyList.length; index += 1) {
      const group = historyList[index];
      if (!group) { continue; }
      const id = String(group.task_id || '');
      if (seen[id]) { continue; }
      seen[id] = true;
      if (liveById[id]) {
        merged.push(liveById[id]);
        continue;
      }
      merged.push(group);
    }
    return merged;
  }

  function taskHaystack(task) {
    const sessionId = String((task && task.session) || currentSessionId());
    const parts = [sessionTitleFor(sessionId), String((task && task.goal) || '')];
    parts.push.apply(parts, taskPaths(task && task.artifacts));
    const events = (task && task.events) || [];
    for (let index = 0; index < events.length; index += 1) {
      const event = events[index] || {};
      parts.push(String(event.tool || ''));
      parts.push(String(event.summary || ''));
      parts.push.apply(parts, taskPaths(event.artifacts));
    }
    return parts.join('\n').toLowerCase();
  }

  function matchesQuery(task) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) { return true; }
    return taskHaystack(task).indexOf(needle) >= 0;
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

  function paintScope() {
    if (!tasksScope) { return; }
    tasksScope.replaceChildren();
    for (let index = 0; index < TASK_SCOPES.length; index += 1) {
      const pair = TASK_SCOPES[index];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-scope' + (pair[0] === scope ? ' on' : '');
      button.textContent = pair[1];
      button.setAttribute('data-scope', pair[0]);
      button.setAttribute('aria-pressed', pair[0] === scope ? 'true' : 'false');
      button.onclick = (function (name) {
        return function () {
          scope = name;
          renderPanel();
        };
      })(pair[0]);
      tasksScope.appendChild(button);
    }
  }

  function paintHistory() {
    if (!tasksHistory) { return; }
    tasksHistory.textContent = '';
    if (!ctx.store.get('tasksOpen')) { return; }
    if (historyState === 'loading') {
      tasksHistory.textContent = '正在加载历史';
      return;
    }
    if (historyState === 'error') {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-history-retry';
      button.textContent = '历史加载失败，可重试';
      button.onclick = function () { loadHistory(); };
      tasksHistory.appendChild(button);
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
    if (scope === 'all') {
      const chip = h('span', 'task-session task-chip');
      const title = sessionTitleFor(task.session || currentSessionId());
      chip.textContent = title;
      chip.title = title;
      chip.setAttribute('title', title);
      meta.appendChild(chip);
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

  function decorate(task) {
    const view = copyTask(task);
    const mine = String(view.session || currentSessionId()) === currentSessionId();
    view.canJump = mine;
    view.currentSession = mine;
    return view;
  }

  function paintDetail(task) {
    if (!tasksDetail) { return; }
    if (!task) {
      tasksDetail.replaceChildren();
      return;
    }
    const view = decorate(task);
    const id = String(view.task_id || '');
    const existing = tasksDetail.querySelector('.task-card');
    if (existing && existing.getAttribute('data-task-id') === id) {
      existing._task = view;
      existing.className = taskCardClass(view.status, taskOpen(existing));
      drawTaskHead(existing);
      if (taskOpen(existing)) { drawTaskBody(existing); }
      return;
    }
    tasksDetail.replaceChildren();
    const card = makeTaskCard(view);
    tasksDetail.appendChild(card);
    if (!taskOpen(card)) { setTaskOpen(card, true, false); }
  }

  function paintArtifacts() {
    if (!ctx.store.get('tasksOpen')) { return; }
    paintArtifactList(
      tasksArtifactsList,
      tasksArtifactsCount,
      artifactState,
      artifactRows,
      loadArtifacts
    );
  }

  function paintSummary() {
    if (!ctx.store.get('tasksOpen')) { return; }
    if (tasksMemoryTitle) { tasksMemoryTitle.textContent = '会话记忆'; }
    if (tasksMemory) { tasksMemory.classList.toggle('is-collapsed', !summaryOpen); }
    if (tasksMemoryToggle) {
      tasksMemoryToggle.setAttribute('aria-expanded', summaryOpen ? 'true' : 'false');
      tasksMemoryToggle.textContent = summaryOpen ? '收起' : '展开';
    }
    paintSessionSummary(tasksMemoryBody, summaryState, summaryView, loadSummary);
  }

  function paintCheckpoints() {
    if (!ctx.store.get('tasksOpen')) { return; }
    if (tasksCheckpointTitle) { tasksCheckpointTitle.textContent = '未完成文档'; }
    paintCheckpointList(tasksCheckpointList, checkpointState, checkpointRows, loadCheckpoints);
  }

  function inScope(task) {
    if (scope === 'all') { return true; }
    return String((task && task.session) || currentSessionId()) === currentSessionId();
  }

  function renderPanel() {
    const live = groupsOf(ctx.store.get('lastState'));
    paintCounts(live);
    if (!ctx.store.get('tasksOpen')) { return; }
    paintScope();
    paintHistory();
    paintArtifacts();
    paintSummary();
    paintCheckpoints();
    paintFilters();
    const merged = mergeGroups(live, historyState === 'ready' ? historyGroups : []);
    const scoped = [];
    for (let index = 0; index < merged.length; index += 1) {
      if (inScope(merged[index])) { scoped.push(merged[index]); }
    }
    const tabbed = [];
    for (let index = 0; index < scoped.length; index += 1) {
      if (matchesTask(scoped[index], filter)) { tabbed.push(scoped[index]); }
    }
    const visible = [];
    for (let index = 0; index < tabbed.length; index += 1) {
      if (matchesQuery(tabbed[index])) { visible.push(tabbed[index]); }
    }
    if (tasksMatch) {
      tasksMatch.textContent = '共 ' + scoped.length + ' 项，匹配 ' + visible.length;
    }
    let current = null;
    for (let index = 0; index < tabbed.length; index += 1) {
      if (String(tabbed[index].task_id || '') === selected) { current = tabbed[index]; }
    }
    if (!current) { selected = ''; }
    if (tasksList) {
      tasksList.replaceChildren();
      if (!visible.length) {
        const empty = h('div', 'tasks-empty');
        empty.textContent = scoped.length
          ? '没有匹配的任务'
          : (scope === 'current' ? '这一会话还没有任务' : '没有匹配的任务');
        tasksList.appendChild(empty);
      } else {
        for (let index = 0; index < visible.length; index += 1) {
          tasksList.appendChild(paintRow(visible[index]));
        }
      }
    }
    paintDetail(current);
  }

  function clearQuery() {
    if (!String(query || '').trim()) { return false; }
    query = '';
    if (tasksSearch) { tasksSearch.value = ''; }
    renderPanel();
    return true;
  }

  function loadHistory() {
    const key = targetKey();
    historyKey = key;
    historyState = 'loading';
    renderPanel();
    const finish = function (ok, payload) {
      if (historyKey !== key) { return; }
      if (!ok || !payload || typeof payload !== 'object') {
        historyState = 'error';
        renderPanel();
        return;
      }
      historyGroups = payload.groups || [];
      historySessions = payload.sessions || [];
      historyState = 'ready';
      renderPanel();
    };
    fetchJson('/tasks', finish);
  }

  function ensureFresh(heldKey, state, load) {
    const key = targetKey();
    if (heldKey === key && (state === 'loading' || state === 'ready' || state === 'error')) {
      return;
    }
    load();
  }

  function ensureHistory() {
    ensureFresh(historyKey, historyState, loadHistory);
  }

  function artifactItems(payload) {
    if (!Array.isArray(payload)) { return null; }
    const rows = [];
    for (let index = 0; index < payload.length; index += 1) {
      const item = payload[index];
      if (!item || typeof item !== 'object') { continue; }
      rows.push({
        kind: String(item.kind || ''),
        title: String(item.title || ''),
        summary: String(item.summary || ''),
        path: String(item.path || ''),
      });
    }
    return rows;
  }

  function loadArtifacts() {
    const key = targetKey();
    artifactKey = key;
    artifactState = 'loading';
    artifactRows = [];
    renderPanel();
    const finish = function (ok, payload) {
      if (artifactKey !== key) { return; }
      const rows = ok ? artifactItems(payload) : null;
      if (!rows) {
        artifactState = 'error';
        artifactRows = [];
        renderPanel();
        return;
      }
      artifactRows = rows;
      artifactState = 'ready';
      renderPanel();
    };
    fetchJson('/artifacts', finish);
  }

  function ensureArtifacts() {
    ensureFresh(artifactKey, artifactState, loadArtifacts);
  }

  function loadSummary() {
    const key = targetKey();
    summaryKey = key;
    summaryState = 'loading';
    summaryView = null;
    renderPanel();
    requestSessionSummary(function (ok, payload) {
      if (summaryKey !== key) { return; }
      const view = ok ? summaryFromPayload(payload) : null;
      if (!view) {
        summaryState = 'error';
        summaryView = null;
      } else {
        summaryState = 'ready';
        summaryView = view;
      }
      renderPanel();
    });
  }

  function ensureSummary() {
    ensureFresh(summaryKey, summaryState, loadSummary);
  }

  function loadCheckpoints() {
    const key = targetKey();
    checkpointKey = key;
    checkpointState = 'loading';
    checkpointRows = [];
    renderPanel();
    fetchJson('/checkpoints', function (ok, payload) {
      if (checkpointKey !== key) { return; }
      const rows = ok ? checkpointItems(payload) : null;
      if (!rows) {
        checkpointState = 'error';
        checkpointRows = [];
      } else {
        checkpointState = 'ready';
        checkpointRows = rows;
      }
      renderPanel();
    });
  }

  function ensureCheckpoints() {
    ensureFresh(checkpointKey, checkpointState, loadCheckpoints);
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
      ensureHistory();
      ensureArtifacts();
      ensureSummary();
      ensureCheckpoints();
      renderPanel();
      if (tasksClose && typeof tasksClose.focus === 'function') { tasksClose.focus(); }
    } else if (tasksBtn && typeof tasksBtn.focus === 'function') {
      tasksBtn.focus();
    }
  }

  function sync(state) {
    paintCounts(groupsOf(state));
    if (ctx.store.get('tasksOpen')) {
      ensureHistory();
      ensureArtifacts();
      ensureSummary();
      ensureCheckpoints();
      renderPanel();
    }
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
  if (tasksSearch) {
    tasksSearch.oninput = function () {
      query = String(tasksSearch.value || '');
      renderPanel();
    };
  }
  if (tasksMemoryToggle) {
    tasksMemoryToggle.onclick = function () {
      summaryOpen = !summaryOpen;
      paintSummary();
    };
  }
  paintCounts([]);

  return { sync: sync, setTasksOpen: setTasksOpen, renderPanel: renderPanel, clearQuery: clearQuery };
}
