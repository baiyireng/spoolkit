// spool web · js/render/session_summary.js
import "../core/dom.js";
/* 任务面板里的会话记忆。只画摘要字段，不画产物、正文、路径或 checkpoint。 */

function summaryStrings(raw) {
  if (!Array.isArray(raw)) { return []; }
  const rows = [];
  for (let index = 0; index < raw.length; index += 1) {
    const item = raw[index];
    if (typeof item !== 'string') { continue; }
    const text = item.trim();
    if (text) { rows.push(text); }
  }
  return rows;
}

function summaryFromPayload(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) { return null; }
  const done = summaryStrings(payload.what_done);
  return {
    headline: String(payload.headline || '').trim(),
    done: done.slice(0, 5),
    doneMore: done.length > 5 ? done.length - 5 : 0,
    openItems: summaryStrings(payload.open_items),
    decisions: summaryStrings(payload.decisions),
    turn: String(payload.last_turn_id || '').trim(),
  };
}

function summaryIsEmpty(view) {
  return !view.headline && !view.done.length && !view.openItems.length
    && !view.decisions.length && !view.turn;
}

function paintMemoryItems(host, label, items) {
  if (!items.length) { return; }
  const name = h('div', 'tasks-memory-label');
  name.textContent = label;
  host.appendChild(name);
  for (let index = 0; index < items.length; index += 1) {
    const row = h('div', 'tasks-memory-item');
    row.textContent = items[index];
    host.appendChild(row);
  }
}

function paintSessionSummary(host, state, view, retry) {
  if (!host) { return; }
  host.replaceChildren();
  if (state === 'loading') {
    const note = h('div', 'tasks-empty');
    note.textContent = '正在加载记忆';
    host.appendChild(note);
    return;
  }
  if (state === 'error' || !view) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tasks-memory-retry';
    button.textContent = '记忆加载失败，可重试';
    button.onclick = function () { retry(); };
    host.appendChild(button);
    return;
  }
  if (summaryIsEmpty(view)) {
    const empty = h('div', 'tasks-empty');
    empty.textContent = '当前会话还没有记忆摘要';
    host.appendChild(empty);
    return;
  }
  if (view.headline) {
    const headline = h('div', 'tasks-memory-headline');
    headline.textContent = view.headline;
    host.appendChild(headline);
  }
  paintMemoryItems(host, '已完成', view.done);
  if (view.doneMore) {
    const more = h('div', 'tasks-memory-more');
    more.textContent = '还有 ' + view.doneMore + ' 条';
    host.appendChild(more);
  }
  paintMemoryItems(host, '未完成', view.openItems);
  paintMemoryItems(host, '决定', view.decisions);
  if (view.turn) {
    const label = h('div', 'tasks-memory-label');
    label.textContent = '最近一轮';
    const turn = h('div', 'tasks-memory-turn');
    turn.textContent = view.turn;
    host.appendChild(label);
    host.appendChild(turn);
  }
}

function requestSessionSummary(finish) {
  let pending;
  try {
    pending = fetch('/session/summary');
  } catch (err) {
    finish(false, null);
    return;
  }
  Promise.resolve(pending).then(function (response) {
    if (!response || !response.ok || typeof response.json !== 'function') {
      finish(false, null);
      return null;
    }
    return response.json().then(function (payload) {
      finish(true, payload);
    }, function () { finish(false, null); });
  }, function () { finish(false, null); });
}
