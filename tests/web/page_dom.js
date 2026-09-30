/* 网页壳的**行为**测试台：把内联脚本放进一个够用的假 DOM 里跑。

   为什么需要它：`test_page.py` 那一层量的是"文本里有没有那个分支"，而这一批
   要钉住的东西是**行为**——"同一帧重复到达时一个节点都不重建""点了行立刻亮选中"
   "历史只追加不重画"。文本层量不出这些：把 `renderSidebar` 改回
   `replaceChildren()` 全量重画，文本断言照样全绿。

   它不是浏览器：不排版、不画、不算样式。所以它**证伪不了**视觉（颜色、间距、
   圆角、滚动条外观）——那些只能在真浏览器里看。它能量的是"脚本对 DOM 做了什么"：
   建了几个节点、换了哪几个、谁被点亮了、认不认得出"连不上模型"。

   用法：node page_dom.js <page.html>，往 stdout 打一段 JSON（断言在
   `test_page_dom.py` 里）。node 不在 PATH 时那条用例跳过（与 `node --check` 同一个态度）。
*/

const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.slice(html.indexOf('<script>') + '<script>'.length,
  html.lastIndexOf('</script>'));

const stats = { created: 0, rows: 0, sidebar: 0, stream: 0, select: 0, replaced: 0, attached: 0 };
function snap() { return JSON.parse(JSON.stringify(stats)); }
function reset() { for (const key of Object.keys(stats)) { stats[key] = 0; } }
function since(before) {
  const out = {};
  for (const key of Object.keys(stats)) { out[key] = stats[key] - before[key]; }
  return out;
}

class ClassList {
  constructor(el) { this.el = el; }
  _list() { return String(this.el._cls || '').split(/\s+/).filter(Boolean); }
  _set(list) { this.el._cls = list.join(' '); }
  add(...names) { const list = this._list(); for (const name of names) { if (list.indexOf(name) < 0) { list.push(name); } } this._set(list); }
  remove(...names) { this._set(this._list().filter((name) => names.indexOf(name) < 0)); }
  contains(name) { return this._list().indexOf(name) >= 0; }
  toggle(name, force) {
    const want = force === undefined ? !this.contains(name) : Boolean(force);
    if (want) { this.add(name); } else { this.remove(name); }
  }
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this._cls = '';
    this._text = '';
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.value = '';
    this.disabled = false;
    this.className = '';
    this.classList = new ClassList(this);
    this.listeners = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    stats.created += 1;
  }
  get options() { return this.children.filter((kid) => kid.tagName === 'OPTION'); }
  get childNodes() { return this.children; }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  get lastChild() { return this.children.length ? this.children[this.children.length - 1] : null; }
  get className() { return this._cls; }
  set className(value) { this._cls = String(value === undefined || value === null ? '' : value); }
  get textContent() {
    // 与真 DOM 同一个意思：连子孙一起算（正文 div 里装的是文本节点）。
    if (!this.children.length) { return this._text; }
    return this._text + this.children.map((kid) => kid.textContent).join('');
  }
  set textContent(value) { this._text = String(value === undefined || value === null ? '' : value); this.children = []; }
  set innerHTML(value) { this._text = String(value); this.children = []; }
  get innerHTML() { return this._text; }
  _rootId() {
    let node = this;
    while (node) { if (node._id) { return node._id; } node = node.parentNode; }
    return '';
  }
  _count(node) {
    stats.attached += 1;
    if (/(^|\s)(li|grp-name|empty-line|grp-sub)(\s|$)/.test(String(node._cls || ''))) { stats.rows += 1; }
    const root = this._rootId();
    if (root === 'workspaces' || root === 'sessions') { stats.sidebar += 1; }
    else if (root === 'stream') { stats.stream += 1; }
    else if (root === 'ws' || root === 'sess') { stats.select += 1; }
  }
  appendChild(node) {
    if (node === undefined || node === null) { return node; }
    node.parentNode = this;
    this.children.push(node);
    this._count(node);
    return node;
  }
  replaceChildren(...nodes) { stats.replaced += 1; this.children = []; for (const node of nodes) { this.appendChild(node); } }
  insertBefore(node, ref) {
    if (!ref) { return this.appendChild(node); }
    const at = this.children.indexOf(ref);
    node.parentNode = this;
    this.children.splice(at < 0 ? this.children.length : at, 0, node);
    this._count(node);
    return node;
  }
  replaceChild(node, ref) {
    const at = this.children.indexOf(ref);
    if (at < 0) { return this.appendChild(node); }
    this.children.splice(at, 1);
    ref.parentNode = null;
    node.parentNode = this;
    this.children.splice(at, 0, node);
    this._count(node);
    return ref;
  }
  removeChild(node) {
    const at = this.children.indexOf(node);
    if (at >= 0) { this.children.splice(at, 1); node.parentNode = null; }
    return node;
  }
  _find(sel) {
    const want = String(sel).replace(/^\./, '');
    const isClass = String(sel).startsWith('.');
    for (const kid of this.children) {
      const hit = isClass
        ? String(kid._cls || '').split(/\s+/).indexOf(want) >= 0
        : kid.tagName === want.toUpperCase();
      if (hit) { return kid; }
      const deeper = kid._find(sel);
      if (deeper) { return deeper; }
    }
    return null;
  }
  querySelector(sel) {
    // 支持 "a b"（后代）：这个假 DOM 不做真正的选择器解析，够这一层用就行。
    return String(sel).split(/\s+/).reduce(
      (node, part) => (node ? node._find(part) : null), this);
  }
  querySelectorAll(sel) {
    const want = String(sel).replace(/^\./, '');
    const out = [];
    const walk = (node) => {
      for (const kid of node.children) {
        if (String(kid._cls || '').split(/\s+/).indexOf(want) >= 0) { out.push(kid); }
        walk(kid);
      }
    };
    walk(this);
    return out;
  }
  addEventListener(kind, fn) { (this.listeners[kind] = this.listeners[kind] || []).push(fn); }
  dispatchEvent(event) { (this.listeners[event.type] || []).forEach((fn) => fn.call(this, event)); return true; }
  scrollTo() { this.scrollTop = this.scrollHeight; }
}

class Event {
  constructor(type) { this.type = type; }
  stopPropagation() { this._stopped = true; }
}

// 静态 HTML 里带 id 的元素先摆好：脚本一上来就 getElementById 它们。
const ids = new Map();
for (const match of html.matchAll(/id="([^"]+)"/g)) {
  const el = new El(match[1].startsWith('ws') || match[1] === 'sess' ? 'select' : 'div');
  el._id = match[1];
  ids.set(match[1], el);
}
const body = new El('body');
body._id = 'body';

const sources = [];
class EventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.onopen = null;
    sources.push(this);
  }
}

const document = {
  body,
  getElementById: (id) => ids.get(id) || null,
  createElement: (tag) => new El(tag),
  createTextNode: (text) => { const node = new El('text'); node.textContent = text; return node; },
  querySelectorAll: (sel) => {
    const out = [];
    for (const root of ids.values()) { out.push(...root.querySelectorAll(sel)); }
    return out;
  },
};

const ctx = {
  document,
  EventSource,
  Event,
  navigator: { clipboard: { writeText: () => Promise.resolve() } },
  console,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearInterval: () => {},
  prompt: () => null,
  fetchCalls: [],
  __nextBody: null,
  __fetchImpl: null,
};
ctx.window = ctx;
ctx.fetch = async (url) => {
  ctx.fetchCalls.push(url);
  if (ctx.__fetchImpl) { return ctx.__fetchImpl(url); }
  return { ok: true, status: 200, json: async () => ctx.__nextBody };
};
vm.createContext(ctx);
vm.runInContext(script, ctx, { filename: 'page.js' });

/* ── 造状态帧 ── */

const ROOT_A = 'D:/ws-a';
const ROOT_B = 'D:/ws-b';

function frame(root, session, workspaces, sessions, messages, extra) {
  return Object.assign({
    target: { root: root, session: session, pinned: false, workspace_source: 'explicit', session_source: 'explicit', note: '' },
    workspaces: workspaces,
    sessions: sessions,
    sessions_removed: [],
    readonly: false,
    host: '127.0.0.1',
    running: false,
    running_targets: [],
    messages: messages,
    usage: { steps: 1 },
    diffs: [],
    awaiting: 0,
    awaiting_detail: {},
    pairings: [],
    health: { model: { ok: true, target: '', detail: '假模型（不连本机端口）' }, services: [] },
  }, extra || {});
}

function history(count, prefix) {
  const out = [];
  for (let index = 0; index < count; index += 1) {
    out.push({ role: index % 2 === 0 ? 'user' : 'assistant', content: prefix + index, meta: '' });
  }
  return out;
}

const WROWS = [
  { name: 'ws-a', alias: '', root: ROOT_A, ready: true, current: true },
  { name: 'ws-b', alias: '', root: ROOT_B, ready: true, current: false },
];
const SROWS_A = [1, 2, 3, 4].map((n) => ({ id: 'a' + n, title: 'A 的第 ' + n + ' 条', archived: false, last_active: n, current: n === 1 }));
// 换工作区时服务端**沿用会话名**（同一个名字在新工作区里是另一份记忆），
// 所以 B 这一帧的会话名与 A 当前那个一样——这正是真实的那一帧长什么样。
const SROWS_B = [{ id: 'a1', title: 'B 里的同名会话', archived: false, last_active: 1, current: true }];
const FRAME_A = frame(ROOT_A, 'a1', WROWS, SROWS_A, history(20, 'a'));
const FRAME_B = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(6, 'b'));

function highlighted() {
  const found = [];
  for (const boxId of ['workspaces', 'sessions']) {
    const walk = (node) => {
      for (const kid of node.children) {
        if (String(kid._cls || '').split(/\s+/).indexOf('on') >= 0) {
          const name = kid.querySelector('.nm');
          found.push(name ? name.textContent : '(无名)');
        }
        walk(kid);
      }
    };
    walk(ids.get(boxId));
  }
  return found;
}

function rowByName(boxId, label) {
  for (const node of ids.get(boxId).querySelectorAll('.li')) {
    const name = node.querySelector('.nm');
    if (name && name.textContent === label) { return node; }
  }
  return null;
}

function streamBubbles() { return ids.get('stream').querySelectorAll('.bubble').length; }
function streamCodes() { return ids.get('stream').querySelectorAll('.code').length; }
function streamProse() { return ids.get('stream').querySelectorAll('.prose').length; }

const out = {};

// ①-1 首帧之后，**同一帧再来 5 次**：一个节点都不该新建（同键跳过）。
ctx.applyState(FRAME_A);
const beforeRepeat = snap();
for (let index = 0; index < 5; index += 1) { ctx.applyState(FRAME_A); }
out.repeat_frame = since(beforeRepeat);

// ①-2 点一行工作区：**同步**就该亮起来（回帧还没到），而且不重建节点。
const beforeClick = snap();
ctx.__fetchImpl = () => new Promise(() => {});   // 回帧永远不到（模拟服务端在探活）
rowByName('workspaces', 'ws-b').onclick();
out.optimistic_selected = highlighted();
out.optimistic_cost = since(beforeClick);
ctx.__fetchImpl = null;

// ①-3 切换：回帧 + 服务端补的那一帧，各重建了多少。
ctx.__nextBody = FRAME_B;
const beforeSwitch = snap();
ctx.switchTarget({ workspace: ROOT_B }).then(() => {
  out.switch_reply = since(beforeSwitch);
  out.switch_selected = highlighted();
  const beforeDup = snap();
  ctx.applyState(FRAME_B);
  out.switch_duplicate = since(beforeDup);

  // ①-4 同一目标、消息变长了两条：只补那两条，侧栏不动。
  const bubbles = streamBubbles();
  const beforeGrow = snap();
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(8, 'b')));
  out.history_grew = since(beforeGrow);
  out.history_added = streamBubbles() - bubbles;

  // ①-5 3 秒轮询（refreshBanners）：横幅之外不该有重建。
  ctx.__nextBody = FRAME_B;
  const beforePoll = snap();
  ctx.refreshBanners().then(() => {
    out.poll = since(beforePoll);

    // ③ 代码块与正文分开：一段"正文 + 围栏代码 + 正文 + 行内代码"
    const bubble = document.createElement('div');
    ctx.fillRich(bubble, [
      '先说一句 `spool status` 之类的行内代码。',
      '```powershell',
      'spool web',
      '```',
      '结尾这一句是正文。',
    ].join('\n'));
    out.rich = {
      code_blocks: bubble.querySelectorAll('.code').length,
      prose: bubble.querySelectorAll('.prose').length,
      inline: bubble.querySelectorAll('.inline-code').length,
      lang: bubble.querySelector('.code-lang').textContent,
      code_text: bubble.querySelector('.code pre code').textContent,
      first_prose_text: bubble.querySelector('.prose').textContent,
      second_prose_text: bubble.querySelectorAll('.prose')[1].textContent,
    };

    // ② 连不上模型：结构化线索 → 人话
    const parts = ctx.explainFailure(
      'spoolkit.llm.llamacpp.LlamaCppError: 无法连接 llama-server（ConnectTimeout），'
      + '请确认服务已启动并监听正确端口',
      { stage: 'model', kind: 'connect', url: 'http://127.0.0.1:8080' }
    );
    out.failure = parts;
    out.failure_by_text = ctx.explainFailure('httpx.ConnectError: Connection refused', null) !== null;
    out.failure_unknown = ctx.explainFailure('工具 read_file 失败：文件不存在', null);
    out.redact = ctx.redact('api_key=sk-abcdef123456 Bearer abcdefghijklmn token=zzz9');
    out.address_fallback = ctx.explainFailure('Connection refused', null).lines[0];

    // 画出来之后长什么样：认不出来的是"原来的词 + 原文"，认出来的是块。
    ctx.showFailure('失败：', '工具 read_file 失败：文件不存在', null);
    const plain = ids.get('stream').querySelectorAll('.sys-line');
    out.failure_plain = plain[plain.length - 1].textContent;
    const before = ids.get('stream').querySelectorAll('.failure').length;
    ctx.showFailure('失败：', 'LlamaCppError: 无法连接 llama-server', {
      kind: 'connect', url: 'http://127.0.0.1:9',
    });
    out.failure_blocks = ids.get('stream').querySelectorAll('.failure').length - before;

    console.log(JSON.stringify(out, null, 2));
  });
});
