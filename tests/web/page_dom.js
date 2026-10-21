/* 网页壳的**行为**测试台：把内联脚本放进一个够用的假 DOM 里跑。

   为什么需要它：`test_page.py` 那一层量的是"文本里有没有那个分支"，而这一批
   要钉住的东西是**行为**——"同一帧重复到达时一个节点都不重建""点了行立刻亮选中"
   "历史只追加不重画"。文本层量不出这些：把 `renderSidebar` 改回
   `replaceChildren()` 全量重画，文本断言照样全绿。

   它不是浏览器：不排版、不画、不算样式。所以它**证伪不了**视觉（颜色、间距、
   圆角、滚动条外观）——那些只能在真浏览器里看。它能量的是"脚本对 DOM 做了什么"：
   建了几个节点、换了哪几个、谁被点亮了、认不认得出"连不上模型"。

   用法：node page_dom.js <page.html> <test-bundle.js>，往 stdout 打一段 JSON
   （断言在 `test_page_dom.py` 里）。HTML 只提供元素 id；脚本跑 test-bundle，
   测试面在 `ctx.__spoolTest`。node 不在 PATH 时那条用例跳过。
*/

const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
if (!process.argv[3]) {
  console.error('用法：node page_dom.js <page.html> <test-bundle.js>');
  process.exit(2);
}
const script = fs.readFileSync(process.argv[3], 'utf8');

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
    this.attrs = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    stats.created += 1;
  }
  setAttribute(name, value) {
    this.attrs[String(name)] = String(value);
    if (String(name) === 'class') { this.className = value; }
  }
  removeAttribute(name) { delete this.attrs[String(name)]; }
  focus() { document.activeElement = this; }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
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
  scrollIntoView() { this._scrolled = (this._scrolled || 0) + 1; }
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
  const start = html.lastIndexOf('<', match.index);
  const end = html.indexOf('>', match.index);
  const tag = html.slice(start, end);
  for (const attr of tag.matchAll(/\s([a-zA-Z:-]+)="([^"]*)"/g)) {
    if (attr[1] === 'id' || attr[1] === 'class') { continue; }
    el.setAttribute(attr[1], attr[2]);
  }
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

// 脚本排下来的定时器（`setTimeout` 那一支）。只有通知那一层用它。
const timers = [];
function fireTimers() {
  const pending = timers.splice(0, timers.length);
  let fired = 0;
  for (const item of pending) { if (!item.cancelled) { item.fn(); fired += 1; } }
  return fired;
}

const document = {
  body,
  activeElement: null,
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
  // 定时器**留着手动放**：通知那一层的两条关键行为（`info` 到点自己走、
  // 错误到点也不走）只能靠"放一遍定时器"来证伪。
  setTimeout: (fn, ms) => { timers.push({fn: fn, ms: ms, id: timers.length + 1, cancelled: false}); return timers.length; },
  clearTimeout: (id) => {
    for (const item of timers) { if (item.id === id) { item.cancelled = true; } }
  },
  prompt: () => null,
  fetchCalls: [],
  __nextBody: null,
  __fetchImpl: null,
};
ctx.window = ctx;
ctx.fetch = async (url, opts) => {
  ctx.fetchCalls.push(url);
  if (ctx.__fetchImpl) { return ctx.__fetchImpl(url, opts); }
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

function highlightedSub() {
  for (const boxId of ['workspaces', 'sessions']) {
    const walk = (node) => {
      for (const kid of node.children) {
        if (String(kid._cls || '').split(/\s+/).indexOf('on') >= 0) {
          const sub = kid.querySelector('.src');
          if (sub) { return sub.textContent; }
        }
        const deeper = walk(kid);
        if (deeper) { return deeper; }
      }
      return '';
    };
    const found = walk(ids.get(boxId));
    if (found) { return found; }
  }
  return '';
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

/* ── 通知（右上角的浮动气泡）与待确认卡片：读它们长什么样的几个函数 ── */

function allNodes(root) {
  const found = [];
  const walk = (node) => {
    for (const kid of node.children) { found.push(kid); walk(kid); }
  };
  walk(root);
  return found;
}

function toastNodes() { return ids.get('toasts').querySelectorAll('.toast'); }
function toastKeys() { return toastNodes().map((node) => String(node.dataset.key)); }
function toastWith(prefix) {
  for (const node of toastNodes()) {
    if (String(node.dataset.key).indexOf(prefix) === 0) { return node; }
  }
  return null;
}
// 只读**正文那块**的文字：关闭键里是内联 SVG，假 DOM 会把那段标记也算进
// textContent（真 DOM 里它不是一个文本节点），比的是人看得见的那几行。
function toastBody(node) {
  if (!node) { return ''; }
  const body = node.querySelector('.toast-body');
  return body ? body.textContent : '';
}
function toastText(prefix) { return toastBody(toastWith(prefix)); }
function closeButtons() { return ids.get('toasts').querySelectorAll('.toast-close'); }
// 关闭键里只有一个图标：读屏得有个名字（`aria-label`）。
function closeLabels() { return closeButtons().map((node) => node.getAttribute('aria-label')); }
function toastsShown() { return ids.get('toasts').classList.contains('show'); }
function allButtonShown() { return ids.get('toast-all').classList.contains('show'); }

/* ── 状态区（输入条那一行）与详情面板：读它长什么样的几个函数 ── */

function stNode(id) { return ids.get(id); }
// 徽标是不是"看得见"：假 DOM 不算样式，判据是那个类名在不在（真页面里它是 display:none）。
function stShown(id) { return !ids.get(id).classList.contains('hidden'); }
function stLabel(id) {
  const node = ids.get(id).querySelector('.st-label');
  return node ? node.textContent : '';
}
function visibleBadges() {
  return ['st-health', 'st-mode', 'st-target', 'st-pair', 'st-elsewhere']
    .filter((id) => stShown(id));
}
function panelOpen() { return !ids.get('st-panel').classList.contains('hidden'); }
function panelText() { return ids.get('st-panel').textContent; }
// 每个文件头（路径 + 加了几行减了几行）。正文在可折叠块里，头是按钮。
function diffHeads() {
  return ids.get('stream').querySelectorAll('.diff-file-head').map(
    (node) => node.textContent);
}
function diffFolds() {
  return ids.get('stream').querySelectorAll('.diff-file').map((node) => {
    const head = node.querySelector('.diff-file-head');
    return {
      open: node.classList.contains('open'),
      expanded: head ? head.getAttribute('aria-expanded') : '',
      head: head ? head.textContent : '',
    };
  });
}
function confirmButtonCounts() {
  return {
    apply_buttons: buttonsWith('应用全部改动').length + buttonsWith('允许执行').length,
    reject_buttons: buttonsWith('拒绝全部').length + buttonsWith('拒绝').length,
  };
}

// 按**文字**数按钮：卡片的「应用 / 拒绝」没有静态 id（没有待确认内容时连节点都不建），
// 只能靠"页面上有没有这两个字的按钮"来证伪。
function buttonsWith(label) {
  const found = [];
  for (const root of ids.values()) {
    for (const node of allNodes(root)) {
      if (node.tagName === 'BUTTON' && node.textContent === label) { found.push(node); }
    }
  }
  return found;
}

function inStream(node) { return node ? node._rootId() : ''; }

const out = {};

// ①-1 首帧之后，**同一帧再来 5 次**：一个节点都不该新建（同键跳过）。
const applyNotices = [];
ctx.__spoolTest.store.subscribe(function (keys) { applyNotices.push(keys.slice()); });
ctx.__spoolTest.applyState(FRAME_A);
out.apply_running = ctx.__spoolTest.store.get('running');
out.apply_session = ctx.__spoolTest.store.get('session');
out.apply_last_session = ctx.__spoolTest.store.get('lastState').target.session;
out.apply_first_keys = applyNotices[0] || [];
const beforeRepeat = snap();
for (let index = 0; index < 5; index += 1) { ctx.__spoolTest.applyState(FRAME_A); }
out.repeat_frame = since(beforeRepeat);

// ①-2 点一行工作区：**同步**就该亮起来（回帧还没到），而且不重建节点。
const beforeClick = snap();
ctx.__fetchImpl = () => new Promise(() => {});   // 回帧永远不到（模拟服务端在探活）
rowByName('workspaces', 'ws-b').onclick();
out.optimistic_selected = highlighted();
out.optimistic_chosen = ctx.__spoolTest.store.get('chosen');
out.optimistic_cost = since(beforeClick);
ctx.__fetchImpl = null;

// ①-3 切换：回帧 + 服务端补的那一帧，各重建了多少。
ctx.__nextBody = FRAME_B;
const beforeSwitch = snap();
ctx.__spoolTest.switchTarget({ workspace: ROOT_B }).then(() => {
  out.switch_reply = since(beforeSwitch);
  out.switch_selected = highlighted();
  const beforeDup = snap();
  ctx.__spoolTest.applyState(FRAME_B);
  out.switch_duplicate = since(beforeDup);

  // ①-4 同一目标、消息变长了两条：只补那两条，侧栏不动。
  const bubbles = streamBubbles();
  const beforeGrow = snap();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(8, 'b')));
  out.history_grew = since(beforeGrow);
  out.history_added = streamBubbles() - bubbles;

  // ①-5 3 秒轮询（refreshStatus）：状态区之外不该有重建。
  ctx.__nextBody = FRAME_B;
  const beforePoll = snap();
  ctx.__spoolTest.refreshStatus().then(() => {
    out.poll = since(beforePoll);

    // ③ 代码块与正文分开：一段"正文 + 围栏代码 + 正文 + 行内代码"
    const bubble = document.createElement('div');
    ctx.__spoolTest.fillRich(bubble, [
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
    const parts = ctx.__spoolTest.explainFailure(
      'spoolkit.llm.llamacpp.LlamaCppError: 无法连接 llama-server（ConnectTimeout），'
      + '请确认服务已启动并监听正确端口',
      { stage: 'model', kind: 'connect', url: 'http://127.0.0.1:8080' }
    );
    out.failure = parts;
    out.failure_by_text = ctx.__spoolTest.explainFailure('httpx.ConnectError: Connection refused', null) !== null;
    out.failure_unknown = ctx.__spoolTest.explainFailure('工具 read_file 失败：文件不存在', null);
    out.redact = ctx.__spoolTest.redact('api_key=sk-abcdef123456 Bearer abcdefghijklmn token=zzz9');
    out.address_fallback = ctx.__spoolTest.explainFailure('Connection refused', null).lines[0];

    // 画出来之后长什么样：认不出来的是"原来的词 + 原文"，认出来的是块。
    const beforePlain = ids.get('stream').querySelectorAll('.failure').length;
    ctx.__spoolTest.showFailure('失败：', '工具 read_file 失败：文件不存在', null);
    out.failure_plain_blocks = ids.get('stream').querySelectorAll('.failure').length - beforePlain;
    const plain = ids.get('stream').querySelectorAll('.sys-line');
    out.failure_plain = plain[plain.length - 1].textContent;
    const before = ids.get('stream').querySelectorAll('.failure').length;
    ctx.__spoolTest.showFailure('失败：', 'LlamaCppError: 无法连接 llama-server', {
      kind: 'connect', url: 'http://127.0.0.1:9',
    });
    out.failure_blocks = ids.get('stream').querySelectorAll('.failure').length - before;

    // ②-2 探活：**常态只剩一枚小圆点**（不是画一句"一切正常"）；不通 / 回话慢 /
    // 还没探过 才变色并**带字**；点开是详情（状态 / 地址 / 探活时间 / 可能原因 / 下一步）。
    // 探活挪到后台之后 `/state` 给的是最近一次结果，所以详情里必须说得出"3 秒前探过"。
    const HEALTH_UP = {
      model: { ok: true, target: '', detail: '假模型' }, services: [],
      state: 'up', probed_at: 1700000000, age_s: 1,
    };
    const DOWN = {
      model: { ok: false, target: 'http://127.0.0.1:8080', detail: '拒绝连接', kind: 'connect' },
      services: [], state: 'down', probed_at: 1700000000, age_s: 3,
    };
    ctx.__spoolTest.closeAllToasts();   // 先清干净：要能说"探活**一条气泡都不产生**"
    ctx.__spoolTest.showHealth(HEALTH_UP);
    out.health_ok = {
      label: stLabel('st-health'),
      cls: stNode('st-health')._cls,
      parts: stNode('st-health').children.length,
      visible: visibleBadges(),
      title: stNode('st-health').title,
      keys: toastKeys(),
    };
    const beforeDown = snap();
    ctx.__spoolTest.showHealth(DOWN);
    out.health_down = {
      label: stLabel('st-health'),
      cls: stNode('st-health')._cls,
      visible: visibleBadges(),
      panel_open: panelOpen(),
      cost: since(beforeDown),
    };
    // 同一份内容再来一拍（3 秒轮询就是这么送的）：一个节点都不该新建。
    const beforeHealthRepeat = snap();
    ctx.__spoolTest.showHealth(DOWN);
    out.health_repeat = since(beforeHealthRepeat);
    // 点开那枚徽标：四件事都要在（地址 / 探活时间 / 可能原因 / 下一步敲什么）。
    const beforePanel = snap();
    const streamKidsBefore = ids.get('stream').children.length;
    const streamNodesBefore = allNodes(ids.get('stream')).length;
    ctx.__spoolTest.setStatusOpen(true);
    out.health_panel = {
      open: panelOpen(),
      text: panelText(),
      aria: stNode('st-health').getAttribute('aria-expanded'),
      stream_children: ids.get('stream').children.length,
      stream_children_before: streamKidsBefore,
      stream_nodes: allNodes(ids.get('stream')).length,
      stream_nodes_before: streamNodesBefore,
      panel_nodes: allNodes(ids.get('st-panel')).length,
      panel_root: (function () {
        const kids = ids.get('st-panel').children.filter(function (node) {
          return node._id !== 'st-close';
        });
        const kid = kids.length ? kids[0] : null;
        return kid ? kid._rootId() : '';
      })(),
      panel_first: (function () {
        const kid = ids.get('st-panel').firstChild;
        return kid ? (kid._id || '') : '';
      })(),
      cost: since(beforePanel),
    };
    ctx.__spoolTest.setStatusOpen(false);
    out.health_panel_closed = {open: panelOpen(), aria: stNode('st-health').getAttribute('aria-expanded')};
    // 模型又通了：圆点回到常态——一条通知都不该留（探活不再是气泡）。
    ctx.__spoolTest.showHealth(HEALTH_UP);
    out.health_back = {
      label: stLabel('st-health'), cls: stNode('st-health')._cls,
      keys: toastKeys(), visible: visibleBadges(),
    };
    // 还没探过：既不许画成"通了"，也不许画成"连不上"。
    ctx.__spoolTest.showHealth({
      model: { ok: null, target: '', detail: '还没探过（后台探活刚起来）', kind: '' },
      services: [], state: 'unknown', probed_at: null, age_s: null,
    });
    out.health_unknown = {label: stLabel('st-health'), cls: stNode('st-health')._cls};
    out.health_line_unknown = stLabel('st-health');
    ctx.__spoolTest.showHealth(HEALTH_UP);

    // ①-6 点「＋ 新建会话」：**先让服务端建出来、再切过去**，两个请求都要发对
    // （以前这里只发一次 /target，而服务端不新建会话——那个按钮点下去只有 400）。
    const posts = [];
    const SROWS_NEW = [{ id: 'web-new', title: '', archived: false, last_active: 1, current: true }];
    ctx.__fetchImpl = (url) => {
      posts.push(url);
      if (url === '/session/new') {
        return Promise.resolve({ ok: true, status: 200,
          json: async () => ({ ok: true, session: 'web-new', title: '' }) });
      }
      return Promise.resolve({ ok: true, status: 200,
        json: async () => frame(ROOT_B, 'web-new', WROWS, SROWS_NEW, history(0, '')) });
    };
    ids.get('new-session').onclick().then(() => {
      out.new_session = {
        calls: posts,
        selected: highlighted(),
        selected_sub: highlightedSub(),
      };
      ctx.__fetchImpl = null;
      return extras();
    }).then(() => {
      // 结果就是这一行 JSON（断言在 test_page_dom.py 里）：走 stdout，不用 console
      // ——它是这个测试台的**输出通道**，不是页面里的调试日志（页面里一行都没有）。
      process.stdout.write(JSON.stringify(out, null, 2) + '\n');
    });
  });
});

/* ── ① 通知 / ② 待确认卡片 / ③ 历史分隔：都放在最后，别打乱上面那几组计数 ── */

async function extras() {
  ctx.__spoolTest.closeAllToasts();

  // ①-7 一次性提示（`info`）**自己走**：页面排了一个定时器，到点那条就没了。
  const beforeInfo = snap();
  ctx.__spoolTest.notifyInfo('已恢复会话 a2', []);
  const infoScheduled = timers.length;
  out.info_toast = {
    keys: toastKeys(),
    shown: toastsShown(),
    close_buttons: closeButtons().length,
    timers: infoScheduled,
    auto_ms: infoScheduled ? timers[0].ms : 0,
    all_shown: allButtonShown(),
    stack_shown: toastsShown(),
    cost: since(beforeInfo),
  };
  const fired = fireTimers();
  out.info_toast.fired = fired;
  out.info_toast.after_auto = { keys: toastKeys(), shown: toastsShown() };

  // ①-8 错误：**不许自己消失**（一个定时器都不排），四件事都要在气泡里。
  const timersBeforeError = timers.length;
  ctx.__spoolTest.showFailure('失败：', 'LlamaCppError: 无法连接 llama-server（ConnectTimeout）', {
    kind: 'connect', url: 'http://127.0.0.1:9',
  });
  const errorToast = toastWith('failure|');
  out.error_toast = {
    keys: toastKeys(),
    text: toastBody(errorToast),
    kind: errorToast ? errorToast._cls : '',
    close_buttons: closeButtons().length,
    new_timers: timers.length - timersBeforeError,
    all_shown: allButtonShown(),
  };
  const keysBeforeFire = toastKeys();
  fireTimers();
  out.error_toast.survived_timers = toastKeys().length === keysBeforeFire.length;
  // 人点关闭键：这条才走。（点的是那一个按钮上的处理函数，不是"直接删节点"。）
  const errorClose = errorToast ? errorToast.querySelector('.toast-close') : null;
  if (errorClose) { errorClose.onclick(); }

  // ①-9 多条同时出现：同一个栈里各占一行（`#toasts` 那一列），两条以上才有「全部关闭」。
  // 气泡现在只管"刚刚发生了什么"（失败 / 做成了的回执）；"这一页什么状态"走状态区，
  // 所以这里直接摆两条失败通知。
  const failSpec = (key, title) => ({key: key, kind: 'error', title: title, lines: ['一行']});
  ctx.__spoolTest.showToast(failSpec('failure|甲', '失败甲'));
  ctx.__spoolTest.showToast(failSpec('failure|乙', '失败乙'));
  const stackNodes = toastNodes();
  out.stack = {
    keys: toastKeys(),
    parents: stackNodes.map((node) => (node.parentNode ? node.parentNode._id : '')),
    kind: stackNodes.length ? stackNodes[0]._cls : '',
    all_shown: allButtonShown(),
    shown: toastsShown(),
  };
  // 关掉一条：另一条留着；只剩一条时「全部关闭」收起来。
  // （`stackNodes` 可能是空的：那说明"通知挂在 `#toasts` 下"这条前提已经坏了——
  // 让上面那几条断言去红，这里不要在假 DOM 里抛异常，否则整组会变成 error 而不是 failed。）
  const first = stackNodes.length ? stackNodes[0].querySelector('.toast-close') : null;
  if (first) { first.onclick(); }
  out.stack.after_one = { keys: toastKeys(), all_shown: allButtonShown(), shown: toastsShown() };
  // 「全部关闭」：一条不剩，整块也收起来。
  ctx.__spoolTest.closeAllToasts();
  out.stack.after_all = { keys: toastKeys(), shown: toastsShown() };

  // ①-10 人关掉的那一份内容不再自己弹回来；内容真变了才回。
  ctx.__spoolTest.showToast(failSpec('failure|丙', '失败丙'));
  const dismissedKey = toastKeys()[0];
  ctx.__spoolTest.dismissToast(dismissedKey);
  out.dismiss = { after_close: toastKeys().length };
  ctx.__spoolTest.showToast(failSpec('failure|丙', '失败丙'));
  out.dismiss.same_again = toastKeys().length;
  ctx.__spoolTest.showToast(failSpec('failure|丙', '失败丙（换了内容）'));
  out.dismiss.changed = toastKeys().length;
  ctx.__spoolTest.closeAllToasts();
  for (const name of ['一', '二', '三', '四']) {
    ctx.__spoolTest.showToast(failSpec('failure|cap|' + name, name));
  }
  out.toast_cap = {
    count: toastNodes().length,
    keys: toastKeys(),
    newest: toastNodes().length ? toastNodes()[0].dataset.key : '',
    alert: toastNodes().length ? toastNodes()[0].getAttribute('role') : '',
  };
  ctx.__spoolTest.closeAllToasts();

  // ②-3 没有待确认内容：这块 UI **在页面上不存在**（没有卡片、没有那两个按钮）。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));
  out.card_absent = Object.assign({
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
  }, confirmButtonCounts());

  // ②-4 有待确认内容：卡片出现在**对话流里**，写着谁的、哪个文件、改了多少。
  const CARD_FRAME = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [
      { path: 'src/a.py', text: '@@ -1,2 +1,3 @@\n-old\n+new\n+more' },
      { path: 'src/b.py', text: '--- a\n+++ b\n+only' },
    ],
    awaiting: 2,
  });
  const beforeCard = snap();
  ctx.__spoolTest.applyState(CARD_FRAME);
  const card = ids.get('stream').querySelector('.ask-card');
  out.card_asking = Object.assign({
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    in_stream: inStream(card),
    state: card ? card.dataset.state : '',
    cls: card ? card._cls : '',
    who: card ? card.querySelector('.ask-who').textContent : '',
    kind: card ? card.querySelector('.ask-kind').textContent : '',
    summaries: diffHeads(),
    folds: diffFolds(),
    apply_label: card && card.querySelector('.ask-apply') ? card.querySelector('.ask-apply').textContent : '',
    reject_label: card && card.querySelector('.ask-reject') ? card.querySelector('.ask-reject').textContent : '',
    cost: since(beforeCard),
  }, confirmButtonCounts());
  // 同一帧再来一次：卡片不重建（轮询/补帧会一遍遍送同一份状态）。
  const beforeCardRepeat = snap();
  ctx.__spoolTest.applyState(CARD_FRAME);
  out.card_repeat = since(beforeCardRepeat);

  // ②-5 点「应用」：卡片变成一行状态，不再是等你点的卡片；按钮没了。
  const decidePosts = [];
  const decideBodies = [];
  ctx.__fetchImpl = (url, opts) => {
    decidePosts.push(url);
    decideBodies.push(opts && opts.body ? String(opts.body) : '');
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.__spoolTest.decide(true);
  ctx.__fetchImpl = null;
  const appliedRow = ids.get('stream').querySelector('.ask-row');
  out.card_applied = Object.assign({
    calls: decidePosts,
    body: decideBodies[0] || '',
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
    text: appliedRow ? appliedRow.textContent : '',
    kind: appliedRow ? appliedRow._cls : '',
  }, confirmButtonCounts());

  // ②-6 再来一份待确认、点「拒绝」：另起一张新卡片，不是把上面那行改回问句。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/c.py', text: '+one' }], awaiting: 1,
  }));
  const askingAgain = ids.get('stream').querySelectorAll('.ask-card').length;
  const denyPosts = [];
  const denyBodies = [];
  ctx.__fetchImpl = (url, opts) => {
    denyPosts.push(url);
    denyBodies.push(opts && opts.body ? String(opts.body) : '');
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.__spoolTest.decide(false);
  ctx.__fetchImpl = null;
  const rows = ids.get('stream').querySelectorAll('.ask-row');
  out.card_rejected = {
    calls: denyPosts,
    body: denyBodies[0] || '',
    new_card: askingAgain,
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: rows.length,
    last: rows.length ? rows[rows.length - 1].textContent : '',
  };

  // ②-7 被拒（服务端 409「当前没有待确认的改动」）：卡片退回"等你点"，别停在已应用上骗人。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/d.py', text: '+x' }], awaiting: 1,
  }));
  ctx.__fetchImpl = () => Promise.resolve({
    ok: false, status: 409, json: async () => ({ error: '当前没有待确认的改动' }),
  });
  await ctx.__spoolTest.decide(true);
  ctx.__fetchImpl = null;
  const backCard = ids.get('stream').querySelector('.ask-card');
  out.card_reverted = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    state: backCard ? backCard.dataset.state : '',
    apply_disabled: backCard && backCard.querySelector('.ask-apply')
      ? backCard.querySelector('.ask-apply').disabled : null,
  };

  // ②-8 只读壳（非回环）：那两条按钮**根本不出现**（既有契约）。
  // 先把上一张卡片收掉（只读是绑定地址决定的，真实使用里不会中途翻转），
  // 再看"只读时新起一张卡片"长什么样。
  ctx.__spoolTest.showReadonly({ readonly: true, host: '0.0.0.0' });
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [], awaiting: 0, readonly: true, host: '0.0.0.0',
  }));
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/e.py', text: '+y' }], awaiting: 1, readonly: true, host: '0.0.0.0',
  }));
  out.card_readonly = Object.assign({
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    state: (ids.get('stream').querySelector('.ask-card') || { dataset: {} }).dataset.state,
    actions: ids.get('stream').querySelectorAll('.ask-actions').length,
  }, confirmButtonCounts());
  // 只读是**绑定地址**决定的，一次会话里不会来回变；这里把状态收回去，
  // 免得后面那几条把"只读时建的那张卡片"当成正常情况。
  ctx.__spoolTest.showReadonly({ readonly: false, host: '127.0.0.1' });
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));

  // ②-9 命令授权也是同一张卡片（它没有 diff，但有"要执行什么"）。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [], awaiting: 1,
    awaiting_detail: { count: 1, command: 'whoami', reason: '白名单外' },
  }));
  const askCard = ids.get('stream').querySelector('.ask-card');
  const askCode = askCard ? askCard.querySelector('code') : null;
  out.card_command = Object.assign({
    cls: askCard ? askCard._cls : '',
    kind: askCard ? askCard.querySelector('.ask-kind').textContent : '',
    title: askCard && askCard.querySelector('.ask-title') ? askCard.querySelector('.ask-title').textContent : '',
    pre: askCard && askCard.querySelector('pre') ? askCard.querySelector('pre').tagName : '',
    code: askCode ? askCode.textContent : '',
    reason: askCard && askCard.querySelector('.ask-reason') ? askCard.querySelector('.ask-reason').textContent : '',
    warn: askCard && askCard.querySelector('.ask-warn') ? askCard.querySelector('.ask-warn').textContent : '',
    body: askCard ? askCard.querySelector('.ask-body').textContent : '',
    card_text: askCard ? askCard.textContent : '',
    allow: buttonsWith('允许执行').length,
    deny: buttonsWith('拒绝').length,
    file_apply: buttonsWith('应用全部改动').length,
    file_reject: buttonsWith('拒绝全部').length,
  }, confirmButtonCounts());

  const cmdBodies = [];
  ctx.__fetchImpl = (url, opts) => {
    cmdBodies.push(opts && opts.body ? String(opts.body) : '');
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.__spoolTest.decide(true);
  const allowedRows = ids.get('stream').querySelectorAll('.ask-row');
  out.card_command_allowed = {
    body: cmdBodies[0] || '',
    text: allowedRows.length ? allowedRows[allowedRows.length - 1].textContent : '',
  };
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [], awaiting: 1,
    awaiting_detail: { count: 1, command: 'echo denied', reason: '再问一次' },
  }));
  cmdBodies.length = 0;
  await ctx.__spoolTest.decide(false);
  ctx.__fetchImpl = null;
  const deniedRows = ids.get('stream').querySelectorAll('.ask-row');
  out.card_command_denied = {
    body: cmdBodies[0] || '',
    text: deniedRows.length ? deniedRows[deniedRows.length - 1].textContent : '',
  };

  // ②-10 服务端说没有待确认了（`awaiting` 没了 / 是 0）：卡片**从页面上消失**。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));
  out.card_gone = Object.assign({
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
  }, confirmButtonCounts());
  // 缺字段（老服务端 / 空快照）也按"没有待确认内容"办。
  const bare = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''));
  delete bare.awaiting;
  delete bare.diffs;
  ctx.__spoolTest.applyState(bare);
  out.card_missing_field = Object.assign({
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
  }, confirmButtonCounts());

  // ②-11 大 diff 默认收起、小 diff 默认展开；点文件头切换，同一帧再来不把折叠状态冲掉。
  const bigLines = [];
  for (let lineNo = 0; lineNo < 200; lineNo += 1) { bigLines.push('+' + lineNo); }
  const FOLD_FRAME = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [
      { path: 'big.py', text: bigLines.join('\n') },
      { path: 'small.py', text: '+one\n-two' },
    ],
    awaiting: 2,
  });
  ctx.__spoolTest.applyState(FOLD_FRAME);
  const foldFiles = ids.get('stream').querySelectorAll('.diff-file');
  const bigHead = foldFiles[0] ? foldFiles[0].querySelector('.diff-file-head') : null;
  const smallHead = foldFiles[1] ? foldFiles[1].querySelector('.diff-file-head') : null;
  const bigSheet = foldFiles[0] ? foldFiles[0].querySelector('.diff') : null;
  const bigBefore = bigHead ? bigHead.getAttribute('aria-expanded') : '';
  if (bigHead && bigHead.onclick) { bigHead.onclick(); }
  const bigAfter = bigHead ? bigHead.getAttribute('aria-expanded') : '';
  const smallBefore = smallHead ? smallHead.getAttribute('aria-expanded') : '';
  if (smallHead && smallHead.onclick) { smallHead.onclick(); }
  const smallAfter = smallHead ? smallHead.getAttribute('aria-expanded') : '';
  const beforeFoldRepeat = snap();
  ctx.__spoolTest.applyState(FOLD_FRAME);
  out.diff_fold = {
    files: foldFiles.length,
    big_lines: bigSheet ? bigSheet.children.length : 0,
    big_before: bigBefore,
    big_after: bigAfter,
    big_open_after: foldFiles[0] ? foldFiles[0].classList.contains('open') : false,
    big_body: foldFiles[0] && foldFiles[0].querySelector('.diff-file-body')
      ? foldFiles[0].querySelector('.diff-file-body').className : '',
    small_before: smallBefore,
    small_after: smallAfter,
    small_open_after: foldFiles[1] ? foldFiles[1].classList.contains('open') : true,
    repeat: since(beforeFoldRepeat),
    still: bigHead ? bigHead.getAttribute('aria-expanded') : '',
  };

  // ②-12 同一帧里既有 diff 又有命令：一张组合卡，两边都在，点一次只发一次 /confirm。
  const BOTH_FRAME = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [
      { path: 'src/a.py', text: '@@ -1,2 +1,3 @@\n-old\n+new\n+more' },
      { path: 'src/b.py', text: '+only' },
    ],
    awaiting: 1,
    awaiting_detail: { count: 1, command: 'git status', reason: '要看工作区' },
  });
  ctx.__spoolTest.applyState(BOTH_FRAME);
  const bothCard = ids.get('stream').querySelector('.ask-card');
  const bothHead = bothCard ? bothCard.querySelector('.diff-file-head') : null;
  const bothBefore = bothHead ? bothHead.getAttribute('aria-expanded') : '';
  if (bothHead && bothHead.onclick) { bothHead.onclick(); }
  const bothFlipped = bothHead ? bothHead.getAttribute('aria-expanded') : '';
  const beforeBothRepeat = snap();
  ctx.__spoolTest.applyState(BOTH_FRAME);
  const bothAsking = {
    cls: bothCard ? bothCard._cls : '',
    kind: bothCard && bothCard.querySelector('.ask-kind') ? bothCard.querySelector('.ask-kind').textContent : '',
    files: bothCard ? bothCard.querySelectorAll('.diff-file').length : 0,
    cmds: bothCard ? bothCard.querySelectorAll('.ask-cmd').length : 0,
    code: bothCard && bothCard.querySelector('code') ? bothCard.querySelector('code').textContent : '',
    reason: bothCard && bothCard.querySelector('.ask-reason') ? bothCard.querySelector('.ask-reason').textContent : '',
    warn: bothCard && bothCard.querySelector('.ask-warn') ? bothCard.querySelector('.ask-warn').textContent : '',
    apply_label: bothCard && bothCard.querySelector('.ask-apply') ? bothCard.querySelector('.ask-apply').textContent : '',
    reject_label: bothCard && bothCard.querySelector('.ask-reject') ? bothCard.querySelector('.ask-reject').textContent : '',
    file_apply: buttonsWith('应用全部改动').length,
    allow: buttonsWith('允许执行').length,
    expanded_before: bothBefore,
    expanded_flipped: bothFlipped,
    repeat: since(beforeBothRepeat),
    still: bothHead ? bothHead.getAttribute('aria-expanded') : '',
  };
  const bothPosts = [];
  ctx.__fetchImpl = (url, opts) => {
    bothPosts.push({url: url, body: opts && opts.body ? String(opts.body) : ''});
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.__spoolTest.decide(true);
  const bothRows = ids.get('stream').querySelectorAll('.ask-row');
  out.card_both = Object.assign(bothAsking, {
    posts: bothPosts.slice(),
    text: bothRows.length ? bothRows[bothRows.length - 1].textContent : '',
  });
  ctx.__spoolTest.applyState(BOTH_FRAME);
  bothPosts.length = 0;
  await ctx.__spoolTest.decide(false);
  ctx.__fetchImpl = null;
  const bothDenied = ids.get('stream').querySelectorAll('.ask-row');
  out.card_both_denied = {
    posts: bothPosts.slice(),
    text: bothDenied.length ? bothDenied[bothDenied.length - 1].textContent : '',
  };

  // ③-1 历史：分隔线还在（一条线），但那句"给人看的、不进模型上下文"没了，
  // 历史消息本身照旧画在对话流里（略淡那一档）。
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(4, 'h')));
  out.history = {
    seps: ids.get('stream').querySelectorAll('.history-sep').length,
    sep_text: (ids.get('stream').querySelector('.history-sep') || { textContent: '' }).textContent,
    bubbles: streamBubbles(),
    marked: ids.get('stream').querySelectorAll('.history').length,
    texts: ids.get('stream').querySelectorAll('.bubble').map((node) => node.textContent),
  };

  // ①-11 通知**不许把页面挤下去**：弹一条前后，对话流的子节点数与主滚动容器的
  // 滚动位置、以及输入条那一块的直接子节点数，都得一模一样；而且气泡本身
  // **不在对话流里**（它挂在 `#toasts` 下，那是 `body` 的直接子节点）。
  // 假 DOM 不排版，所以能量的是"有没有往流里塞节点、有没有动 scrollTop /
  // scrollHeight"——真浏览器里"被挤下去"正是这两件事的结果。
  const streamBox = ids.get('stream');
  streamBox.scrollHeight = 800;
  streamBox.clientHeight = 200;
  streamBox.scrollTop = 120;
  const readStream = () => ({
    stream_children: streamBox.children.length,
    stream_nodes: allNodes(streamBox).length,
    scrollTop: streamBox.scrollTop,
    scrollHeight: streamBox.scrollHeight,
  });
  const beforePush = readStream();
  const pushed = ctx.__spoolTest.showToast({key: 'failure|挤', kind: 'error', title: '失败：挤一下', lines: ['一行']});
  out.toast_no_push = {
    before: beforePush,
    after: readStream(),
    toast_root: pushed ? pushed._rootId() : 'missing',
    container_root: ids.get('toasts')._rootId(),
    in_stream: pushed ? inStream(pushed) : 'missing',
    parent_id: pushed && pushed.parentNode ? pushed.parentNode._id : '',
  };
  ctx.__spoolTest.closeAllToasts();
  out.toast_no_push.after_close = readStream();

  // ②-11 状态区：常态下**只看得见一枚圆点**；只读 / 钉住 / 待配对 / 别处在跑
  // 才各多出一枚徽标。徽标只是同一行里的东西，所以展开详情也不动对话流与输入条结构。
  ctx.__spoolTest.showReadonly({ readonly: true, host: '0.0.0.0' });
  out.status_readonly = {
    visible: visibleBadges(), mode: stLabel('st-mode'),
    title: stNode('st-mode').title,
  };
  const beforeStatusPanel = snap();
  ctx.__spoolTest.setStatusOpen(true);
  out.status_readonly_panel = { open: panelOpen(), text: panelText(), cost: since(beforeStatusPanel) };
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.showReadonly({ readonly: false, host: '127.0.0.1' });
  ctx.__spoolTest.showPairings([{ code: '482913', user: 'u-1' }]);
  out.status_pair = { visible: visibleBadges(), pair: stLabel('st-pair') };
  ctx.__spoolTest.setStatusOpen(true);
  out.status_pair_panel = { text: panelText() };
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.showPairings([]);
  ctx.__spoolTest.showElsewhere({ running_targets: [
    { root: 'D:/ws-c', session: 'c1', name: 'ws-c', goal: '另一条在跑', current: false, awaiting: true },
  ] });
  out.status_elsewhere = { visible: visibleBadges(), label: stLabel('st-elsewhere') };
  ctx.__spoolTest.setStatusOpen(true);
  out.status_elsewhere_panel = { text: panelText() };
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.showElsewhere({ running_targets: [] });
  out.status_clean = { visible: visibleBadges(), label: stLabel('st-health'), panel: panelOpen() };

  // `--root` 钉住：模式徽标说"已钉住"，详情里那段解释还在（回答"为什么点不动"）。
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(4, 'h'), {
    target: { root: ROOT_A, session: 'a1', pinned: true, workspace_source: 'explicit',
      session_source: 'explicit', note: '' },
  }));
  out.status_pinned = { visible: visibleBadges(), mode: stLabel('st-mode') };
  ctx.__spoolTest.setStatusOpen(true);
  out.status_pinned_panel = { text: panelText() };
  ctx.__spoolTest.setStatusOpen(false);
  // 空态（还没有目标）：那一枚"未选工作区"徽标 + 怎么选的说明。
  ctx.__spoolTest.applyState(frame('', 'a1', WROWS, SROWS_A, history(0, ''), {
    target: { root: '', session: 'a1', pinned: false, workspace_source: 'none',
      session_source: 'default', note: '未选择工作区：在左侧「工作区」段选一项。' },
  }));
  out.status_no_target = { visible: visibleBadges(), label: stLabel('st-target') };
  ctx.__spoolTest.setStatusOpen(true);
  out.status_no_target_panel = { text: panelText() };
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(4, 'h')));

  // ②-12 收起详情有三条路：Esc（焦点不在输入框里也算）、点 `#composer` 之外、
  // 再点一次徽标。前两条挂在 `document` 上——假 DOM 里 `document` 是普通对象，
  // 直接把它那两个处理函数叫一遍就行（这正是"处理函数写了却没绑上"的证伪点）。
  // 没绑上时**不许**让假 DOM 抛异常（那会把整组变成 error）：照常放行，
  // 让下面那几条断言去红。
  const pressEsc = () => {
    if (typeof ctx.document.onkeydown === 'function') {
      ctx.document.onkeydown({ key: 'Escape' });
    }
  };
  const clickAt = (inside) => {
    if (typeof ctx.document.onclick === 'function') {
      ctx.document.onclick({ target: { closest: () => (inside ? {} : null) } });
    }
  };
  ctx.__spoolTest.setStatusOpen(true);
  pressEsc();
  out.status_esc = { open: panelOpen(), bound: typeof ctx.document.onkeydown === 'function' };
  ctx.__spoolTest.setStatusOpen(true);
  clickAt(false);
  out.status_outside = { open: panelOpen(), bound: typeof ctx.document.onclick === 'function' };
  ctx.__spoolTest.setStatusOpen(true);
  clickAt(true);
  out.status_inside = { open: panelOpen() };
  ctx.__spoolTest.setStatusOpen(false);

  // ③-2 自己说的话不加符号前缀：发一条之后，气泡里就是原话（一个字符都不多）。
  const goalInput = ids.get('goal');
  goalInput.value = '改一下登录页';
  ctx.__fetchImpl = () => Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  await ctx.__spoolTest.say();
  ctx.__fetchImpl = null;
  const bubbles = ids.get('stream').querySelectorAll('.bubble');
  const mine = bubbles.filter((node) => String(node.parentNode && node.parentNode._cls || '')
    .indexOf('user') >= 0);
  out.say_text = {
    calls: ctx.fetchCalls.slice(-1),
    last_user: mine.length ? mine[mine.length - 1].textContent : 'missing',
  };

  // ④-1 已归档会话默认不在左栏；只在页脚「设置」里主动找。
  const REMOVED = [
    { id: 'old-1', title: '归档的登录页修复' },
    { id: 'old-2', title: '' },
  ];
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(0, ''), {
    sessions_removed: REMOVED,
  }));
  out.archive_default = {
    sidebar_text: ids.get('sessions').textContent,
    count: ids.get('archived-count').textContent,
    hidden: ids.get('archived-count').classList.contains('hidden'),
    settings_hidden: ids.get('settings').classList.contains('hidden'),
  };
  ids.get('settings-btn').onclick();
  out.archive_open = {
    open: !ids.get('settings').classList.contains('hidden'),
    aria: ids.get('settings').getAttribute('aria-hidden'),
    list: ids.get('archived-list').textContent,
    rows: ids.get('archived-list').querySelectorAll('.archive-row').length,
    restore: ids.get('archived-list').querySelectorAll('.arc-restore').length,
    restore_disabled: ids.get('archived-list').querySelectorAll('.arc-restore')
      .map((node) => node.disabled),
  };
  if (typeof ctx.document.onkeydown === 'function') {
    ctx.document.onkeydown({ key: 'Escape' });
  }
  out.archive_esc = { closed: ids.get('settings').classList.contains('hidden') };

  // ④-2 当前会话不在会话表里也要补进左栏，标题优先、id 作为次级信息。
  ctx.__spoolTest.applyState(frame(ROOT_A, 'cli', WROWS, SROWS_A, history(0, ''), {
    target: { root: ROOT_A, session: 'cli', session_title: '修复缓存穿透',
      session_title_source: 'agent', pinned: false,
      workspace_source: 'explicit', session_source: 'explicit', note: '' },
  }));
  const currentRow = rowByName('sessions', '修复缓存穿透');
  out.current_session_row = {
    found: Boolean(currentRow),
    highlighted: currentRow ? currentRow.classList.contains('on') : false,
    subtitle: currentRow ? currentRow.querySelector('.src').textContent : '',
  };
  const titled = rowByName('sessions', 'A 的第 1 条');
  out.session_title_sub = titled ? titled.querySelector('.src').textContent : '';

  // ④-3 种子标题：final 之后请 Agent 起名，返回后左栏立刻换成真标题。
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, [
    { id: 'a1', title: '种子标题', title_source: 'seed', archived: false,
      last_active: 1, current: true },
  ], history(0, ''), {
    target: { root: ROOT_A, session: 'a1', session_title: '种子标题',
      session_title_source: 'seed', pinned: false,
      workspace_source: 'explicit', session_source: 'explicit', note: '' },
  }));
  ctx.__fetchImpl = (url) => {
    return Promise.resolve({ ok: true, status: 200, json: async () =>
      ({ ok: true, title: '模型起的标题', generated: true, source: 'agent' }) });
  };
  await ctx.__spoolTest.maybeAgentTitle(true);
  ctx.__fetchImpl = null;
  const namedRow = rowByName('sessions', '模型起的标题');
  out.agent_title = {
    title: namedRow ? namedRow.querySelector('.nm').textContent : '',
    sub: namedRow ? namedRow.querySelector('.src').textContent : '',
    fetched: ctx.fetchCalls.slice(-1),
  };

  await extras2();
}

/* ── ① 常驻提示挪到右上角之后**不许挤对话区** / ② 回复卡片 / ⑤ 停止 ──
   单独一段、放在最后：上面那些计数是既有用例的判据，别让这一段的节点搅进去。 */

function lastCard() {
  const wraps = ids.get('stream').querySelectorAll('.msg');
  return wraps.length ? wraps[wraps.length - 1] : null;
}

// 一张助手卡片当前长什么样：气泡里有几个、是不是还挂着占位、轨迹那一块收着没有。
function cardInfo(card) {
  const trace = card ? card.querySelector('.trace') : null;
  const head = trace ? trace.querySelector('.trace-head') : null;
  const body = trace ? trace.querySelector('.trace-body') : null;
  const bubble = card ? card.querySelector('.bubble') : null;
  return {
    bubbles: card ? card.querySelectorAll('.bubble').length : 0,
    bubble_text: bubble ? bubble.textContent : '',
    pending: bubble ? String(bubble._cls).indexOf('pending') >= 0 : false,
    trace_head: head ? head.textContent : '',
    trace_aria: head ? head.getAttribute('aria-expanded') : '',
    trace_nodes: body ? allNodes(body).length : 0,
    trace_text: body ? body.textContent : '',
    card_text: card ? card.textContent : '',
  };
}

function bubbleTexts() {
  return ids.get('stream').querySelectorAll('.bubble').map((node) => node.textContent);
}

function streamText() { return ids.get('stream').textContent; }

function event(type, payload) {
  return { data: JSON.stringify(Object.assign({type: type}, payload || {})) };
}

async function extras2() {
  // ══ ① 常驻提示：从"一枚圆点都没有"到"五枚徽标全亮 + 详情展开"，对话区不许动 ══
  // 假 DOM 不排版，所以这里用一条**粗模型**把"挤"量化，而且模型自己先被反证过：
  //   对话区可用高度 = #thread 的高 - #composer 的高
  //   #composer 的高 = 72 + （它里面在文档流里的节点数 × 24）
  // 那 24px 是"一行小字"的量级——徽标换行、面板展开都会让那一行多一行。
  // 判据：常驻提示亮起 / 详情展开 / 全部收起，这个数**一个像素都不许动**；
  // 而 `hints_nodes` 同时在变（说明提示真的出现了，不是"什么都没发生"），
  // 再把徽标搬回输入条那一行——可用高度立刻掉 120px（模型量得出"挤"）。
  const threadBox = ids.get('thread');
  const composerBox = ids.get('composer');
  const streamBox = ids.get('stream');
  threadBox.clientHeight = 600;
  streamBox.clientHeight = 520;
  streamBox.scrollHeight = 900;
  streamBox.scrollTop = 220;
  const room = () => {
    // 节点数只算**元素**（文本节点不额外占行）：一行文字本身不会让那一行变高。
    const nodes = allNodes(composerBox).filter((node) => node.tagName !== 'TEXT').length;
    const composerH = 72 + nodes * 24;
    return {
      composer_nodes: nodes,
      composer_h: composerH,
      thread_h: threadBox.clientHeight,
      room: threadBox.clientHeight - composerH,
      stream_children: streamBox.children.length,
      stream_nodes: allNodes(streamBox).length,
      scrollTop: streamBox.scrollTop,
      scrollHeight: streamBox.scrollHeight,
    };
  };
  const UP = { model: { ok: true, target: '', detail: '假模型' }, services: [],
    state: 'up', probed_at: 1700000000, age_s: 1 };
  const DOWN = { model: { ok: false, target: 'http://127.0.0.1:8080',
    detail: '拒绝连接', kind: 'connect' }, services: [], state: 'down',
    probed_at: 1700000000, age_s: 3 };
  // **同一个目标**（ROOT_A）、同样空的历史，只改"常驻提示那几样"：
  // 安静 → 四枚徽标全亮 → 详情展开 → 又回到安静。目标一变就会整屏重绑，
  // 那会把对话流的节点数搅进来，而这一条要量的正是"提示自己占不占地方"。
  const hintFrame = (extra) => frame(ROOT_A, 'a1', WROWS, SROWS_A, [],
    Object.assign({ readonly: false, host: '127.0.0.1', pairings: [],
      running_targets: [], health: UP }, extra));
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.applyState(hintFrame({}));
  const quiet = room();
  ctx.__spoolTest.applyState(hintFrame({
    readonly: true,
    host: '0.0.0.0',
    pairings: [{ code: '482913', user: 'u-1' }],
    running_targets: [
      { root: 'D:/ws-c', session: 'c1', name: 'ws-c', goal: '另一条在跑',
        current: false, awaiting: true },
    ],
    health: DOWN,
  }));
  const busy = room();
  const busyBadges = visibleBadges();
  ctx.__spoolTest.setStatusOpen(true);
  const opened = room();
  const panelNodes = allNodes(ids.get('st-panel')).length;
  ctx.__spoolTest.setStatusOpen(false);
  ctx.__spoolTest.applyState(hintFrame({}));
  const clean = room();
  const cleanBadges = visibleBadges();
  out.no_squeeze = {
    quiet: quiet, busy: busy, opened: opened, clean: clean,
    panel_nodes: panelNodes,
    busy_badges: busyBadges,
    clean_badges: cleanBadges,
  };
  // 反证：把五枚徽标搬回**输入条那一行**（也就是这一轮改掉的那个设计），
  // 可用高度立刻掉 5 × 24 = 120px，滚动位置与对话流节点数倒是没动——
  // 说明"挤"正是从那一行的高度来的，而模型量得出它。
  const moved = [];
  for (const id of ['st-health', 'st-mode', 'st-target', 'st-pair', 'st-elsewhere']) {
    const node = ids.get(id);
    if (node.parentNode) { node.parentNode.removeChild(node); }
    composerBox.appendChild(node);
    moved.push(id);
  }
  out.no_squeeze.moved_back = room();
  for (const id of moved) {
    const node = ids.get(id);
    if (node.parentNode) { node.parentNode.removeChild(node); }
    ids.get('hints').appendChild(node);
  }
  out.no_squeeze.restored = room();

  // ══ ② 回复卡片：占位 → 真正的回复；思考过程收起可展开、库里那条不重复画 ══
  // `resetView` 就是"刷新了一次"在假 DOM 里的样子：对话流清空、这一轮的实时状态作废。
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, '')));
  const goalInput2 = ids.get('goal');
  goalInput2.value = '改一下登录页';
  ctx.__fetchImpl = () => Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  await ctx.__spoolTest.say();
  ctx.__fetchImpl = null;
  out.reply_user_bubbles = streamBubbles();
  ctx.__spoolTest.onEvent(event('start', { goal: '改一下登录页' }));
  out.reply_placeholder = cardInfo(lastCard());
  // 边跑边攒：步骤 / 失败工具 / 等待心跳进「思考过程」——运行中自动展开。
  ctx.__spoolTest.onEvent(event('step', { n: 1 }));
  ctx.__spoolTest.onEvent(event('tool', { name: 'read_file', ok: false, detail: '文件不存在：src/a.py' }));
  ctx.__spoolTest.onEvent(event('wait', { elapsed: 12, limit: 491 }));
  // 同 phase 再一条 wait：应替换末行，不堆第二行心跳。
  ctx.__spoolTest.onEvent(event('wait', { elapsed: 27, limit: 491 }));
  out.reply_live = cardInfo(lastCard());
  out.reply_live_bubbles = streamBubbles();
  out.reply_open = cardInfo(lastCard());
  // phase 变了（输出加长）：追加新 wait，保留上一阶段末行。
  ctx.__spoolTest.onEvent(event('wait', { elapsed: 15, limit: 491, phase: '主循环·输出加长' }));
  out.reply_wait_phases = cardInfo(lastCard());
  ctx.__spoolTest.onEvent(event('final', { ok: true, text: '改好了：加了校验' }));
  out.reply_done = cardInfo(lastCard());
  out.reply_done_bubbles = streamBubbles();
  // 库里那条回复到了（快照）：**认领**同一张卡片——不新建节点、也不重复一遍。
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, [
    { role: 'user', content: '改一下登录页', meta: '', created_at: 10 },
    { role: 'assistant', content: '改好了：加了校验', meta: '1 步，完成', created_at: 11,
      trace: [
        { type: 'step', text: '第 1 步' },
        { type: 'tool', ok: false, text: 'read_file → 失败', detail: '文件不存在：src/a.py' },
        { type: 'final', ok: true, text: '改好了：加了校验' },
      ] },
  ]));
  out.reply_persisted = {
    ...cardInfo(lastCard()),
    stream_bubbles: streamBubbles(),
    texts: bubbleTexts(),
    has_placeholder: streamText().indexOf('生成中') >= 0,
    bubble_placeholder: bubbleTexts().indexOf('生成中') >= 0,
  };

  // 刷新页面 / 切走再切回之后：只喂一帧 `/state`（历史里那条消息带着 trace），
  // **思考过程必须重新画得出来**——这是"排错"那条硬要求。
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, [
    { role: 'user', content: '看一下 src/a.py', meta: '', created_at: 20 },
    { role: 'assistant', content: '没做成：文件不存在', meta: '1 步，未完成', created_at: 21,
      trace: [
        { type: 'step', text: '第 1 步' },
        { type: 'tool', ok: false, text: 'read_file → 失败', detail: '文件不存在：src/a.py' },
        { type: 'final', ok: false, text: '没做成：文件不存在',
          error: { stage: 'model', kind: 'connect', url: 'http://127.0.0.1:9' } },
      ] },
  ]));
  out.refresh_trace = {
    ...cardInfo(lastCard()),
    stream_bubbles: streamBubbles(),
    has_placeholder: streamText().indexOf('生成中') >= 0,
    bubble_placeholder: bubbleTexts().indexOf('生成中') >= 0,
  };
  // 刷新之后一键展开（展开键没建出来时不抛异常，让断言去红）。
  const freshHead = lastCard() ? lastCard().querySelector('.trace-head') : null;
  if (freshHead) { freshHead.onclick(); }
  out.refresh_trace_open = cardInfo(lastCard());

  // ══ ⑤ 运行中那一枚键是「停止」：点了立刻有反馈，`/state` 说停下来了就回到发送 ══
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(0, ''), { running: true }));
  const sendNode = ids.get('send');
  out.stop_button = {
    mode: sendNode.dataset.mode, disabled: sendNode.disabled, title: sendNode.title,
  };
  const stopPosts = [];
  ctx.__fetchImpl = (url) => {
    stopPosts.push(url);
    return Promise.resolve({ ok: true, status: 200,
      json: async () => ({ ok: true, stopped: true }) });
  };
  await sendNode.onclick();
  ctx.__fetchImpl = null;
  out.stop_clicked = {
    calls: stopPosts,
    mode: sendNode.dataset.mode, disabled: sendNode.disabled, title: sendNode.title,
    last_line: (streamText().split('\n').pop() || '').indexOf('已请求停止') >= 0,
  };
  ctx.__nextBody = frame(ROOT_A, 'a1', WROWS, SROWS_A, history(0, ''),
    { running: false, stopped: true });
  await ctx.__spoolTest.refreshStatus();
  out.stop_settled = {
    mode: sendNode.dataset.mode, disabled: sendNode.disabled, title: sendNode.title,
  };

  // ══ ⑥ 收尾报告的**分档**：结论段干净、详情与用量进「思考过程」 ══
  // 现场：`tests/fixtures/interact_probe/web-real-reports.md`（用户在网页壳里真实
  // 看到的第二次报告——结论段被内部账与用量糊住，过程区里还混进了一句系统对
  // 模型说的话）。这一段的判据全在渲染档位上：气泡里只有结论段，`详情：` 那几行
  // 与用量都在轨迹里，第三档说「跳过」而不是「成功」。
  const REPORT = [
    '已创建文件 hello.txt，内容为 Hello Word。',
    '没有校验器（不是失败）。',
    '详情：本次运行共落成 1 处改动。最终文件状态：hello.txt：1 行，指纹 44d3a83db52a/1行。',
    '撤销这一轮：spool revert --run 20261006-022747-299732 （网页里可直接点，桥里回 y）',
  ].join('\n');
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, '')));
  ctx.__spoolTest.onEvent(event('start', { goal: '创建一个文本文件' }));
  ctx.__spoolTest.onEvent(event('step', { n: 1 }));
  ctx.__spoolTest.onEvent(event('tool', { name: 'write_file', ok: true }));
  // 第三档（没有校验器）：`ok=true` 只说「没有发现问题」。渲染成「成功」就是
  // 一个不成立的通过（问题⑤），而状态字与结论段那句一样则是"同一句说两遍"。
  ctx.__spoolTest.onEvent(event('tool', { name: '最终验证', ok: true, no_checker: true,
    detail: '这次改的是 .txt。' }));
  ctx.__spoolTest.onEvent(event('final', { ok: true, text: REPORT }));
  // 用量**在结局之后**到（真实那次就是这个次序）：它必须挂回刚画好的那张卡片，
  // 不许另起一张只有一行用量的"幽灵卡片"。
  const cardsBeforeUsage = ids.get('stream').querySelectorAll('.msg').length;
  ctx.__spoolTest.onEvent(event('usage', { steps: 3, calls: 3, prompt_tokens: 13025,
    completion_tokens: 207 }));
  out.report_tiers = {
    ...cardInfo(lastCard()),
    cards: ids.get('stream').querySelectorAll('.msg').length,
    cards_before_usage: cardsBeforeUsage,
    stream_text: streamText(),
  };
  // 展开「思考过程」：详情那几行与用量都在里面（收起时正文块一个节点都不建，
  // 所以必须点开才读得到——那本身也是"默认收起"的判据）。
  const tiersHead = lastCard() ? lastCard().querySelector('.trace-head') : null;
  if (tiersHead) { tiersHead.onclick(); }
  out.report_tiers_open = cardInfo(lastCard());

  // 刷新 / 切走再切回：库里存的仍是**完整那份报告**，分档必须一样。
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, [
    { role: 'user', content: '创建一个文本文件', meta: '', created_at: 40 },
    { role: 'assistant', content: REPORT, meta: '3 步，完成', created_at: 41,
      trace: [{ type: 'step', text: '第 1 步' }] },
  ]));
  const historyHead = lastCard() ? lastCard().querySelector('.trace-head') : null;
  if (historyHead) { historyHead.onclick(); }
  out.report_tiers_history = cardInfo(lastCard());

  const goal = ids.get('goal');
  goal.value = '只走ctx';
  ctx.__spoolTest.store.set('readonly', false);
  ctx.__spoolTest.store.set('busy', false);
  let viaCtx = 0;
  let viaGlobal = 0;
  const realAdd = ctx.__spoolTest.app.thread.add;
  ctx.__spoolTest.app.thread.add = function () { viaCtx += 1; };
  ctx.add = function () { viaGlobal += 1; };
  const prevFetch = ctx.__fetchImpl;
  ctx.__fetchImpl = function () {
    return Promise.resolve({ ok: true, status: 200, json: async function () { return {}; } });
  };
  await ctx.__spoolTest.app.composer.say();
  ctx.__fetchImpl = prevFetch;
  ctx.__spoolTest.app.thread.add = realAdd;
  goal.value = '';
  out.composer_uses_ctx = viaCtx === 1 && viaGlobal === 0;
  ctx.__spoolTest.app.status.setStatusOpen(true);
  goal.onkeydown({
    key: 'Escape', preventDefault: function () {},
    shiftKey: false, altKey: false, ctrlKey: false, metaKey: false,
  });
  out.escape_closes_status = ctx.__spoolTest.store.get('statusOpen') === false;

  const keyRow = rowByName('workspaces', 'ws-b') || rowByName('workspaces', 'ws-a');
  const keyPosts = [];
  ctx.__fetchImpl = (url) => {
    keyPosts.push(url);
    return new Promise(() => {});
  };
  out.row_key = keyRow ? {
    role: keyRow.getAttribute('role'),
    tab: keyRow.tabIndex,
    current: keyRow.getAttribute('aria-current'),
  } : null;
  if (keyRow && keyRow.onkeydown) {
    keyRow.onkeydown({ key: ' ', preventDefault() {} });
  }
  out.row_enter = {
    posts: keyPosts.slice(),
    chosen: ctx.__spoolTest.store.get('chosen'),
  };
  ctx.__fetchImpl = null;
  ctx.__spoolTest.app.settings.setSettingsOpen(false);
  ctx.__spoolTest.app.sidebar.openDrawer();
  out.drawer_open = {
    drawer: document.body.classList.contains('drawer'),
    expanded: ids.get('menu').getAttribute('aria-expanded'),
    focus: document.activeElement ? document.activeElement._id : '',
  };
  document.onkeydown({ key: 'Escape' });
  out.drawer_esc = {
    drawer: document.body.classList.contains('drawer'),
    expanded: ids.get('menu').getAttribute('aria-expanded'),
    focus: document.activeElement ? document.activeElement._id : '',
  };
  out.no_hidden_select = !ids.get('ws') && !ids.get('sess');
  out.panel_close = ids.get('st-close') ? {
    label: ids.get('st-close').getAttribute('aria-label'),
    click: typeof ids.get('st-close').onclick === 'function',
  } : null;
  out.live = {
    conn: ids.get('conn') ? ids.get('conn').getAttribute('aria-live') : '',
    conn_role: ids.get('conn') ? ids.get('conn').getAttribute('role') : '',
    thinking: ids.get('thinking') ? ids.get('thinking').getAttribute('role') : '',
    toasts: ids.get('toasts') ? ids.get('toasts').getAttribute('aria-live') : '',
    goal: ids.get('goal') ? ids.get('goal').getAttribute('aria-labelledby') : '',
    stream: ids.get('stream') ? ids.get('stream').getAttribute('role') : '',
  };

  // 草稿按「工作区|会话」分开。输入事件才落盘；同一目标的后续帧不许用空串盖住正在打的字。
  const goalBox = ids.get('goal');
  const draftOf = () => ctx.__spoolTest.store.get('drafts') || {};
  const keyOf = (root, name) => root + '|' + name;
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  goalBox.value = 'A 的一半';
  goalBox.oninput();
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, []));
  const blankB = goalBox.value;
  goalBox.value = 'B 的另一半';
  goalBox.oninput();
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  const backA = goalBox.value;
  ctx.__spoolTest.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, []));
  const backB = goalBox.value;
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  goalBox.value = '还在打';
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  const keptTyping = goalBox.value;
  goalBox.value = 'A 的一半';
  goalBox.oninput();
  const beforeNew = ctx.fetchCalls.length;
  ctx.__fetchImpl = (url) => {
    const path = String(url);
    if (path.indexOf('/session/new') >= 0) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ session: 'fresh-1' }) });
    }
    return Promise.resolve({
      ok: true, status: 200,
      json: async () => frame(ROOT_A, 'fresh-1', WROWS, SROWS_A, []),
    });
  };
  await ctx.__spoolTest.newSession();
  ctx.__fetchImpl = null;
  const freshValue = goalBox.value;
  const freshDraft = Object.prototype.hasOwnProperty.call(draftOf(), keyOf(ROOT_A, 'fresh-1'));
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  out.drafts = {
    blank_b: blankB,
    back_a: backA,
    back_b: backB,
    kept_typing: keptTyping,
    fresh_value: freshValue,
    fresh_draft: freshDraft,
    restored_a: goalBox.value,
    map_a: draftOf()[keyOf(ROOT_A, 'a1')] || '',
    map_b: draftOf()[keyOf(ROOT_B, 'a1')] || '',
    new_posts: ctx.fetchCalls.slice(beforeNew),
  };

  function userBubbles() {
    return ids.get('stream').querySelectorAll('.bubble').filter((node) => {
      return String(node.parentNode && node.parentNode._cls || '').indexOf('user') >= 0;
    });
  }
  function failedOf(text) {
    return userBubbles().filter((node) => {
      const wrap = node.parentNode;
      return node.textContent === text && wrap && wrap.classList.contains('failed');
    });
  }

  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  goalBox.value = '五百的原文';
  goalBox.oninput();
  ctx.__fetchImpl = () => Promise.resolve({
    ok: false, status: 500, json: async () => ({ error: '服务器坏了' }),
  });
  await ctx.__spoolTest.say();
  ctx.__fetchImpl = null;
  const failed500 = failedOf('五百的原文');
  const retry500 = failed500.length && failed500[0].parentNode.querySelector('.msg-retry');
  out.send_500 = {
    value: goalBox.value,
    focus: document.activeElement ? document.activeElement._id : '',
    draft: draftOf()[keyOf(ROOT_A, 'a1')] || '',
    bubbles: failed500.length,
    unsent: failed500.length ? failed500[0].parentNode.textContent : '',
    retry: retry500 ? retry500.textContent : '',
    pending: ctx.__spoolTest.app.thread.getPendingUser(),
    users: userBubbles().length,
  };
  ctx.__spoolTest.applyState(frame(ROOT_B, 'empty-b', WROWS, SROWS_B, []));
  const away = goalBox.value;
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  out.send_500_roundtrip = { away: away, back: goalBox.value };

  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  goalBox.value = '断网的原文';
  goalBox.oninput();
  ctx.__fetchImpl = () => Promise.reject(new Error('offline'));
  let rejected = false;
  try {
    await ctx.__spoolTest.say();
  } catch (err) {
    rejected = true;
  }
  ctx.__fetchImpl = null;
  const failedNet = failedOf('断网的原文');
  const retryNet = failedNet.length ? failedNet[0].parentNode.querySelector('.msg-retry') : null;
  const usersBeforeRetry = userBubbles().filter((node) => node.textContent === '断网的原文').length;
  const noteBefore = (ids.get('stream').textContent.match(/草稿还在输入框里，可以重试/g) || []).length;
  ctx.__fetchImpl = () => Promise.resolve({
    ok: false, status: 500, json: async () => ({ error: '仍失败' }),
  });
  const postsBefore = ctx.fetchCalls.length;
  if (retryNet) { await retryNet.onclick(); }
  ctx.__fetchImpl = null;
  out.send_net = {
    rejected: rejected,
    value: goalBox.value,
    focus: document.activeElement ? document.activeElement._id : '',
    draft: draftOf()[keyOf(ROOT_A, 'a1')] || '',
    bubbles: failedNet.length,
    retry: retryNet ? retryNet.textContent : '',
    pending: ctx.__spoolTest.app.thread.getPendingUser(),
    users_before: usersBeforeRetry,
    users_after: userBubbles().filter((node) => node.textContent === '断网的原文').length,
    still_failed: failedOf('断网的原文').length,
    posts: ctx.fetchCalls.slice(postsBefore),
    notes: (ids.get('stream').textContent.match(/草稿还在输入框里，可以重试/g) || []).length,
    notes_before: noteBefore,
  };

  ctx.__spoolTest.applyState(frame(ROOT_A, 'a2', WROWS, SROWS_A, []));
  goalBox.value = '重试成功的原文';
  goalBox.oninput();
  ctx.__fetchImpl = () => Promise.reject(new Error('offline'));
  await ctx.__spoolTest.say();
  ctx.__fetchImpl = null;
  const failedOk = failedOf('重试成功的原文');
  const retryOk = failedOk.length ? failedOk[0].parentNode.querySelector('.msg-retry') : null;
  ctx.__fetchImpl = () => Promise.resolve({
    ok: true, status: 200, json: async () => ({ ok: true }),
  });
  const okBefore = ctx.fetchCalls.length;
  if (retryOk) { await retryOk.onclick(); }
  const afterRetry = {
    posts: ctx.fetchCalls.slice(okBefore),
    users: userBubbles().filter((node) => node.textContent === '重试成功的原文').length,
    failed: failedOf('重试成功的原文').length,
    pending: ctx.__spoolTest.app.thread.getPendingUser(),
    value: goalBox.value,
  };
  ctx.__fetchImpl = null;
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a2', WROWS, SROWS_A, [
    { role: 'user', content: '重试成功的原文', meta: '' },
  ]));
  out.send_retry = {
    before: afterRetry,
    users: userBubbles().filter((node) => node.textContent === '重试成功的原文').length,
    pending: ctx.__spoolTest.app.thread.getPendingUser(),
  };

  const mdBubble = document.createElement('div');
  ctx.__spoolTest.fillRich(mdBubble, [
    '## 结论',
    '',
    '- 先跑 `spool status`',
    '- **注意** 看 [文档](https://example.test/docs)',
    '',
    '| 项 | 值 |',
    '| --- | --- |',
    '| `a` | **b** |',
  ].join('\n'));
  const tagged = (tag) => allNodes(mdBubble).filter((node) => node.tagName === tag);
  out.markdown = {
    heading: mdBubble.querySelector('.md-h') ? mdBubble.querySelector('.md-h').textContent : '',
    items: tagged('LI').map((node) => node.textContent),
    codes: mdBubble.querySelectorAll('.inline-code').map((node) => node.textContent),
    strong: mdBubble.querySelector('strong') ? mdBubble.querySelector('strong').textContent : '',
    link: mdBubble.querySelector('a') ? mdBubble.querySelector('a').getAttribute('href') : '',
    cells: tagged('TD').map((node) => node.textContent),
    raw: mdBubble.textContent,
  };

  const postsBeforeEmpty = ctx.fetchCalls.length;
  ctx.__spoolTest.applyState(frame('', '', WROWS, SROWS_A, [], {
    target: { root: '', session: '', pinned: false, workspace_source: 'none',
      session_source: 'none', note: '未选择工作区' },
  }));
  const pick = ids.get('stream').querySelector('.empty-state');
  const pickGo = pick ? pick.querySelector('.empty-go') : null;
  if (pickGo && pickGo.onclick) { pickGo.onclick(); }
  out.empty_pick = {
    mode: pick ? pick.dataset.mode : '',
    text: pick ? pick.textContent : '',
    go: pickGo ? pickGo.textContent : '',
    drawer: document.body.classList.contains('drawer'),
  };
  if (document.body.classList.contains('drawer')) { document.body.classList.remove('drawer'); }

  ctx.__spoolTest.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, []));
  const ready = ids.get('stream').querySelector('.empty-state');
  const sample = ready ? ready.querySelector('.empty-sample') : null;
  const beforeSample = ctx.fetchCalls.length;
  if (sample && sample.onclick) { sample.onclick(); }
  out.empty_ready = {
    mode: ready ? ready.dataset.mode : '',
    text: ready ? ready.textContent : '',
    samples: ready ? ready.querySelectorAll('.empty-sample').map((node) => node.textContent) : [],
    goal: ids.get('goal').value,
    focus: document.activeElement ? document.activeElement._id : '',
    posts: ctx.fetchCalls.length - beforeSample,
    draft: (ctx.__spoolTest.store.get('drafts') || {})[keyOf(ROOT_A, 'a1')] || '',
  };
  ids.get('goal').value = '派一个任务';
  ctx.__fetchImpl = () => Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  await ctx.__spoolTest.say();
  ctx.__fetchImpl = null;
  out.empty_after_send = {
    shown: ids.get('stream').querySelectorAll('.empty-state').length,
    posts: ctx.fetchCalls.length - postsBeforeEmpty,
  };
  ctx.__spoolTest.applyState(frame(ROOT_A, 'a2', WROWS, SROWS_A, []));
  const nextEmpty = ids.get('stream').querySelector('.empty-state');
  out.empty_switched = {
    mode: nextEmpty ? nextEmpty.dataset.mode : '',
    text: nextEmpty ? nextEmpty.textContent : '',
    shown: ids.get('stream').querySelectorAll('.empty-state').length,
  };

  const LONG = 'D:/very/long/path/that/should/not/wrap/beta-workspace';
  const SEARCH_WS = [
    { name: 'beta', alias: '', root: LONG, ready: true },
    { name: 'alpha', alias: '阿尔法', root: 'D:/alpha', ready: true },
    { name: 'spool-chat-9', alias: '', root: 'D:/scratch/spool-chat-9', ready: true },
    { name: 'tmp-notes', alias: '', root: 'D:/tmp-notes', ready: true },
    { name: 'temp-scratch', alias: '', root: 'D:/temp-scratch', ready: true },
    { name: 'my-tmp-files', alias: '', root: 'D:/my-tmp-files', ready: true },
  ];
  const SEARCH_SESS = [
    { id: 'a1', title: '当前会话', archived: false, last_active: 1, current: true },
    { id: 'alpha-2', title: 'Alpha 笔记', archived: false, last_active: 2, current: false },
  ];
  function sideNames(boxId) {
    const found = [];
    for (const node of ids.get(boxId).querySelectorAll('.li')) {
      const name = node.querySelector('.nm');
      if (name) { found.push(name.textContent); }
    }
    return found;
  }
  function typeSearch(value) {
    const box = ids.get('side-search');
    box.value = value;
    if (box.oninput) { box.oninput(); }
  }
  ctx.__spoolTest.applyState(frame(LONG, 'a1', SEARCH_WS, SEARCH_SESS, history(1, 's'), {
    target: {
      root: LONG, session: 'a1', session_title: '当前会话', pinned: false,
      workspace_source: 'explicit', session_source: 'explicit', note: '',
    },
  }));
  const where = ids.get('where');
  ctx.__spoolTest.setStatusOpen(true);
  const panel = ids.get('st-panel');
  out.where_bar = {
    text: where.textContent,
    title: where.title || '',
    status: panel ? panel.textContent : '',
  };
  out.search_default = { ws: sideNames('workspaces'), sess: sideNames('sessions') };
  const postsAtSearch = ctx.fetchCalls.length;
  typeSearch('alpha');
  out.search_alpha = { ws: sideNames('workspaces'), sess: sideNames('sessions') };
  typeSearch('zzzz-nope');
  let stopped = false;
  out.search_none = {
    ws: sideNames('workspaces'),
    sess: sideNames('sessions'),
    text: ids.get('workspaces').textContent,
  };
  typeSearch('');
  out.search_cleared = { ws: sideNames('workspaces'), sess: sideNames('sessions') };
  typeSearch('阿尔法');
  out.search_alias = { ws: sideNames('workspaces') };
  typeSearch('spool-chat');
  out.search_temp = { ws: sideNames('workspaces') };
  const box = ids.get('side-search');
  box.onkeydown({
    key: 'Escape',
    preventDefault() {},
    stopPropagation() { stopped = true; },
  });
  out.search_escape = {
    value: box.value,
    ws: sideNames('workspaces'),
    focus: document.activeElement ? document.activeElement._id : '',
    stopped: stopped,
    statusOpen: ctx.__spoolTest.store.get('statusOpen'),
    posts: ctx.fetchCalls.length - postsAtSearch,
  };
  ctx.__spoolTest.store.set('statusOpen', true);
  document.onkeydown({ key: 'Escape' });
  out.search_escape_global = { statusOpen: ctx.__spoolTest.store.get('statusOpen') };
  ctx.__spoolTest.applyState(frame('D:/scratch/spool-chat-9', 'a1', SEARCH_WS, SEARCH_SESS, history(1, 's'), {
    target: {
      root: 'D:/scratch/spool-chat-9', session: 'a1', session_title: '当前会话',
      pinned: false, workspace_source: 'explicit', session_source: 'explicit', note: '',
    },
  }));
  out.search_current_temp = { ws: sideNames('workspaces') };
  ctx.__spoolTest.applyState(frame('', '', SEARCH_WS, SEARCH_SESS, [], {
    target: {
      root: '', session: '', pinned: false,
      workspace_source: 'none', session_source: 'none', note: '',
    },
  }));
  out.where_empty = ids.get('where').textContent;

  // 任务卡：有 tasks 的助手消息画卡、不画旧轨迹；没有 tasks 的旧消息仍画轨迹。
  function taskEvent(seq, phase, status, summary, extra) {
    return Object.assign({
      seq: seq, step: phase === 'observe' ? 1 : 0, phase: phase, source: 'system',
      status: status, summary: summary, tool: '', tool_call_id: '', intent: '',
      refs: [], artifacts: [], raw: 'RAW_SECRET',
    }, extra || {});
  }
  function taskGroup(status, events, goal) {
    return {
      task_id: 'run:' + status, run_id: 'run', turn_id: status,
      goal: goal || '写一个文件', status: status, artifacts: [], events: events,
    };
  }
  const longGoal = '写一个文件' + '而且目标很长'.repeat(6);
  const story = [
    taskEvent(1, 'understand', 'running', longGoal),
    taskEvent(2, 'retrieval', 'ok', '预取目录'),
    taskEvent(3, 'observe', 'ok', '4 行', { tool: 'read_file', intent: '读源码', tool_call_id: 'tc-1-0', source: 'tool' }),
    taskEvent(4, 'approval', 'await', 'a.py 待确认', { tool_call_id: 'tc-1-0' }),
    taskEvent(5, 'verify', 'error', '编译失败'),
    taskEvent(6, 'final', 'ok', '这是一段很长的最终答复不应该出现在任务卡里'),
  ];
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame('D:/task-ui', 'a1', WROWS, SROWS_A, [
    { role: 'user', content: '旧问题', created_at: 1 },
    { role: 'assistant', content: '旧答复', created_at: 2, trace: [
      { type: 'step', text: '第 1 步' },
      { type: 'tool', ok: false, text: 'read_file → 失败', detail: '文件不存在' },
    ] },
    { role: 'user', content: '写一个文件', created_at: 3 },
    { role: 'assistant', content: '写好了', created_at: 4,
      trace: [{ type: 'step', text: '不该出现的轨迹' }],
      tasks: [
        taskGroup('verify_failed', story, longGoal),
        taskGroup('failed', [
          taskEvent(1, 'understand', 'running', '做不完'),
          taskEvent(2, 'error', 'error', '已达步数上限'),
        ]),
        taskGroup('cancelled', [
          taskEvent(1, 'understand', 'running', '停下来'),
          taskEvent(2, 'final', 'cancelled', '已取消的长文'),
        ]),
        taskGroup('done', [
          taskEvent(1, 'understand', 'running', '做完了'),
          taskEvent(2, 'final', 'ok', '做完的长文'),
        ]),
      ] },
  ], {
    diffs: [{ path: 'a.py', text: '+x\n', tool_call_id: 'tc-1-0' }],
    awaiting: 1,
  }));
  const assistants = ids.get('stream').querySelectorAll('.msg').filter((node) =>
    String(node._cls || '').indexOf('assistant') >= 0);
  const oldCard = assistants[0];
  const newCard = assistants[assistants.length - 1];
  const oldHead = oldCard ? oldCard.querySelector('.trace-head') : null;
  if (oldHead) { oldHead.onclick(); }
  const cards = newCard ? newCard.querySelectorAll('.task-card') : [];
  const first = cards[0] || null;
  const firstHead = first ? first.querySelector('.task-head') : null;
  const arias = cards.map((node) => node.querySelector('.task-head').getAttribute('aria-expanded'));
  const collapsed = firstHead ? firstHead.getAttribute('aria-expanded') : '';
  const goalNode = first ? first.querySelector('.task-goal') : null;
  if (firstHead) { firstHead.onclick(); }
  const openedAria = firstHead ? firstHead.getAttribute('aria-expanded') : '';
  const openedBody = first ? first.querySelector('.task-body') : null;
  const openedText = openedBody ? openedBody.textContent : '';
  const approvalRows = openedBody ? openedBody.querySelectorAll('.task-approval').length : 0;
  const observeRows = openedBody ? openedBody.querySelectorAll('.task-observe').length : 0;
  const jump = openedBody ? openedBody.querySelector('.task-jump') : null;
  const ask = ids.get('stream').querySelector('.ask-card');
  const toolNode = openedBody ? openedBody.querySelector('.task-tool') : null;
  const toolText = toolNode ? toolNode.textContent : '';
  const intentText = openedBody && openedBody.querySelector('.task-intent')
    ? openedBody.querySelector('.task-intent').textContent : '';
  const summaryNode = openedBody ? openedBody.querySelector('.task-summary') : null;
  const verifyNode = openedBody ? openedBody.querySelector('.task-verify') : null;
  const rowStatus = openedBody && openedBody.querySelector('.task-row-status')
    ? openedBody.querySelector('.task-row-status').textContent : '';
  const rowCounts = {
    retrieval: openedBody ? openedBody.querySelectorAll('.task-retrieval').length : 0,
    verify: openedBody ? openedBody.querySelectorAll('.task-verify').length : 0,
    final: openedBody ? openedBody.querySelectorAll('.task-final').length : 0,
  };
  if (jump) { jump.onclick(); }
  const askScrolled = ask ? ask._scrolled || 0 : 0;
  const askFocus = document.activeElement ? document.activeElement.className : '';
  const failedCard = cards[1] || null;
  const failedHead = failedCard ? failedCard.querySelector('.task-head') : null;
  if (failedHead) { failedHead.onclick(); }
  const errorRow = failedCard ? failedCard.querySelector('.task-error') : null;
  const errorText = errorRow ? errorRow.textContent : '';
  if (firstHead) { firstHead.onclick(); }
  out.task_cards = {
    old_trace: oldCard ? Boolean(oldCard.querySelector('.trace')) : false,
    old_trace_text: oldCard && oldCard.querySelector('.trace-body')
      ? oldCard.querySelector('.trace-body').textContent : '',
    new_trace: newCard ? Boolean(newCard.querySelector('.trace')) : true,
    count: cards.length,
    heads: cards.map((node) => node.querySelector('.task-head').textContent),
    arias: arias,
    chevrons: newCard ? newCard.querySelectorAll('.task-chevron').length : 0,
    chips: newCard ? newCard.querySelectorAll('.task-chip').length : 0,
    marks: cards.map((node) => node.querySelector('.task-mark').textContent),
    collapsed: collapsed,
    opened_aria: openedAria,
    closed_again: firstHead ? firstHead.getAttribute('aria-expanded') : '',
    opened: openedText,
    approval_rows: approvalRows,
    observe_rows: observeRows,
    retrieval_rows: rowCounts.retrieval,
    verify_rows: rowCounts.verify,
    verify_text: verifyNode ? verifyNode.textContent : '',
    final_rows: rowCounts.final,
    error_text: errorText,
    tool: toolText,
    intent: intentText,
    summary: summaryNode ? summaryNode.textContent : '',
    summary_title: summaryNode ? summaryNode.getAttribute('title') : '',
    summary_parent: summaryNode && summaryNode.parentNode
      ? summaryNode.parentNode.className : '',
    tool_parent: toolNode && toolNode.parentNode ? toolNode.parentNode.className : '',
    row_status: rowStatus,
    jump: jump ? jump.textContent : '',
    ask_id: ask ? ask.getAttribute('data-tool-call-id') : '',
    ask_scrolled: askScrolled,
    ask_focus: askFocus,
    task_apply: newCard ? newCard.querySelectorAll('.ask-apply').length : 1,
    goal_title: goalNode ? goalNode.getAttribute('title') : '',
    raw: newCard ? newCard.textContent.indexOf('RAW_SECRET') >= 0 : true,
    goal_times: openedText.split('写一个文件').length - 1,
  };
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame('D:/task-live', 'a1', WROWS, SROWS_A, [
    { role: 'user', content: '写一个文件', created_at: 1 },
  ], {
    running: true,
    goal: '写一个文件',
    trace: { open: true, goal: '写一个文件', lines: [{ type: 'step', text: '第 1 步' }] },
    task_groups: [taskGroup('running', [
      taskEvent(1, 'understand', 'running', '写一个文件'),
      taskEvent(2, 'retrieval', 'ok', '预取目录'),
      taskEvent(3, 'observe', 'ok', '正在读', { tool: 'search_code', step: 0, tool_call_id: 'tc-0-0' }),
    ])],
  }));
  const liveCard = lastCard();
  const liveTrace = liveCard ? liveCard.querySelector('.trace') : null;
  const liveTask = liveCard ? liveCard.querySelector('.task-card') : null;
  const liveHead = liveTask ? liveTask.querySelector('.task-head') : null;
  out.task_running = {
    role: liveCard ? liveCard._cls : '',
    head: liveHead ? liveHead.textContent : '',
    aria: liveHead ? liveHead.getAttribute('aria-expanded') : '',
    body: liveTask && liveTask.querySelector('.task-body')
      ? liveTask.querySelector('.task-body').textContent : '',
    trace_hidden: liveTrace ? liveTrace.classList.contains('hidden') : false,
    trace_visible_text: liveTrace && !liveTrace.classList.contains('hidden')
      ? liveTrace.textContent : '',
  };
  function liveFrame(summary) {
    return frame('D:/task-live', 'a1', WROWS, SROWS_A, [
      { role: 'user', content: '写一个文件', created_at: 1 },
    ], {
      running: true,
      goal: '写一个文件',
      trace: { open: true, goal: '写一个文件', lines: [{ type: 'step', text: '第 1 步' }] },
      task_groups: [taskGroup('running', [
        taskEvent(1, 'understand', 'running', '写一个文件'),
        taskEvent(2, 'retrieval', 'ok', '预取目录'),
        taskEvent(3, 'observe', 'ok', summary, { tool: 'search_code', step: 0, tool_call_id: 'tc-0-0' }),
      ])],
    });
  }
  if (liveHead) { liveHead.onclick(); }
  ctx.__spoolTest.applyState(liveFrame('又读了一段'));
  const afterClose = liveHead ? liveHead.getAttribute('aria-expanded') : '';
  if (liveHead) { liveHead.onclick(); }
  const openedNow = liveHead ? liveHead.getAttribute('aria-expanded') : '';
  const openedSummary = liveTask && liveTask.querySelector('.task-summary')
    ? liveTask.querySelector('.task-summary').textContent : '';
  ctx.__spoolTest.applyState(liveFrame('第三次'));
  out.task_pin = {
    closed: afterClose,
    opened: openedNow,
    stayed: liveHead ? liveHead.getAttribute('aria-expanded') : '',
    opened_summary: openedSummary,
    rebound_summary: liveTask && liveTask.querySelector('.task-summary')
      ? liveTask.querySelector('.task-summary').textContent : '',
  };
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame('D:/task-cmd', 'a1', WROWS, SROWS_A, [
    { role: 'user', content: '跑一下', created_at: 1 },
    { role: 'assistant', content: '等确认', created_at: 2, tasks: [
      taskGroup('done', [
        taskEvent(1, 'understand', 'running', '跑一下'),
        taskEvent(2, 'observe', 'ok', 'find .', {
          tool: 'exec', intent: '找文件', tool_call_id: 'tc-cmd', step: 0,
        }),
        taskEvent(3, 'approval', 'await', 'find .', { tool_call_id: 'tc-cmd' }),
      ]),
    ] },
  ], {
    awaiting: 1,
    awaiting_detail: { command: 'find .', reason: '找文件', tool_call_id: 'tc-cmd' },
  }));
  const cmdCard = ids.get('stream').querySelector('.task-card');
  const cmdHead = cmdCard ? cmdCard.querySelector('.task-head') : null;
  const cmdCollapsed = cmdHead ? cmdHead.getAttribute('aria-expanded') : '';
  if (cmdHead) { cmdHead.onclick(); }
  const cmdJump = cmdCard ? cmdCard.querySelector('.task-jump') : null;
  const cmdAsk = ids.get('stream').querySelector('.ask-card');
  if (cmdJump) { cmdJump.onclick(); }
  out.task_command = {
    collapsed: cmdCollapsed,
    jump: cmdJump ? cmdJump.textContent : '',
    ask_id: cmdAsk ? cmdAsk.getAttribute('data-tool-call-id') : '',
    ask_cls: cmdAsk ? cmdAsk.className : '',
    ask_scrolled: cmdAsk ? cmdAsk._scrolled || 0 : 0,
    ask_focus: document.activeElement ? document.activeElement.className : '',
    task_apply: cmdCard ? cmdCard.querySelectorAll('.ask-apply').length : 1,
  };

  function panelGroup(status, goal, events) {
    return taskGroup(status, events, goal);
  }
  const panelGroups = [
    panelGroup('done', '先做完', [
      taskEvent(1, 'understand', 'running', '先做完'),
      taskEvent(2, 'final', 'ok', '结论'),
    ]),
    panelGroup('verify_failed', '验证那件', [
      taskEvent(1, 'understand', 'running', '验证那件'),
      taskEvent(2, 'verify', 'error', '编译失败'),
      taskEvent(3, 'final', 'ok', '仍收尾'),
    ]),
    panelGroup('failed', '做不完', [
      taskEvent(1, 'understand', 'running', '做不完'),
      taskEvent(2, 'error', 'error', '已达步数上限'),
    ]),
    panelGroup('running', '还在跑的长目标', [
      taskEvent(1, 'understand', 'running', '还在跑的长目标'),
      taskEvent(2, 'observe', 'ok', '4 行', {
        tool: 'read_file', intent: '读源码', tool_call_id: 'tc-panel', source: 'tool',
      }),
      taskEvent(3, 'approval', 'await', 'a.py 待确认', {
        tool_call_id: 'tc-panel',
        artifacts: [{ type: 'diff', path: 'a.py', ref: 'tc-panel' }],
      }),
    ]),
  ];
  ctx.__spoolTest.resetView();
  ctx.__spoolTest.applyState(frame('D:/task-panel', 'a1', WROWS, SROWS_A, [
    { role: 'user', content: '列一下', created_at: 1 },
  ], {
    task_groups: panelGroups,
    diffs: [{ path: 'a.py', text: '+x\n', tool_call_id: 'tc-panel' }],
    awaiting: 1,
  }));
  const taskBtn = ids.get('tasks-btn');
  const taskPanel = ids.get('tasks-panel');
  function panelLines() {
    return ids.get('tasks-list').querySelectorAll('.task-line');
  }
  function clickFilter(label) {
    const nodes = ids.get('tasks-filters').children;
    for (const node of nodes) {
      if (node.textContent === label) { node.onclick(); return true; }
    }
    return false;
  }
  const closedCount = ids.get('tasks-count').textContent;
  const closedWait = ids.get('tasks-wait').textContent;
  const closedHidden = taskPanel.classList.contains('hidden');
  if (taskBtn) { taskBtn.onclick(); }
  const openedExpanded = taskBtn ? taskBtn.getAttribute('aria-expanded') : '';
  const openedFocus = document.activeElement ? document.activeElement._id : '';
  const openedLines = panelLines().map((node) => node.querySelector('.task-goal').textContent);
  const openedMarks = panelLines().map((node) => node.querySelector('.task-mark').textContent);
  const openedWait = panelLines()[0] && panelLines()[0].querySelector('.task-wait')
    ? panelLines()[0].querySelector('.task-wait').textContent : '';
  if (panelLines()[0]) { panelLines()[0].onclick(); }
  const detail = ids.get('tasks-detail');
  const detailJump = detail.querySelector('.task-jump');
  const detailTool = detail.querySelector('.task-tool')
    ? detail.querySelector('.task-tool').textContent : '';
  const detailOpen = detail.querySelector('.task-head')
    ? detail.querySelector('.task-head').getAttribute('aria-expanded') : '';
  const detailRaw = detail.textContent.indexOf('RAW_SECRET') >= 0;
  const detailApply = detail.querySelectorAll('.ask-apply').length;
  const panelAsk = ids.get('stream').querySelector('.ask-card');
  if (detailJump) { detailJump.onclick(); }
  const jumped = {
    id: panelAsk ? panelAsk.getAttribute('data-tool-call-id') : '',
    scrolled: panelAsk ? panelAsk._scrolled || 0 : 0,
    focus: document.activeElement ? document.activeElement.className : '',
  };
  clickFilter('失败');
  const failedGoals = panelLines().map((node) => node.querySelector('.task-goal').textContent);
  clickFilter('完成');
  const doneGoals = panelLines().map((node) => node.querySelector('.task-goal').textContent);
  clickFilter('等审批');
  const awaitGoals = panelLines().map((node) => node.querySelector('.task-goal').textContent);
  clickFilter('进行中');
  const runningGoals = panelLines().map((node) => node.querySelector('.task-goal').textContent);
  ctx.__spoolTest.applyState(frame('D:/task-panel', 'a1', WROWS, SROWS_A, [
    { role: 'user', content: '列一下', created_at: 1 },
  ], {
    task_groups: [panelGroup('running', '只剩这一件', [
      taskEvent(1, 'understand', 'running', '只剩这一件'),
    ])],
  }));
  clickFilter('完成');
  const emptyText = ids.get('tasks-list').textContent;
  const shrunkCount = ids.get('tasks-count').textContent;
  const closeBtn = ids.get('tasks-close');
  if (closeBtn) { closeBtn.onclick(); }
  const closedBack = document.activeElement ? document.activeElement._id : '';
  if (taskBtn) { taskBtn.onclick(); }
  document.onkeydown({ key: 'Escape' });
  out.task_panel = {
    count: closedCount,
    wait: closedWait,
    closed: closedHidden,
    opened: openedExpanded,
    focus: openedFocus,
    lines: openedLines,
    marks: openedMarks,
    wait_mark: openedWait,
    detail_open: detailOpen,
    tool: detailTool,
    jump: detailJump ? detailJump.textContent : '',
    raw: detailRaw,
    detail_apply: detailApply,
    ask_id: jumped.id,
    ask_scrolled: jumped.scrolled,
    ask_focus: jumped.focus,
    failed: failedGoals,
    done: doneGoals,
    awaiting: awaitGoals,
    running: runningGoals,
    empty: emptyText,
    shrunk: shrunkCount,
    close_focus: closedBack,
    esc: taskPanel.classList.contains('hidden'),
    esc_focus: document.activeElement ? document.activeElement._id : '',
  };
}

