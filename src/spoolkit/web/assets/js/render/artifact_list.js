// spool web · js/render/artifact_list.js
import "../core/dom.js";
/* 任务面板里的产物行。只画元数据，不画正文，也不提供打开或继续。 */
const ARTIFACT_KINDS = {
  document: '文档',
  report: '报告',
  code: '代码',
  output: '输出',
};

function artifactKind(kind) {
  const name = String(kind || '');
  return ARTIFACT_KINDS[name] || name || '产物';
}

function copyArtifactPath(path, button) {
  const fail = function () { button.textContent = '复制失败'; };
  try {
    const clip = (typeof navigator !== 'undefined' && navigator.clipboard) ? navigator.clipboard : null;
    if (clip && typeof clip.writeText === 'function') {
      Promise.resolve().then(function () { return clip.writeText(path); }).then(
        function () { button.textContent = '已复制'; },
        fail
      );
      return;
    }
  } catch (err) {
    fail();
    return;
  }
  fail();
}

function paintArtifact(item) {
  const row = h('div', 'artifact-line');
  const head = h('div', 'artifact-line-head');
  const chip = h('span', 'task-chip artifact-kind');
  chip.textContent = artifactKind(item.kind);
  const title = h('span', 'artifact-title');
  title.textContent = item.title || '未命名产物';
  head.appendChild(chip);
  head.appendChild(title);
  row.appendChild(head);
  const summary = h('div', 'artifact-summary');
  summary.textContent = item.summary || '';
  row.appendChild(summary);
  const pathRow = h('div', 'artifact-path-row');
  const path = h('span', 'artifact-path');
  path.textContent = item.path || '';
  if (item.path) {
    path.title = item.path;
    path.setAttribute('title', item.path);
  }
  pathRow.appendChild(path);
  if (item.path) {
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'artifact-copy';
    copy.textContent = '复制路径';
    copy.setAttribute('aria-label', '复制路径');
    copy.onclick = function () { copyArtifactPath(item.path, copy); };
    pathRow.appendChild(copy);
  }
  row.appendChild(pathRow);
  return row;
}

function paintArtifactList(host, count, state, rows, retry) {
  if (!host) { return; }
  host.replaceChildren();
  if (state === 'loading') {
    if (count) { count.textContent = ''; }
    const note = h('div', 'tasks-empty');
    note.textContent = '正在加载产物';
    host.appendChild(note);
    return;
  }
  if (state === 'error') {
    if (count) { count.textContent = ''; }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tasks-artifacts-retry';
    button.textContent = '产物加载失败，可重试';
    button.onclick = function () { retry(); };
    host.appendChild(button);
    return;
  }
  const items = rows || [];
  if (count) { count.textContent = items.length + ' 个产物'; }
  if (!items.length) {
    const empty = h('div', 'tasks-empty');
    empty.textContent = '当前会话还没有产物';
    host.appendChild(empty);
    return;
  }
  for (let index = 0; index < items.length; index += 1) {
    host.appendChild(paintArtifact(items[index]));
  }
}
