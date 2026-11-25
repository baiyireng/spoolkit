// spool web · js/render/checkpoint_list.js
import "../core/dom.js";
/* 任务面板里的未完成文档。继续只提交 checkpoint 键，不画 state 和正文。 */

let resumeBusy = false;
let resumeActive = '';
let resumeError = '';
let resumeArmed = false;
let resumeOnEnd = function () {};
let resumePaint = function () {};
let resumeReload = function () {};
let resumeCtx = null;

function bindCheckpointResume(onEnd, paint) {
  resumeOnEnd = onEnd || function () {};
  resumePaint = paint || function () {};
}

function watchCheckpointRun(running) {
  if (resumeArmed && !running) {
    resumeArmed = false;
    resumeOnEnd();
  }
}

function checkpointItems(payload) {
  if (!Array.isArray(payload)) { return null; }
  const rows = [];
  for (let index = 0; index < payload.length; index += 1) {
    const item = payload[index];
    if (!item || typeof item !== 'object') { continue; }
    const title = String(item.title || '').trim();
    const goal = String(item.goal || '').trim();
    rows.push({
      checkpointKey: String(item.checkpoint_key || '').trim(),
      title: title || goal,
      path: String(item.path || ''),
      phase: String(item.phase || ''),
      status: String(item.status || ''),
      updatedAt: String(item.updated_at || ''),
    });
  }
  return rows;
}

function checkpointCanResume(item) {
  const key = String((item && item.checkpointKey) || '');
  if (!key || key === '.' || key === '..') { return false; }
  if (key.indexOf('/') >= 0 || key.indexOf('\\') >= 0 || key.indexOf(':') >= 0 || key.indexOf('..') >= 0) {
    return false;
  }
  const status = String((item && item.status) || '');
  return status === 'paused' || status === 'running' || status === 'stopped';
}

function resumeMessage(status) {
  if (status === 400) { return '请求不合法'; }
  if (status === 403) { return '只读：只能看，不能驱动'; }
  if (status === 404) { return '这份未完成文档已不可用'; }
  if (status === 409) { return '已有运行在进行，等它结束再发'; }
  return '继续失败，可重试';
}

function finishResume(message, reload) {
  resumeBusy = false;
  resumeActive = '';
  resumeError = message || '';
  resumePaint();
  if (reload) { resumeReload(); }
}

function startCheckpointResume(key) {
  if (resumeBusy) { return; }
  if (!checkpointCanResume({ checkpointKey: key, status: 'paused' })) { return; }
  resumeBusy = true;
  resumeActive = key;
  resumeError = '';
  resumePaint();
  let pending;
  try {
    pending = fetch('/longform/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ checkpoint_key: key }),
    });
  } catch (err) {
    finishResume('继续失败，可重试', false);
    return;
  }
  Promise.resolve(pending).then(function (response) {
    if (!response) {
      finishResume('继续失败，可重试', false);
      return;
    }
    const status = Number(response.status || 0);
    if (status === 202) {
      resumeBusy = false;
      resumeActive = '';
      resumeError = '';
      resumeArmed = true;
      resumePaint();
      const composer = resumeCtx && resumeCtx.composer;
      if (composer) {
        composer.setThinking(true);
        composer.setRunning(true);
      }
      return;
    }
    finishResume(resumeMessage(status), status === 404);
  }, function () {
    finishResume('继续失败，可重试', false);
  });
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
  if (checkpointCanResume(item)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'checkpoint-resume';
    const active = resumeBusy && resumeActive === item.checkpointKey;
    button.textContent = active ? '继续中…' : '继续';
    button.disabled = Boolean(resumeBusy);
    button.onclick = function (event) {
      if (event && event.stopPropagation) { event.stopPropagation(); }
      startCheckpointResume(item.checkpointKey);
    };
    row.appendChild(button);
  }
  return row;
}

function paintResumeError(host) {
  if (!resumeError) { return; }
  const note = h('div', 'checkpoint-resume-error');
  note.textContent = resumeError;
  host.appendChild(note);
}

function paintCheckpointList(host, state, rows, retry, ctx) {
  if (ctx) { resumeCtx = ctx; }
  if (typeof retry === 'function') { resumeReload = retry; }
  if (!host) { return; }
  host.replaceChildren();
  if (state === 'loading') {
    const note = h('div', 'tasks-empty');
    note.textContent = '正在加载未完成文档';
    host.appendChild(note);
    paintResumeError(host);
    return;
  }
  if (state === 'error') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tasks-checkpoints-retry';
    button.textContent = '未完成文档加载失败，可重试';
    button.onclick = function () { retry(); };
    host.appendChild(button);
    paintResumeError(host);
    return;
  }
  const items = rows || [];
  if (!items.length) {
    const empty = h('div', 'tasks-empty');
    empty.textContent = '没有未完成文档';
    host.appendChild(empty);
    paintResumeError(host);
    return;
  }
  for (let index = 0; index < items.length; index += 1) {
    host.appendChild(paintCheckpoint(items[index]));
  }
  paintResumeError(host);
}
