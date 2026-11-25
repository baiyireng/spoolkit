// spool web · js/render/code.js
import "../core/dom.js";
import "./markdown.js";
function buildColumn() {
  const col = document.createElement('div');
  col.className = 'col';
  stream.appendChild(col);
  return col;
}

// 挂到对话列上。返回挂进去的那一块，调用方自己决定要不要滚。
function appendNode(node) {
  let col = stream.querySelector('.col');
  if (col === null) { col = buildColumn(); }
  col.appendChild(node);
  return node;
}

function makeMsg(kind) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + kind;
  appendNode(wrap);
  return wrap;
}

function makeBubble(kind, text, cls) {
  const wrap = makeMsg(kind);
  if (cls) { wrap.classList.add(cls); }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  return bubble;
}

// 步骤 / 工具 / 用量这些"系统说的一行"——左对齐的细字，不是气泡：
// 它们不是"助手说的话"，混进气泡里会让人以为模型在自言自语。
function makeSystem(text, cls) {
  const wrap = makeMsg('system');
  const line = document.createElement('div');
  line.className = 'sys-line ' + (cls || '');
  line.textContent = text;
  wrap.appendChild(line);
  return wrap;
}

// 助手的话里有三类内容要分开画：**正文**、**围栏代码块**（``` 围起来）、
// **行内代码**（` 围起来）。全都用 textContent 装配而不是拼 innerHTML：
// 模型输出是不可信输入，拼标签等于把页面交给它。
//
// 分成"奇数块 = 代码"是 `split('```')` 的性质：围栏一定成对，割出来就是
// [正文, 代码, 正文, 代码, …]。以前每一块都被当成代码块画（连正文也是），
// 于是整条消息看上去全是等宽的代码——这正是"代码与对话分不开"的根。
function fillRich(bubble, content) {
  const text = String(content === null || content === undefined ? '' : content);
  const parts = text.split('```');
  for (let index = 0; index < parts.length; index += 1) {
    if (index % 2 === 1) {
      bubble.appendChild(codeBlock(parts[index]));
      continue;
    }
    appendProse(bubble, parts[index]);
  }
}

// 正文：按 ` 再切成"普通字"与"行内代码"，一句一段（换行保留）。
function appendProse(bubble, raw) {
  const text = raw.replace(/^\r?\n/, '').replace(/\r?\n$/, '');
  if (text === '') { return; }
  // 反引号不再挡住块级：标题、列表、表格和行内代码可以出现在同一段里。
  // 围栏已经在 fillRich 里按 ``` 切开，这里看到的是正文，不是代码块。
  if (hasBlockMarkdown(text)) {
    renderMarkdownBlocks(bubble, parseMarkdown(text));
    return;
  }
  const para = document.createElement('div');
  para.className = 'prose';
  for (const inline of parseInlines(text)) { appendInline(para, inline); }
  if (para.childNodes.length) { bubble.appendChild(para); }
}

// 围栏代码块：自己滚（横向 + 纵向），复制键固定在右上角，语言标签可选。
function codeBlock(raw) {
  const body = raw.replace(/^\r?\n/, '').replace(/\r?\n$/, '');
  const lines = body.split('\n');
  const first = lines.length > 1 ? lines[0].trim() : '';
  // 第一行是个"一个词"（没空格、不太长）就当语言名：```python / ```powershell。
  const fenced = first !== '' && first.indexOf(' ') === -1 && first.length <= 24;
  const payload = fenced ? lines.slice(1).join('\n') : body;
  const code = document.createElement('div');
  code.className = 'code';
  const bar = document.createElement('div');
  bar.className = 'code-bar';
  const label = document.createElement('span');
  label.className = 'code-lang';
  label.textContent = fenced ? first : '代码';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'copy-btn';
  copy.textContent = '复制';
  copy.onclick = function () { copyText(payload, copy); };
  bar.appendChild(label);
  bar.appendChild(copy);
  const pre = document.createElement('pre');
  const codeEl = document.createElement('code');
  codeEl.textContent = payload;
  pre.appendChild(codeEl);
  code.appendChild(bar);
  code.appendChild(pre);
  // 只标记溢出，不改代码节点。滚到最右就收起提示。
  pre.onscroll = function () { markCodeOverflow(code); };
  if (typeof ResizeObserver === 'function') {
    const watch = new ResizeObserver(function () { markCodeOverflow(code); });
    watch.observe(pre);
  }
  markCodeOverflow(code);
  return code;
}

function markCodeOverflow(block) {
  const pre = block && block.querySelector ? block.querySelector('pre') : null;
  if (!pre || !block.classList) { return; }
  const width = Number(pre.clientWidth) || 0;
  const full = Number(pre.scrollWidth) || 0;
  const left = Number(pre.scrollLeft) || 0;
  const more = width > 0 && full > width + 1 && (left + width) < (full - 1);
  block.classList.toggle('code-x', more);
}

// 复制：剪贴板优先，失败再用一次性文本框走 execCommand，
// 还不行就选中代码并提示手动复制。任何一步失败都不往外抛。
function copyWithCommand(payload) {
  if (!document.body || typeof document.execCommand !== 'function') { return false; }
  const area = document.createElement('textarea');
  area.value = payload;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.left = '-9999px';
  let ok = false;
  try {
    document.body.appendChild(area);
    if (typeof area.focus === 'function') { area.focus(); }
    if (typeof area.select === 'function') { area.select(); }
    if (typeof area.setSelectionRange === 'function') {
      area.setSelectionRange(0, payload.length);
    }
    ok = document.execCommand('copy') !== false;
  } catch (err) {
    ok = false;
  } finally {
    try {
      if (area.parentNode && typeof area.parentNode.removeChild === 'function') {
        area.parentNode.removeChild(area);
      }
    } catch (err) { /* 文本框已经不在了 */ }
  }
  return ok;
}

function selectCode(button) {
  const block = button && button.parentNode && button.parentNode.parentNode;
  if (!block || typeof block.querySelector !== 'function') { return false; }
  const code = block.querySelector('code');
  if (!code) { return false; }
  const selection = (typeof window !== 'undefined' && typeof window.getSelection === 'function')
    ? window.getSelection() : null;
  if (!selection || typeof document.createRange !== 'function') { return false; }
  const range = document.createRange();
  if (!range || typeof range.selectNodeContents !== 'function') { return false; }
  range.selectNodeContents(code);
  if (typeof selection.removeAllRanges === 'function') { selection.removeAllRanges(); }
  if (typeof selection.addRange === 'function') { selection.addRange(range); }
  return true;
}

function copyText(text, button) {
  const payload = String(text == null ? '' : text);
  const mark = function (label) {
    if (button) { button.textContent = label; }
  };
  const manual = function () {
    try { selectCode(button); } catch (err) { /* 选不中也要留下提示 */ }
    mark('请手动复制');
  };
  const fallback = function () {
    let copied = false;
    try { copied = copyWithCommand(payload); } catch (err) { copied = false; }
    if (copied) { mark('已复制'); return; }
    manual();
  };
  try {
    const clip = (typeof navigator !== 'undefined' && navigator.clipboard) ? navigator.clipboard : null;
    if (clip && typeof clip.writeText === 'function') {
      Promise.resolve().then(function () { return clip.writeText(payload); }).then(
        function () { mark('已复制'); },
        function () { fallback(); }
      ).catch(function () { fallback(); });
      return;
    }
  } catch (err) {
    fallback();
    return;
  }
  fallback();
}

function appendInline(parent, inline) {
  if (!inline) { return; }
  if (inline.type === 'code') {
    const node = h('code', 'inline-code');
    node.textContent = inline.text;
    parent.appendChild(node);
    return;
  }
  if (inline.type === 'strong') {
    const node = h('strong');
    node.textContent = inline.text;
    parent.appendChild(node);
    return;
  }
  if (inline.type === 'link') {
    const href = safeHref(inline.href);
    if (!href) {
      parent.appendChild(text(inline.text));
      return;
    }
    const node = h('a');
    node.textContent = inline.text;
    node.setAttribute('href', href);
    node.setAttribute('rel', 'noreferrer');
    parent.appendChild(node);
    return;
  }
  parent.appendChild(text(inline.text || ''));
}

function renderMarkdownBlocks(bubble, blocks) {
  for (const block of blocks || []) {
    if (block.type === 'heading') {
      const level = Math.min(6, Math.max(1, Number(block.level) || 1));
      const node = h('h' + level, 'md-h');
      for (const inline of block.inlines || []) { appendInline(node, inline); }
      bubble.appendChild(node);
      continue;
    }
    if (block.type === 'ul' || block.type === 'ol') {
      const list = h(block.type);
      for (const item of block.items || []) {
        const li = h('li');
        for (const inline of item || []) { appendInline(li, inline); }
        list.appendChild(li);
      }
      bubble.appendChild(list);
      continue;
    }
    if (block.type === 'table') {
      const table = h('table');
      const head = h('thead');
      const headRow = h('tr');
      for (const cell of block.header || []) {
        const th = h('th');
        for (const inline of cell || []) { appendInline(th, inline); }
        headRow.appendChild(th);
      }
      head.appendChild(headRow);
      table.appendChild(head);
      const body = h('tbody');
      for (const row of block.rows || []) {
        const tr = h('tr');
        for (const cell of row) {
          const td = h('td');
          for (const inline of cell || []) { appendInline(td, inline); }
          tr.appendChild(td);
        }
        body.appendChild(tr);
      }
      table.appendChild(body);
      bubble.appendChild(table);
      continue;
    }
    const para = h('div', 'prose');
    for (const inline of block.inlines || []) { appendInline(para, inline); }
    if (para.childNodes.length) { bubble.appendChild(para); }
  }
}
