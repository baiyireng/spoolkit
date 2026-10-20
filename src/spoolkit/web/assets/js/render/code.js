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
  return code;
}

// 复制走 navigator.clipboard；失败就选中那段代码让人自己按 Ctrl+C——
// 不弹第二个对话框（本地壳上"复制"点一下就该完事）。
function copyText(text, button) {
  const done = function (ok) { button.textContent = ok ? '已复制' : '复制未成功'; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function () { done(true); },
      function () { done(false); }
    );
    return;
  }
  done(false);
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
