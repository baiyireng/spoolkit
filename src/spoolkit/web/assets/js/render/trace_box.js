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

// 标题那一行：几行 + **有没有失败**。一行都没有时也要说得出"这里现在是空的"，
// 而失败必须直接可见（藏在收起块里的失败等于没提示）。
function drawTraceHead(box) {
  const lines = box._lines || [];
  let text = '思考过程 · ' + lines.length + ' 行';
  for (const one of lines) {
    if (one.ok === false) { text += ' · 有失败'; break; }
  }
  box._head.textContent = text;
  box._head.title = text + '（点开查看本轮步骤、工具调用与失败原因）';
}

// 正文：**只追加没画过的那几行**（增量追加，重复到达不会叠）。收起时不建节点。
function drawTraceBody(box) {
  const lines = box._lines || [];
  if (box._body.children.length > lines.length) { box._body.replaceChildren(); }
  for (let index = box._body.children.length; index < lines.length; index += 1) {
    box._body.appendChild(traceLineNode(lines[index]));
  }
}

function traceLineText(line) {
  const kind = String(line.type || '');
  const text = String(line.text || '');
  if (kind === 'final') { return (line.ok ? '完成：' : '失败：') + text; }
  if (kind === 'error') { return '错误：' + text; }
  return text;
}

function traceLineNode(line) {
  const node = document.createElement('div');
  const bad = line.ok === false || line.type === 'error';
  node.className = 'trace-line'
    + (bad ? ' bad' : (line.type === 'final' ? ' ok' : ''));
  // 上屏一律先遮蔽密钥（与服务端那份同一口径）。
  node.textContent = redact(traceLineText(line));
  if (line.detail) {
    const detail = document.createElement('div');
    detail.className = 'trace-detail';
    detail.textContent = redact(String(line.detail));
    node.appendChild(detail);
  }
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
