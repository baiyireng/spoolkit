// spool web · js/render/checkpoint_list.js
import "../core/dom.js";
/* 任务面板里的未完成文档。只画可续写元数据，不画 state、正文，也不提供继续。 */

function checkpointItems(payload) {
  if (!Array.isArray(payload)) { return null; }
  const rows = [];
  for (let index = 0; index < payload.length; index += 1) {
    const item = payload[index];
    if (!item || typeof item !== 'object') { continue; }
    const title = String(item.title || '').trim();
    const goal = String(item.goal || '').trim();
    rows.push({
      title: title || goal,
      path: String(item.path || ''),
      phase: String(item.phase || ''),
      status: String(item.status || ''),
      updatedAt: String(item.updated_at || ''),
    });
  }
  return rows;
}

function paintCheckpoint(item) {
  const row = h('div', 'checkpoint-line');
  const title = h('div', 'checkpoint-title');
  title.textContent = item.title || '未命名文档';
  row.appendChild(title);
  const path = h('div', 'checkpoint-path');
  path.textContent = item.path || '';
  if (item.path) { path.title = item.path; }
  row.appendChild(path);
  const meta = h('div', 'checkpoint-meta');
  const phase = h('span', 'checkpoint-phase');
  phase.textContent = item.phase || '';
  const status = h('span', 'checkpoint-status');
  status.textContent = item.status || '';
  const updated = h('span', 'checkpoint-updated');
  updated.textContent = item.updatedAt || '';
  meta.appendChild(phase);
  meta.appendChild(status);
  meta.appendChild(updated);
  row.appendChild(meta);
  return row;
}

function paintCheckpointList(host, state, rows, retry) {
  if (!host) { return; }
  host.replaceChildren();
  if (state === 'loading') {
    const note = h('div', 'tasks-empty');
    note.textContent = '正在加载未完成文档';
    host.appendChild(note);
    return;
  }
  if (state === 'error') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tasks-checkpoints-retry';
    button.textContent = '未完成文档加载失败，可重试';
    button.onclick = function () { retry(); };
    host.appendChild(button);
    return;
  }
  const items = rows || [];
  if (!items.length) {
    const empty = h('div', 'tasks-empty');
    empty.textContent = '没有未完成文档';
    host.appendChild(empty);
    return;
  }
  for (let index = 0; index < items.length; index += 1) {
    host.appendChild(paintCheckpoint(items[index]));
  }
}
