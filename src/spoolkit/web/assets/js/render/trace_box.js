// spool web · js/render/trace_box.js
import "./failure.js";
/* 思考过程那一块：默认收起，收起时正文一个节点都不留。对话流只负责把它挂到卡片上。 */
// 一个可折叠的轨迹块。收起时**只有标题那一行**（正文块是空的，`hidden`），
// 点开才把行建出来：这是"默认收起、不占版面"在 DOM 层的判据。
function makeTrace() {
  const box = document.createElement('div');
  box.className = 'trace';
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'trace-head';
  head.setAttribute('aria-expanded', 'false');
  const body = document.createElement('div');
  body.className = 'trace-body hidden';
  head.onclick = function () { toggleTrace(box); };
  box.appendChild(head);
  box.appendChild(body);
  box._head = head;
  box._body = body;
  box._lines = [];
  drawTraceHead(box);
  return box;
}

function traceOpen(box) { return !box._body.classList.contains('hidden'); }

function setTraceOpen(box, open) {
  box._body.classList.toggle('hidden', !open);
  box._head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { drawTraceBody(box); }
  else { box._body.replaceChildren(); }  // 收起时不占节点（与「默认收起」同口径）
}

function toggleTrace(box) {
  setTraceOpen(box, !traceOpen(box));
}

// 标题那一行：可见的几步 + **有没有失败**。纯步骤号不占这一格。
// 失败必须直接可见（藏在收起块里的失败等于没提示）。
function drawTraceHead(box) {
  const lines = box._lines || [];
  const count = chainLines(lines).length;
  let text = count ? ('思维链 · ' + count) : '思维链';
  for (const one of lines) {
    if (one && one.type !== 'step' && one.ok === false) { text += ' · 有失败'; break; }
  }
  box._head.textContent = text;
  box._head.title = text + '（点开查看这一轮怎么想的、调了什么、卡在哪）';
  // 只有步骤号、或这一轮根本没有过程时，不留一块空标题。
  box.classList.toggle('trace-empty', count === 0);
}

// 正文按可见行重画。步骤号会被滤掉，不能再用「子节点数 = 原始行数」往下补。
function drawTraceBody(box) {
  box._body.replaceChildren();
  const lines = chainLines(box._lines || []);
  for (let index = 0; index < lines.length; index += 1) {
    box._body.appendChild(traceLineNode(lines[index], index === lines.length - 1));
  }
}

function traceLineText(line) {
  const kind = String(line.type || '');
  const text = String(line.text || '');
  if (kind === 'final') { return (line.ok ? '完成：' : '失败：') + text; }
  if (kind === 'error') { return '错误：' + text; }
  return text;
}

// 「第 0 步」这种空标记，以及【模型结论】一类内部标签，不属于思维链。
// 数据仍留在 `_lines` 里，只是不上屏。
const CHAIN_TAG = /【[^】\n]{1,24}】/g;
const CHAIN_STEP = /^第\s*\d+\s*步$/;

function chainText(line) {
  if (!line || line.type === 'step') { return ''; }
  const text = traceLineText(line).replace(CHAIN_TAG, ' ').replace(/\s+/g, ' ').trim();
  if (!text || CHAIN_STEP.test(text)) { return ''; }
  return text;
}

function chainLines(lines) {
  const shown = [];
  for (let index = 0; index < (lines || []).length; index += 1) {
    if (chainText(lines[index])) { shown.push(lines[index]); }
  }
  return shown;
}

function traceLineNode(line, fresh) {
  const text = redact(chainText(line));
  const node = document.createElement('div');
  const bad = line.ok === false || line.type === 'error';
  node.className = 'trace-line' + (fresh ? ' stream-row' : '')
    + (bad ? ' bad' : (line.type === 'final' ? ' ok' : ''))
    + ' trace-' + String(line.type || 'note');
  const rail = document.createElement('span');
  rail.className = 'trace-rail';
  rail.setAttribute('aria-hidden', 'true');
  const main = document.createElement('div');
  main.className = 'trace-main';
  const tool = line.type === 'tool' ? text.match(/^(.+?)\s*→\s*(.+)$/) : null;
  if (tool) {
    const row = document.createElement('div');
    row.className = 'trace-row';
    const name = document.createElement('span');
    name.className = 'trace-name';
    name.textContent = tool[1];
    const arrow = document.createElement('span');
    arrow.className = 'trace-arrow';
    arrow.textContent = ' → ';
    const status = document.createElement('span');
    status.className = 'trace-status';
    status.textContent = tool[2];
    row.appendChild(name);
    row.appendChild(arrow);
    row.appendChild(status);
    main.appendChild(row);
  } else {
    const body = document.createElement('div');
    body.className = 'trace-text';
    body.textContent = text;
    main.appendChild(body);
  }
  if (line.detail) {
    const detail = document.createElement('div');
    detail.className = 'trace-detail';
    detail.textContent = redact(String(line.detail).replace(CHAIN_TAG, ' ').replace(/\s+/g, ' ').trim());
    main.appendChild(detail);
  }
  node.appendChild(rail);
  node.appendChild(main);
  return node;
}

// 把一份轨迹填进那个块（历史消息与"认领快照"时用）。
function fillTrace(box, lines) {
  box._lines = (lines || []).map(function (one) { return one; });
  drawTraceHead(box);
  if (traceOpen(box)) { drawTraceBody(box); }
}

// 一份轨迹里"失败的那一轮"：`final` / `error` 那行（带着服务端只增的结构化线索）。
// 刷新之后靠它把失败原因与下一步重新画成那一块说明。
function failedTraceLine(lines) {
  for (const one of lines || []) {
    if (one.ok === false && (one.type === 'final' || one.type === 'error')) { return one; }
  }
  return null;
}
