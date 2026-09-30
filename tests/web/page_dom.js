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
// 每个 diff 的头一行（"哪个文件 + 加了几行减了几行"）。
function diffHeads() {
  return ids.get('stream').querySelectorAll('.diff').map(
    (node) => (node.firstChild ? node.firstChild.textContent : ''));
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
    const beforePlain = ids.get('stream').querySelectorAll('.failure').length;
    ctx.showFailure('失败：', '工具 read_file 失败：文件不存在', null);
    out.failure_plain_blocks = ids.get('stream').querySelectorAll('.failure').length - beforePlain;
    const plain = ids.get('stream').querySelectorAll('.sys-line');
    out.failure_plain = plain[plain.length - 1].textContent;
    const before = ids.get('stream').querySelectorAll('.failure').length;
    ctx.showFailure('失败：', 'LlamaCppError: 无法连接 llama-server', {
      kind: 'connect', url: 'http://127.0.0.1:9',
    });
    out.failure_blocks = ids.get('stream').querySelectorAll('.failure').length - before;

    // ②-2 探活那条通知：模型断了，而且**说得出这是什么时候探的**（探活挪到后台之后，
    // `/state` 给的是最近一次结果，可能是几秒前那一份——陈旧值可以接受，
    // 静默的陈旧值不行）。它现在是右上角的一个气泡：可关、而且**不许自己消失**。
    const beforeHealth = snap();
    const DOWN = {
      model: { ok: false, target: 'http://127.0.0.1:8080', detail: '拒绝连接', kind: 'connect' },
      services: [], state: 'down', probed_at: 1700000000, age_s: 3,
    };
    ctx.showHealth(DOWN);
    const downToast = toastWith('health|model');
    out.health_toast = {
      keys: toastKeys(),
      text: toastBody(downToast),
      kind: downToast ? downToast._cls : '',
      close_buttons: closeButtons().length,
      close_labels: closeLabels(),
      stack_shown: toastsShown(),
      all_shown: allButtonShown(),
      cost: since(beforeHealth),
    };
    // 同一份内容再来一拍（3 秒轮询就是这么送的）：一个节点都不该新建。
    const beforeHealthRepeat = snap();
    ctx.showHealth(DOWN);
    out.health_repeat = since(beforeHealthRepeat);
    // 模型又通了：这一条**自己收掉**（状态没了，不用人去关）。
    ctx.showHealth({
      model: { ok: true, target: '', detail: '假模型' }, services: [],
      state: 'up', probed_at: 1, age_s: 1,
    });
    out.health_cleared = { keys: toastKeys(), shown: toastsShown() };
    // 还没探过：既不许画成"通了"，也不许画成"连不上"。
    ctx.showHealth({
      model: { ok: null, target: '', detail: '还没探过（后台探活刚起来）', kind: '' },
      services: [], state: 'unknown', probed_at: null, age_s: null,
    });
    out.health_line_unknown = toastText('health|unknown');
    ctx.showHealth({ model: { ok: true, target: '', detail: '假模型' }, services: [], state: 'up', probed_at: 1, age_s: 1 });

    // ①-6 点「＋ 新建会话」：**先让服务端建出来、再切过去**，两个请求都要发对
    // （以前这里只发一次 /target，而服务端不新建会话——那个按钮点下去只有 400）。
    const posts = [];
    const SROWS_NEW = [{ id: 'web-new', title: '', archived: false, last_active: 1, current: true }];
    ctx.prompt = () => 'web-new';
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
      out.new_session = { calls: posts, selected: highlighted() };
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
  ctx.closeAllToasts();

  // ①-7 一次性提示（`info`）**自己走**：页面排了一个定时器，到点那条就没了。
  const beforeInfo = snap();
  ctx.notifyInfo('已恢复会话 a2', []);
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
  ctx.showFailure('失败：', 'LlamaCppError: 无法连接 llama-server（ConnectTimeout）', {
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
  const elsewhere = (goal) => ({
    running_targets: [
      { root: 'D:/ws-b', session: 'a1', name: 'ws-b', goal: goal, current: false, awaiting: false },
      { root: 'D:/ws-c', session: 'c1', name: 'ws-c', goal: '另一条在跑', current: false, awaiting: true },
    ],
  });
  ctx.showElsewhere(elsewhere('改前端那一版'));
  const stackNodes = toastNodes();
  out.stack = {
    keys: toastKeys(),
    parents: stackNodes.map((node) => (node.parentNode ? node.parentNode._id : '')),
    kind: stackNodes.length ? stackNodes[0]._cls : '',
    all_shown: allButtonShown(),
    shown: toastsShown(),
  };
  // 关掉一条：另一条留着；只剩一条时「全部关闭」收起来。
  const first = stackNodes[0].querySelector('.toast-close');
  if (first) { first.onclick(); }
  out.stack.after_one = { keys: toastKeys(), all_shown: allButtonShown(), shown: toastsShown() };
  // 「全部关闭」：一条不剩，整块也收起来。
  ctx.closeAllToasts();
  out.stack.after_all = { keys: toastKeys(), shown: toastsShown() };

  // ①-10 人关掉的那一份内容不再自己弹回来；内容真变了才回。
  ctx.showElsewhere(elsewhere('改前端那一版'));
  const dismissedKey = toastKeys()[0];
  ctx.dismissToast(dismissedKey);
  out.dismiss = { after_close: toastKeys().length };
  ctx.showElsewhere(elsewhere('改前端那一版'));
  out.dismiss.same_again = toastKeys().length;
  ctx.showElsewhere(elsewhere('换了个活'));
  out.dismiss.changed = toastKeys().length;
  ctx.closeAllToasts();

  // ②-3 没有待确认内容：这块 UI **在页面上不存在**（没有卡片、没有那两个按钮）。
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));
  out.card_absent = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
    apply_buttons: buttonsWith('应用').length,
    reject_buttons: buttonsWith('拒绝').length,
  };

  // ②-4 有待确认内容：卡片出现在**对话流里**，写着谁的、哪个文件、改了多少。
  const CARD_FRAME = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [
      { path: 'src/a.py', text: '@@ -1,2 +1,3 @@\n-old\n+new\n+more' },
      { path: 'src/b.py', text: '--- a\n+++ b\n+only' },
    ],
    awaiting: 2,
  });
  const beforeCard = snap();
  ctx.applyState(CARD_FRAME);
  const card = ids.get('stream').querySelector('.ask-card');
  out.card_asking = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    in_stream: inStream(card),
    state: card ? card.dataset.state : '',
    who: card ? card.querySelector('.ask-who').textContent : '',
    kind: card ? card.querySelector('.ask-kind').textContent : '',
    summaries: diffHeads(),
    apply_buttons: buttonsWith('应用').length,
    reject_buttons: buttonsWith('拒绝').length,
    cost: since(beforeCard),
  };
  // 同一帧再来一次：卡片不重建（轮询/补帧会一遍遍送同一份状态）。
  const beforeCardRepeat = snap();
  ctx.applyState(CARD_FRAME);
  out.card_repeat = since(beforeCardRepeat);

  // ②-5 点「应用」：卡片变成一行状态，不再是等你点的卡片；按钮没了。
  const decidePosts = [];
  ctx.__fetchImpl = (url) => {
    decidePosts.push(url);
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.decide(true);
  ctx.__fetchImpl = null;
  const appliedRow = ids.get('stream').querySelector('.ask-row');
  out.card_applied = {
    calls: decidePosts,
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
    text: appliedRow ? appliedRow.textContent : '',
    kind: appliedRow ? appliedRow._cls : '',
    apply_buttons: buttonsWith('应用').length,
    reject_buttons: buttonsWith('拒绝').length,
  };

  // ②-6 再来一份待确认、点「拒绝」：另起一张新卡片，不是把上面那行改回问句。
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/c.py', text: '+one' }], awaiting: 1,
  }));
  const askingAgain = ids.get('stream').querySelectorAll('.ask-card').length;
  const denyPosts = [];
  ctx.__fetchImpl = (url) => {
    denyPosts.push(url);
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
  };
  await ctx.decide(false);
  ctx.__fetchImpl = null;
  const rows = ids.get('stream').querySelectorAll('.ask-row');
  out.card_rejected = {
    calls: denyPosts,
    new_card: askingAgain,
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: rows.length,
    last: rows.length ? rows[rows.length - 1].textContent : '',
  };

  // ②-7 被拒（服务端 409「当前没有待确认的改动」）：卡片退回"等你点"，别停在已应用上骗人。
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/d.py', text: '+x' }], awaiting: 1,
  }));
  ctx.__fetchImpl = () => Promise.resolve({
    ok: false, status: 409, json: async () => ({ error: '当前没有待确认的改动' }),
  });
  await ctx.decide(true);
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
  ctx.showReadonly({ readonly: true, host: '0.0.0.0' });
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [], awaiting: 0, readonly: true, host: '0.0.0.0',
  }));
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [{ path: 'src/e.py', text: '+y' }], awaiting: 1, readonly: true, host: '0.0.0.0',
  }));
  out.card_readonly = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    state: (ids.get('stream').querySelector('.ask-card') || { dataset: {} }).dataset.state,
    apply_buttons: buttonsWith('应用').length,
    reject_buttons: buttonsWith('拒绝').length,
    actions: ids.get('stream').querySelectorAll('.ask-actions').length,
  };
  // 只读是**绑定地址**决定的，一次会话里不会来回变；这里把状态收回去，
  // 免得后面那几条把"只读时建的那张卡片"当成正常情况。
  ctx.showReadonly({ readonly: false, host: '127.0.0.1' });
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));

  // ②-9 命令授权也是同一张卡片（它没有 diff，但有"要执行什么"）。
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), {
    diffs: [], awaiting: 1,
    awaiting_detail: { count: 1, command: 'whoami', reason: '白名单外' },
  }));
  const askCard = ids.get('stream').querySelector('.ask-card');
  out.card_command = {
    kind: askCard ? askCard.querySelector('.ask-kind').textContent : '',
    body: askCard ? askCard.querySelector('.ask-body').textContent : '',
    apply_buttons: buttonsWith('应用').length,
  };

  // ②-10 服务端说没有待确认了（`awaiting` 没了 / 是 0）：卡片**从页面上消失**。
  ctx.applyState(frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''), { diffs: [], awaiting: 0 }));
  out.card_gone = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    rows: ids.get('stream').querySelectorAll('.ask-row').length,
    apply_buttons: buttonsWith('应用').length,
    reject_buttons: buttonsWith('拒绝').length,
  };
  // 缺字段（老服务端 / 空快照）也按"没有待确认内容"办。
  const bare = frame(ROOT_B, 'a1', WROWS, SROWS_B, history(0, ''));
  delete bare.awaiting;
  delete bare.diffs;
  ctx.applyState(bare);
  out.card_missing_field = {
    cards: ids.get('stream').querySelectorAll('.ask-card').length,
    apply_buttons: buttonsWith('应用').length,
  };

  // ③-1 历史：分隔线还在（一条线），但那句"给人看的、不进模型上下文"没了，
  // 历史消息本身照旧画在对话流里（略淡那一档）。
  ctx.applyState(frame(ROOT_A, 'a1', WROWS, SROWS_A, history(4, 'h')));
  out.history = {
    seps: ids.get('stream').querySelectorAll('.history-sep').length,
    sep_text: (ids.get('stream').querySelector('.history-sep') || { textContent: '' }).textContent,
    bubbles: streamBubbles(),
    marked: ids.get('stream').querySelectorAll('.history').length,
    texts: ids.get('stream').querySelectorAll('.bubble').map((node) => node.textContent),
  };
}
