// spool web · js/core/dom.js
/* 内联 SVG：零外链图标。
   下面这几行里有 "<"，但它长在**字符串里面**，不是 HTML 解析器看得见的位置：
   能把这段脚本提前收掉的只有"小于号 + 斜杠 + script"那一个序列，而它一次都不出现
   —— `tests/web/test_page.py` 用 node --check 与那两条钉住这件事。 */
const ICON = {
  menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></svg>',
  send: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 12l15-7-6 7 6 7z"></path></svg>',
  // 运行中那一枚键变成「停止」：一个方块（与"发送"的箭头形状上可分开）。
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"></rect></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v13"></path><path d="M6 13l6 6 6-6"></path></svg>',
  close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12"></path><path d="M18 6L6 18"></path></svg>',
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="5" y="11" width="14" height="9" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.08a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.08A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6h.08A1.7 1.7 0 0 0 10.63 3V3a2 2 0 1 1 4 0v.08A1.7 1.7 0 0 0 16.18 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 20.55 9v.08A1.7 1.7 0 0 0 22.1 10.63H22a2 2 0 1 1 0 4h-.08A1.7 1.7 0 0 0 20.37 16z"></path></svg>',
  pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>',
};

const stream = document.getElementById('stream');
const meta = document.getElementById('meta');
const conn = document.getElementById('conn');
const sendBtn = document.getElementById('send');
const goalBox = document.getElementById('goal');
const whereBox = document.getElementById('where');
const toastsBox = document.getElementById('toasts');
const toastAllBtn = document.getElementById('toast-all');
// 状态区那几枚徽标（在右上角 `#hints` 里，脱离文档流）：常态下只剩一枚小圆点，
// 所以徽标里的节点都得现建。
const stHealth = document.getElementById('st-health');
const stMode = document.getElementById('st-mode');
const stTarget = document.getElementById('st-target');
const stPair = document.getElementById('st-pair');
const stElsewhere = document.getElementById('st-elsewhere');
const stPanel = document.getElementById('st-panel');
const stClose = document.getElementById('st-close');
const sidebarBox = document.getElementById('sidebar');
const menuBtn = document.getElementById('menu');
const workspaceList = document.getElementById('workspaces');
const sessionList = document.getElementById('sessions');
const newBtn = document.getElementById('new-session');
const thinkingBox = document.getElementById('thinking');
const toBottomBtn = document.getElementById('toBottom');
const scrim = document.getElementById('scrim');
// 设置弹层：默认不出现；只在人主动点页脚「设置」时才建归档行。
const settingsBox = document.getElementById('settings');
const settingsBtn = document.getElementById('settings-btn');
const settingsClose = document.getElementById('settings-close');
const settingsIcon = document.getElementById('settings-icon');
const archivedList = document.getElementById('archived-list');
const archivedCount = document.getElementById('archived-count');
const archivedCountInline = document.getElementById('archived-count-inline');
const tasksBtn = document.getElementById('tasks-btn');
const tasksPanel = document.getElementById('tasks-panel');
const tasksClose = document.getElementById('tasks-close');
const tasksCount = document.getElementById('tasks-count');
const tasksWait = document.getElementById('tasks-wait');
const tasksSearch = document.getElementById('tasks-search');
const tasksMatch = document.getElementById('tasks-match');
const tasksScope = document.getElementById('tasks-scope');
const tasksHistory = document.getElementById('tasks-history');
const tasksFilters = document.getElementById('tasks-filters');
const tasksList = document.getElementById('tasks-list');
const tasksDetail = document.getElementById('tasks-detail');
const renameBox = document.getElementById('rename');
const renameForm = document.getElementById('rename-form');
const renameInput = document.getElementById('rename-input');
const renameError = document.getElementById('rename-error');
const renameClose = document.getElementById('rename-close');
const renameCancel = document.getElementById('rename-cancel');
const renameSave = document.getElementById('rename-save');

function h(tag, className) {
  const node = document.createElement(tag);
  if (className) { node.className = className; }
  return node;
}

function text(value) {
  return document.createTextNode(value == null ? '' : String(value));
}

function on(node, type, fn) {
  if (!node || typeof node.addEventListener !== 'function') { return node; }
  node.addEventListener(type, fn);
  return node;
}

function focus(node) {
  if (node && typeof node.focus === 'function') { node.focus(); }
  return node;
}

// spool web · js/core/store.js
// 页面状态只活在这一个对象里。feature 通过 ctx.store 读写，不再各写一份 let。

const WAIT_PREFIX = '模型生成中，已等待';
const RUN_MARK = '运行中';
const LOCK = 'lock';

function createStore(initial) {
  const state = {};
  const seed = initial || {};
  const seedKeys = Object.keys(seed);
  for (let index = 0; index < seedKeys.length; index += 1) {
    state[seedKeys[index]] = seed[seedKeys[index]];
  }
  const listeners = [];
  function emit(keys) {
    const partial = {};
    for (let index = 0; index < keys.length; index += 1) {
      partial[keys[index]] = state[keys[index]];
    }
    for (let index = 0; index < listeners.length; index += 1) {
      listeners[index](keys, partial);
    }
  }
  return {
    get: function (key) { return state[key]; },
    set: function (key, value) {
      if (!Object.prototype.hasOwnProperty.call(state, key)) { return value; }
      if (Object.is(state[key], value)) { return value; }
      state[key] = value;
      emit([key]);
      return value;
    },
    patch: function (partial) {
      const incoming = Object.keys(partial || {});
      const keys = [];
      for (let index = 0; index < incoming.length; index += 1) {
        const key = incoming[index];
        if (!Object.prototype.hasOwnProperty.call(state, key)) { continue; }
        if (Object.is(state[key], partial[key])) { continue; }
        state[key] = partial[key];
        keys.push(key);
      }
      if (keys.length) { emit(keys); }
      return state;
    },
    update: function (key, fn) {
      if (!Object.prototype.hasOwnProperty.call(state, key)) { return undefined; }
      state[key] = fn(state[key]);
      emit([key]);
      return state[key];
    },
    subscribe: function (fn) {
      listeners.push(fn);
      return function () {
        const at = listeners.indexOf(fn);
        if (at >= 0) { listeners.splice(at, 1); }
      };
    },
  };
}

const INITIAL_STATE = {
  running: false,
  decided: false,
  stopping: false,
  stoppedRun: false,
  readonly: false,
  session: 'cli',
  pendings: [],
  drawn: 0,
  busy: false,
  bound: '',
  sidebarSig: '',
  chosen: {root: '', session: ''},
  armed: '',
  lastState: {},
  thinking: false,
  card: null,
  cardAsking: false,
  cardApply: null,
  cardReject: null,
  cardSig: '',
  healthView: {cls: 'warn', label: '探活中', lines: [], raw: ''},
  healthInfo: {},
  pairingRows: [],
  elsewhereRows: [],
  readOnlyNote: '',
  pinnedNote: '',
  pinned: false,
  targetNotes: [],
  targetMissing: false,
  statusOpen: false,
  settingsOpen: false,
  tasksOpen: false,
  renameOpen: false,
  lastSettingsFocus: null,
  drafts: {},
};
const STATE_KEYS = Object.keys(INITIAL_STATE);
const store = createStore(INITIAL_STATE);

// spool web · js/core/ctx.js
// 装配根创建的那一个实例。feature 工厂用参数接住它，不读全局表。
function createApp(store) {
  return { store: store };
}

// spool web · js/core/format.js
function keyFor(root, name) {
  return (root || '') + '|' + (name || '');
}

function short(text, limit) {
  const line = String(text || '');
  return line.length > limit ? line.slice(0, limit) + '…' : line;
}
// 「这是什么时候探的」必须说得出：探活挪到后台之后，`/state` 给的是**最近一次**
// 结果，可能是几秒前那一份。陈旧值可以接受，**静默的**陈旧值不行——不然一个
// 几分钟前的结论看起来就像刚刚测的。用服务端算好的 `age_s`（两边的墙钟不保证
// 同步，页面拿它换算成"3 秒前"就行），页面只负责给出可读说明。
function probedAgo(health) {
  const info = health || {};
  const age = info.age_s;
  // 既没有 `age_s` 也没有 `probed_at`：**不说**"未探测"——有结果而没时间戳时
  // 那句话会和"状态：正常"打架。照实说时间戳缺失。
  if (age === null || age === undefined) {
    return info.probed_at ? '' : '（时间戳缺失）';
  }
  const seconds = Math.max(0, Math.round(Number(age)));
  if (seconds < 2) { return '（刚刚）'; }
  if (seconds < 60) { return '（' + seconds + ' 秒前）'; }
  return '（' + Math.round(seconds / 60) + ' 分钟前）';
}

// spool web · js/render/markdown.js
function parseInlines(src) {
  const text = String(src || '');
  const out = [];
  // 代码放在最前：`` `a **b**` `` 里的星号和链接不再被拆开。
  const re = /`([^`\n]+)`|\*\*([^*]+)\*\*|\[([^\]\n]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  let match = re.exec(text);
  while (match) {
    if (match.index > last) {
      out.push({type: 'text', text: text.slice(last, match.index)});
    }
    if (match[1] != null) {
      out.push({type: 'code', text: match[1]});
    } else if (match[2] != null) {
      out.push({type: 'strong', text: match[2]});
    } else {
      out.push({type: 'link', text: match[3], href: match[4]});
    }
    last = match.index + match[0].length;
    match = re.exec(text);
  }
  if (last < text.length) { out.push({type: 'text', text: text.slice(last)}); }
  return out;
}

function splitCells(line) {
  const body = String(line || '').trim().replace(/^\|/, '').replace(/\|$/, '');
  return body.split('|').map(function (cell) { return cell.trim(); });
}

function isSeparatorCell(cell) {
  return /^:?-{3,}:?$/.test(String(cell || '').trim());
}

function isBlockStart(line) {
  const trimmed = String(line || '').trim();
  if (/^#{1,6}\s+\S/.test(trimmed)) { return true; }
  if (/^[-*]\s+\S/.test(trimmed)) { return true; }
  if (/^\d+\.\s+\S/.test(trimmed)) { return true; }
  if (/^\|.+\|$/.test(trimmed)) { return true; }
  return false;
}

function hasBlockMarkdown(src) {
  const lines = String(src || '').split('\n');
  for (const raw of lines) {
    if (isBlockStart(raw)) { return true; }
  }
  return false;
}

function hasInlineMarkdown(src) {
  const text = String(src || '');
  return text.indexOf('`') >= 0 || text.indexOf('**') >= 0 || /\[[^\]\n]+\]\([^)\s]+\)/.test(text);
}

function safeHref(url) {
  const value = String(url || '').trim();
  const lower = value.toLowerCase();
  if (!value || lower.indexOf('javascript:') === 0 || lower.indexOf('data:') === 0) {
    return '';
  }
  if (lower.indexOf('//') === 0) { return ''; }
  if (lower.indexOf('http:') === 0 || lower.indexOf('https:') === 0 || lower.indexOf('mailto:') === 0) {
    return value;
  }
  return '';
}

function parseMarkdown(src) {
  const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (trimmed === '') { index += 1; continue; }
    const heading = /^(#{1,6})\s+(\S.*)$/.exec(trimmed);
    if (heading) {
      blocks.push({
        type: 'heading',
        level: heading[1].length,
        inlines: parseInlines(heading[2]),
      });
      index += 1;
      continue;
    }
    if (/^[-*]\s+\S/.test(trimmed)) {
      const items = [];
      while (index < lines.length && /^[-*]\s+\S/.test(lines[index].trim())) {
        items.push(parseInlines(lines[index].trim().replace(/^[-*]\s+/, '')));
        index += 1;
      }
      blocks.push({type: 'ul', items: items});
      continue;
    }
    if (/^\d+\.\s+\S/.test(trimmed)) {
      const items = [];
      while (index < lines.length && /^\d+\.\s+\S/.test(lines[index].trim())) {
        items.push(parseInlines(lines[index].trim().replace(/^\d+\.\s+/, '')));
        index += 1;
      }
      blocks.push({type: 'ol', items: items});
      continue;
    }
    if (/^\|.+\|$/.test(trimmed)) {
      const rows = [];
      while (index < lines.length && /^\|.+\|$/.test(lines[index].trim())) {
        rows.push(splitCells(lines[index]));
        index += 1;
      }
      const kept = rows.filter(function (row) { return !row.every(isSeparatorCell); });
      if (kept.length) {
        const cellsOf = function (row) {
          return row.map(function (cell) { return parseInlines(cell); });
        };
        blocks.push({
          type: 'table',
          header: cellsOf(kept[0]),
          rows: kept.slice(1).map(cellsOf),
        });
      }
      continue;
    }
    const para = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() !== '' && !isBlockStart(lines[index])) {
      para.push(lines[index]);
      index += 1;
    }
    blocks.push({type: 'paragraph', inlines: parseInlines(para.join('\n'))});
  }
  return blocks;
}


// spool web · js/render/failure.js
/* ── 失败要给出可读说明，其中"连不上模型"这一种最常见 ──
   一行原始异常（`LlamaCppError: …`、`ConnectTimeout`、一截回溯）谁也读不出
   下一步该干什么。这里只做**展示层**：把错误文本分类成人能读的四件事——
   哪一步失败、地址是哪个、可能为什么、下一步敲什么命令。

   线索从哪来：① 服务端在 final 事件上**只增**的那一条结构化字段
   `error: {stage, url, kind}`（别的一概没动）；② 没有它时按错误文本里的词
   自己认（老服务端、别的失败路径）。地址还能退回 `/state` 的探活结果。

   页面上的内容一律先过 `redact`：密钥、Bearer 串、KEY=VALUE 不许出现在页面上。 */
const FAIL_KINDS = [
  {kind: 'connect', marks: ['无法连接 llama-server', 'ConnectError', 'ConnectTimeout',
    'Connection refused', '连接被拒绝', '连接失败']},
  {kind: 'slow', marks: ['秒内没返回', 'ReadTimeout', 'Read timed out', 'timed out',
    'timeout', '超时']},
  {kind: 'server', marks: ['服务返回', 'tokenize 返回', 'LlamaCppError', 'HTTP 5']},
];

// 页面上的兜底遮蔽：密钥那几类串一律换成 ***（不碰别的字，原文照旧可读）。
function redact(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/g, '$1***')
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/-]{4,}/gi, '$1***')
    .replace(/((?:api[-_]?key|token|secret|password|passwd|authorization)\s*[=:]\s*)\S+/gi,
      '$1***');
}

// 这条错误是"哪一步"坏了。认不出来就是空串（不当成模型故障，别乱指路）。
function failureKind(text) {
  const line = String(text || '');
  for (const item of FAIL_KINDS) {
    for (const mark of item.marks) {
      if (line.indexOf(mark) >= 0) { return item.kind; }
    }
  }
  return '';
}

function failureCause(kind) {
  if (kind === 'connect') { return '模型服务未启动 / 端口不对 / 被代理拦截'; }
  if (kind === 'slow') { return '本次生成内容过长 / 模型响应慢 / 服务端未响应'; }
  if (kind === 'server') { return '服务端参数不对 / 上下文超限 / 进程刚重启'; }
  return '';
}

function failureNext(kind) {
  if (kind === 'connect') {
    return '`spool service list` 查看本机模型配方是否在运行'
      + '（配方表里那条叫 llama 的话，`spool service start llama` 即可启动它）；'
      + '`spool config` 查看 provider 与 base_url 的来源；启动后重新发送';
  }
  if (kind === 'slow') {
    return '把任务拆小后重发；`spool service list` 查看模型进程是否还在';
  }
  if (kind === 'server') {
    return '查看下面那行原始信息；`spool service list` 查看服务状态';
  }
  return '';
}

// 模型地址：以这一次运行的配置为准（服务端探活接口给的），没有就说"配置里那个"。
function modelAddress(extra) {
  const info = extra || {};
  if (info.url) { return String(info.url); }
  const snap = (typeof store !== 'undefined' && store && store.get)
    ? store.get('lastState') : null;
  const model = ((snap || {}).health || {}).model || {};
  return String(model.target || '');
}

// 从一堆行里挑出**最有信息量**的那一行：报错那行通常带 Error / 连接 / 超时这些词。
function usefulLine(text) {
  const lines = String(text || '').split('\n')
    .map(function (one) { return one.trim(); })
    .filter(function (one) { return one !== ''; });
  if (!lines.length) { return ''; }
  for (const one of lines) {
    if (/Error|错误|连不上|连接|超时|timed out|拒绝|返回/.test(one)) { return one; }
  }
  return lines[lines.length - 1];
}

// 认出来就返回 {head, lines, raw}；认不出来返回 null（调用方走原来的画法）。
function explainFailure(text, extra) {
  const info = extra || {};
  const kind = info.kind || failureKind(text);
  if (kind === '') { return null; }
  const where = modelAddress(info);
  const stage = kind === 'connect' ? '连接模型'
    : kind === 'slow' ? '等模型生成' : '模型服务回话';
  const lines = [
    '模型地址：' + (where === '' ? '未提供，按配置取' : where),
    '可能原因：' + failureCause(kind),
    '下一步：' + failureNext(kind),
  ];
  const raw = redact(usefulLine(text)).slice(0, 300);
  return {
    head: '失败环节：' + stage + '（这一步未通过，后续步骤未执行）',
    lines: lines,
    raw: raw,
  };
}

// 认出来的失败画成一块带左边框的说明（`.sys-line.failure`）：哪一步 / 哪个地址 /
// 可能原因 / 下一步敲什么。`showFailure`（对话流里那块）与消息卡片里那一块共用它，
// 免得同一个失败在两处有两套说法。
function failureBox(parts) {
  const box = document.createElement('div');
  box.className = 'sys-line failure';
  const title = document.createElement('div');
  title.className = 'fail-head';
  title.textContent = parts.head;
  box.appendChild(title);
  for (const item of parts.lines) {
    const line = document.createElement('div');
    line.className = 'fail-line';
    line.textContent = item;
    box.appendChild(line);
  }
  if (parts.raw) {
    const raw = document.createElement('div');
    raw.className = 'fail-raw';
    raw.textContent = '原始信息：' + parts.raw;
    box.appendChild(raw);
  }
  return box;
}

// 失败要给出可读说明，而且**要弹出来**：失败是通知，不该只在对话流里等人自己往下翻。
// 对话流里那一块仍然留着——那是这一趟干过什么的记录。
// 认不出来的错误仍按**原来的画法**画（不假装懂）：
// `head` 就是原来那个词——`final` 是「失败：」、`error` 事件是「错误：」。
function createFailure(ctx) {
  function showFailure(head, text, extra) {
    const parts = explainFailure(text, extra);
    ctx.toasts.notifyFailure(head, text, parts);
    if (parts === null) { ctx.thread.add(head + redact(text), 'bad'); return; }
    const stick = ctx.thread.atBottom();
    const wrap = makeMsg('system');
    wrap.appendChild(failureBox(parts));
    if (stick) { ctx.thread.scrollDown(); }
    ctx.thread.updateToBottom();
  }
  return { showFailure: showFailure };
}

// 失败画进消息卡片（这一轮的回复那张）而不是另起一行系统消息：同一件事在一屏上
// 出现两遍，人会以为是两次失败。气泡让位给这块说明（`.bubble.failed` 去掉了自己的
// 底色与内边距），**占位"正在思考"一个字符都不留**。
function failIntoBubble(bubble, head, text, extra) {
  const parts = explainFailure(text, extra);
  bubble.className = 'bubble failed';
  bubble.textContent = '';
  if (parts === null) {
    const line = document.createElement('div');
    line.className = 'sys-line bad';
    line.textContent = head + redact(text);
    bubble.appendChild(line);
    return;
  }
  bubble.appendChild(failureBox(parts));
}

// spool web · js/render/trace_box.js
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

// spool web · js/render/task_card.js
/* 任务卡：挂在助手回复下面。只读 summary，不画 raw。
   running 第一次出现时展开；其余状态收起。人点过之后，重绑不再改开合。 */
const TASK_STATUS_LABEL = {
  done: '完成',
  verify_failed: '验证未过',
  failed: '失败',
  cancelled: '已取消',
  running: '进行中',
};

function taskStatusClass(status) {
  return Object.prototype.hasOwnProperty.call(TASK_STATUS_LABEL, status) ? status : 'running';
}

function taskCardClass(status, open) {
  return 'task-card task-card-' + taskStatusClass(status) + (open ? ' open' : '');
}

function taskStepCount(task) {
  let max = -1;
  for (const event of task.events || []) {
    const step = Number(event.step);
    if (!Number.isNaN(step) && step > max) { max = step; }
  }
  return max < 0 ? 0 : max + 1;
}

function taskVerifyText(task) {
  let found = '';
  for (const event of task.events || []) {
    if (event.phase !== 'verify') { continue; }
    if (event.status === 'error') { found = '验证未过'; }
    else if (event.status === 'ok') { found = '验证通过'; }
    else { found = '已验证'; }
  }
  if (found) { return found; }
  if (task.status === 'verify_failed') { return '验证未过'; }
  return '未验证';
}

function approvalLabel(status) {
  if (status === 'confirmed') { return '已确认'; }
  if (status === 'rejected') { return '已拒绝'; }
  return '';
}

function approvalWaiting(status) {
  return status !== 'confirmed' && status !== 'rejected';
}

function approvalJump(event) {
  const artifacts = event.artifacts || [];
  for (const item of artifacts) {
    if (item && String(item.type || '') === 'diff') { return '查看改动'; }
  }
  const summary = String(event.summary || '');
  if (summary.indexOf('待确认命令') >= 0) { return '去确认'; }
  if (summary.indexOf('待确认') >= 0) { return '查看改动'; }
  return '去确认';
}

function observeStatus(status) {
  if (status === 'ok') { return '完成'; }
  if (status === 'error') { return '失败'; }
  if (status === 'running') { return '进行中'; }
  return String(status || '');
}

function taskRows(task) {
  const events = (task.events || []).slice().sort(function (left, right) {
    return Number(left.seq || 0) - Number(right.seq || 0);
  });
  const rows = [];
  const byCall = {};
  for (const event of events) {
    const phase = String(event.phase || '');
    if (phase === 'understand') { continue; }
    if (phase === 'approval') {
      const call = String(event.tool_call_id || '');
      const waiting = approvalWaiting(event.status);
      const patch = {
        approval: waiting ? '' : approvalLabel(event.status),
        jump: waiting ? approvalJump(event) : '',
        callId: call,
      };
      if (call && byCall[call]) {
        byCall[call].approval = patch.approval;
        byCall[call].jump = patch.jump;
        byCall[call].callId = call;
        continue;
      }
      rows.push({kind: 'approval', text: patch.approval, jump: patch.jump, callId: call});
      continue;
    }
    if (phase === 'observe') {
      const row = {
        kind: 'observe',
        tool: String(event.tool || ''),
        intent: String(event.intent || ''),
        summary: String(event.summary || ''),
        status: observeStatus(event.status),
        bad: event.status === 'error',
        approval: '',
        jump: '',
        callId: String(event.tool_call_id || ''),
      };
      rows.push(row);
      if (event.tool_call_id) { byCall[String(event.tool_call_id)] = row; }
      continue;
    }
    if (phase === 'retrieval') {
      rows.push({kind: 'retrieval', text: String(event.summary || '')});
    } else if (phase === 'verify') {
      rows.push({kind: 'verify', text: String(event.summary || ''), bad: event.status === 'error'});
    } else if (phase === 'error') {
      rows.push({kind: 'error', text: String(event.summary || ''), bad: true});
    } else if (phase === 'final') {
      rows.push({kind: 'final', text: '结论已生成'});
    }
  }
  return rows;
}

function drawTaskHead(box) {
  const task = box._task || {};
  const head = box._head;
  const goalText = redact(String(task.goal || ''));
  head.replaceChildren();
  head.title = goalText;
  head.setAttribute('title', goalText);
  const mark = h('span', 'task-mark task-chip');
  mark.textContent = TASK_STATUS_LABEL[task.status] || TASK_STATUS_LABEL.running;
  const goal = h('span', 'task-goal');
  goal.textContent = goalText;
  goal.title = goalText;
  goal.setAttribute('title', goalText);
  const meta = h('span', 'task-meta');
  const steps = h('span', 'task-steps task-chip');
  steps.textContent = taskStepCount(task) + ' 步';
  const verify = h('span', 'task-verify task-chip');
  verify.textContent = taskVerifyText(task);
  meta.appendChild(steps);
  meta.appendChild(verify);
  const chevron = h('span', 'task-chevron');
  chevron.setAttribute('aria-hidden', 'true');
  head.appendChild(mark);
  head.appendChild(goal);
  head.appendChild(meta);
  head.appendChild(chevron);
}

function focusAsk(callId) {
  const want = String(callId || '');
  const cards = document.querySelectorAll('.ask-card');
  let hit = null;
  for (const node of cards) {
    const ids = (
      String(node.getAttribute('data-tool-call-id') || '') + ' '
      + String(node.getAttribute('data-tool-call-ids') || '')
    ).split(/\s+/);
    if (want && ids.indexOf(want) >= 0) { hit = node; break; }
  }
  if (!hit && cards.length === 1) { hit = cards[0]; }
  if (!hit) { return; }
  if (typeof hit.scrollIntoView === 'function') {
    hit.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
  const apply = hit.querySelector('.ask-apply');
  if (apply && typeof apply.focus === 'function') { apply.focus(); }
}

function appendJump(line, row) {
  if (!row.jump) { return; }
  const jump = document.createElement('button');
  jump.type = 'button';
  jump.className = 'task-jump';
  jump.textContent = row.jump;
  jump.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    focusAsk(row.callId);
  };
  line.appendChild(jump);
}

function drawTaskBody(box) {
  const task = box._task || {};
  box._body.replaceChildren();
  if (task.currentSession === false) {
    const note = h('div', 'task-other');
    note.textContent = '该任务属于其他会话';
    box._body.appendChild(note);
  }
  for (const row of taskRows(task)) {
    const line = h('div', 'task-row task-' + row.kind + (row.bad ? ' bad' : ''));
    if (row.kind === 'observe') {
      const meta = h('div', 'task-row-line');
      const tool = h('span', 'task-tool');
      tool.textContent = redact(row.tool);
      meta.appendChild(tool);
      if (row.intent) {
        const intent = h('span', 'task-intent');
        intent.textContent = redact(row.intent);
        meta.appendChild(intent);
      }
      const status = h('span', 'task-row-status');
      status.textContent = row.status;
      meta.appendChild(status);
      line.appendChild(meta);
      const summary = h('div', 'task-summary');
      const summaryText = redact(row.summary);
      summary.textContent = summaryText;
      summary.title = summaryText;
      summary.setAttribute('title', summaryText);
      line.appendChild(summary);
      if (row.approval) {
        const approval = h('span', 'task-approval');
        approval.textContent = row.approval;
        line.appendChild(approval);
      }
      if (task.canJump !== false) { appendJump(line, row); }
    } else if (row.kind === 'approval') {
      const showJump = row.jump && task.canJump !== false;
      if (!row.text && !showJump) { continue; }
      if (row.text) { line.appendChild(document.createTextNode(row.text)); }
      if (showJump) { appendJump(line, row); }
    } else {
      line.textContent = redact(row.text);
    }
    box._body.appendChild(line);
  }
}

function taskOpen(box) { return !box._body.classList.contains('hidden'); }

function setTaskOpen(box, open, fromUser) {
  if (fromUser) { box._userSet = true; }
  box._body.classList.toggle('hidden', !open);
  box.classList.toggle('open', open);
  box._head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { drawTaskBody(box); }
  else { box._body.replaceChildren(); }
}

function makeTaskCard(task) {
  const box = h('div', taskCardClass(task.status, false));
  box.setAttribute('data-task-id', String(task.task_id || ''));
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'task-head';
  head.setAttribute('aria-expanded', 'false');
  const body = h('div', 'task-body hidden');
  head.onclick = function () { setTaskOpen(box, !taskOpen(box), true); };
  box.appendChild(head);
  box.appendChild(body);
  box._head = head;
  box._body = body;
  box._task = task;
  box._userSet = false;
  drawTaskHead(box);
  if (taskStatusClass(task.status) === 'running') { setTaskOpen(box, true, false); }
  return box;
}

function syncTaskCards(wrap, tasks) {
  if (!wrap) { return; }
  let host = wrap.querySelector('.task-list');
  if (!host) {
    host = h('div', 'task-list');
    wrap.appendChild(host);
  }
  for (const task of tasks || []) {
    const id = String(task.task_id || '');
    let card = null;
    for (const node of host.querySelectorAll('.task-card')) {
      if (node.getAttribute('data-task-id') === id) { card = node; break; }
    }
    if (!card) {
      host.appendChild(makeTaskCard(task));
      continue;
    }
    card._task = task;
    card.className = taskCardClass(task.status, taskOpen(card));
    drawTaskHead(card);
    if (taskOpen(card)) { drawTaskBody(card); }
  }
}

// spool web · js/render/diff.js
// diff 摘要：几行加、几行减。看正文之前先给一个"改了多少"。
// `diffSummary` 不碰 DOM，节点测试直接引这个文件。`diffBox` 仍用 document 装节点。
function diffSummary(text) {
  let adds = 0, dels = 0;
  for (const raw of String(text || '').split('\n')) {
    if (raw.startsWith('+') && !raw.startsWith('+++')) { adds += 1; }
    if (raw.startsWith('-') && !raw.startsWith('---')) { dels += 1; }
  }
  return '+' + adds + ' -' + dels + ' 行';
}

const DIFF_FOLD_AT = 40;

function diffBox(path, text) {
  const lines = String(text || '').split('\n');
  const open = lines.length <= DIFF_FOLD_AT;
  const box = document.createElement('div');
  box.className = 'diff-file' + (open ? ' open' : '');
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'diff-file-head';
  head.setAttribute('aria-expanded', open ? 'true' : 'false');
  head.textContent = path + '  ' + diffSummary(text);
  const body = document.createElement('div');
  body.className = 'diff-file-body';
  const sheet = document.createElement('div');
  sheet.className = 'diff';
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index];
    const line = document.createElement('div');
    line.className = raw.startsWith('+') ? 'add'
      : raw.startsWith('-') ? 'del' : 'dim';
    line.textContent = raw;
    sheet.appendChild(line);
  }
  body.appendChild(sheet);
  head.onclick = function () {
    const next = head.getAttribute('aria-expanded') !== 'true';
    head.setAttribute('aria-expanded', next ? 'true' : 'false');
    box.classList.toggle('open', next);
  };
  box.appendChild(head);
  box.appendChild(body);
  return box;
}

// spool web · js/render/code.js
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

// spool web · js/features/toasts.js
// 只有 `info` 那一档的提示自己会走（几秒）；错误那些**永远不自动消失**
// ——在用户看到之前消失，比不提示更糟。
const AUTO_CLOSE_MS = 5000;
const TOAST_CAP = 3;

/* ── 通知：右上角的浮动气泡（可关、可堆叠） ──
   为什么不是横幅：横幅那条带子是"这一页现在是什么"，通知是"刚刚发生了什么"。
   混在一起时，一条早就没意义的旧提示会把新错误挤下去，而它自己不会走。

   三条性质：
   ① 每条一个关闭键，两条以上时多一个「全部关闭」；
   ② 同一个 key 的同一份内容只**更新**不重建——`/state` 是 3 秒一拍，
      重建就是每 3 秒抖一次；人手动关掉的那一份内容不再自己弹回来
      （内容真变了才回：错误换了、那条提示消失了又回来）；
   ③ 它**脱离文档流**：`position:fixed` 在右上（CSS），节点挂在 `#toasts` 上——
      那个容器是 `body` 的直接子节点，不在 `#stream` / `#thread` 里。所以弹一条通知
      既不新增对话流的节点，也不改主滚动容器的滚动位置（"把页面挤下去"就是这么坏的）。 */
function createToasts(ctx) {
const toastNodes = {};
const toastSigs = {};
const toastOrder = [];
const toastDismissed = {};

// 通知栈的外观：一条都没有时整块不存在；两条以上才给「全部关闭」。
function syncToastChrome() {
  toastsBox.classList.toggle('show', toastOrder.length > 0);
  toastAllBtn.classList.toggle('show', toastOrder.length > 1);
}

// 关掉一条（返回是不是真关掉了一条）。
function closeToast(key) {
  const node = toastNodes[key];
  if (!node) { return false; }
  if (node._timer) { clearTimeout(node._timer); node._timer = 0; }
  if (node.parentNode) { node.parentNode.removeChild(node); }
  delete toastNodes[key];
  delete toastSigs[key];
  const at = toastOrder.indexOf(key);
  if (at >= 0) { toastOrder.splice(at, 1); }
  syncToastChrome();
  return true;
}

// 人点的关闭键：记下"这一份内容人不要了"，下一拍轮询别把它又放回来。
function dismissToast(key) {
  toastDismissed[key] = toastSigs[key] || '';
  closeToast(key);
}

function closeAllToasts() {
  for (const key of toastOrder.slice()) { closeToast(key); }
}

// 往一个盒子里填若干行：只在**行数变多**时建节点，行数变少时删掉多的，
// 内容没变的那几行一个字都不动（同"数据没变就一个节点都不重建"那条既有取舍）。
function fillToastLines(box, lines) {
  for (let index = 0; index < lines.length; index += 1) {
    let line = box.children[index];
    if (!line) {
      line = document.createElement('div');
      line.className = 'toast-line';
      box.appendChild(line);
    }
    if (line.textContent !== lines[index]) { line.textContent = lines[index]; }
  }
  while (box.children.length > lines.length) { box.removeChild(box.lastChild); }
}

// 建 / 更新一条通知。`spec`：{key, kind, title, lines, raw}
//   kind: 'info'（自己消失）/ 'warn' / 'error'（后两档只能手动关）
// 气泡只讲"刚刚发生了什么"（失败、做成了的回执）。"这一页现在什么状态"
// （探活 / 模式 / 配对 / 别处在跑）走状态区，所以这里没有"这一拍没出现就收掉"的活。
function showToast(spec) {
  const key = String(spec.key);
  const lines = spec.lines || [];
  const sig = JSON.stringify([spec.kind, spec.title, lines, spec.raw || '']);
  const have = toastNodes[key];
  if (have && toastSigs[key] === sig) { return have; }
  if (!have && toastDismissed[key] === sig) {
    // 人刚关掉的就是这一份：状态类通知（轮询一遍遍送同一份）不再弹。
    // 但他**刚点出来**的一次性提示要照常出现——那是他自己那个动作的回执。
    if (spec.kind !== 'info') { return null; }
    delete toastDismissed[key];
  }
  let node = have;
  if (!node) {
    node = document.createElement('div');
    node.dataset.key = key;
    const body = document.createElement('div');
    body.className = 'toast-body';
    const title = document.createElement('div');
    title.className = 'toast-title';
    const linesBox = document.createElement('div');
    linesBox.className = 'toast-lines';
    const raw = document.createElement('div');
    raw.className = 'toast-raw';
    body.appendChild(title);
    body.appendChild(linesBox);
    body.appendChild(raw);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.title = '关掉这条通知';
    // 按钮里只有一个图标，读屏得有个名字（只给 title 是不够稳的）。
    close.setAttribute('aria-label', '关掉这条通知');
    close.innerHTML = ICON.close;
    close.onclick = (function (which) {
      return function () { dismissToast(which); };
    })(key);
    node.appendChild(body);
    node.appendChild(close);
    node._parts = {title: title, lines: linesBox, raw: raw};
    toastNodes[key] = node;
    toastOrder.unshift(key);
    const firstToast = toastsBox.querySelector('.toast');
    if (firstToast) { toastsBox.insertBefore(node, firstToast); }
    else { toastsBox.appendChild(node); }
    while (toastOrder.length > TOAST_CAP) { closeToast(toastOrder[toastOrder.length - 1]); }
  }
  if (node._kind !== spec.kind) {
    node._kind = spec.kind;
    node.className = 'toast ' + spec.kind;
    if (spec.kind === 'error' || spec.kind === 'warn') { node.setAttribute('role', 'alert'); }
    else { node.setAttribute('role', 'status'); }
  }
  node._parts.title.textContent = spec.title;
  fillToastLines(node._parts.lines, lines);
  node._parts.raw.textContent = spec.raw || '';
  node._parts.raw.style.display = spec.raw ? 'block' : 'none';
  toastSigs[key] = sig;
  if (node._timer) { clearTimeout(node._timer); node._timer = 0; }
  // 只有 `info` 自己消失。错误那一档一律留到人点关闭为止。
  if (spec.kind === 'info') {
    node._timer = setTimeout(function () { closeToast(key); }, AUTO_CLOSE_MS);
  }
  syncToastChrome();
  return node;
}

// 一次性提示：操作做成了这类回执，自己会走，不必人去关。
function notifyInfo(title, lines) {
  return showToast({key: 'info|' + title, kind: 'info',
    title: title, lines: lines || []});
}

// 失败通知：右上角弹出来，**只能手动关**。四件事（哪一步 / 地址 / 可能原因 /
// 下一步敲什么）由 `explainFailure` 给，这里只负责把它摆进气泡——不另写一套。
function notifyFailure(head, text, parts) {
  const title = parts === null ? head + redact(text) : parts.head;
  return showToast({
    key: 'failure|' + head + '|' + redact(usefulLine(text)).slice(0, 120),
    kind: 'error', title: title,
    lines: parts === null ? [] : parts.lines,
    raw: parts === null ? '' : parts.raw,
  });
}

  return { syncToastChrome: syncToastChrome, closeToast: closeToast, dismissToast: dismissToast, closeAllToasts: closeAllToasts, fillToastLines: fillToastLines, showToast: showToast, notifyInfo: notifyInfo, notifyFailure: notifyFailure };
}

// spool web · js/features/status.js
/* ══ 状态区：输入条下面那一行 ══
   这一页"现在什么状态"只有这一个落点，所以顶部可以是干净的：
   ① **探活**：常态只剩一枚小圆点（`healthView.label` 为空）；不通 / 回话慢 / 还没探过
      才变色并写出几个字。异常**带字**是刻意的——一枚沉默的绿点会把"模型连不上"
      藏起来，那正是"静默的陈旧值比报错更糟"那条教训。
   ② **模式**：只读（非回环）与 `--root` 钉住才摆出一枚锁徽标；它回答"为什么点不动"。
   ③ **待配对 / 别处还有一条在跑**：收进同一行；配对码直接写在徽标上（一次点击就能看到，
      不会被划走），详情里仍然有完整的那条 `spool approve <码>` 命令。
   徽标与"Enter 发送"那句共用**同一行**（`.st`，本来就在），所以状态怎么变都不新增占位。
   点徽标展开 `#st-panel`（绝对定位，浮在输入条上方，同样不占文档流）；再点、点别处、
   按 Esc 都收起。 */

// 每个徽标里的节点（圆点 / 锁图标 / 标签）现建：HTML 里只摆了空按钮。
// 为什么不用 `querySelector` 去取静态 HTML 里写好的那几个 span——假 DOM（测试台）
// 只按 id 造空壳，不解析静态子节点；靠它取就会在测试里拿到 null。
function badgeParts(box, withDot, withLock) {
  if (withDot) {
    const dot = document.createElement('span');
    dot.className = 'st-dot';
    box.appendChild(dot);
  }
  if (withLock) {
    const icon = document.createElement('span');
    icon.className = 'st-icon';
    icon.innerHTML = ICON.lock;
    box.appendChild(icon);
  }
  const label = document.createElement('span');
  label.className = 'st-label';
  box.appendChild(label);
  return label;
}

const healthLabel = badgeParts(stHealth, true, false);
const modeLabel = badgeParts(stMode, false, true);
const targetLabelBox = badgeParts(stTarget, false, false);
const pairLabel = badgeParts(stPair, false, false);
const elsewhereLabel = badgeParts(stElsewhere, false, false);

// 徽标上的短标签：只读 / 钉住同时成立时说全（"--root 起的只读壳"是可能的）。
function createStatus(ctx) {
function modeBadgeText() {
  const parts = [];
  if (ctx.store.get('readonly')) { parts.push('只读'); }
  if (ctx.store.get('pinned')) { parts.push('目标已固定'); }
  return parts.join(' · ');
}

// 顶栏状态入口只说三档。详情里的超时、未探测、外接服务仍留在面板。
function statusEntryWord() {
  const view = ctx.store.get('healthView') || {};
  if (view.cls === 'ok') { return '正常'; }
  if (view.label === '模型未连接' || view.label === '未探测' || view.cls === 'warn') {
    return '未连接';
  }
  return '异常';
}

function approvalCount() {
  const groups = ((ctx.store.get('lastState') || {}).task_groups) || [];
  let waiting = 0;
  for (let index = 0; index < groups.length; index += 1) {
    const events = groups[index].events || [];
    for (let eventIndex = 0; eventIndex < events.length; eventIndex += 1) {
      const event = events[eventIndex];
      if (String(event.phase || '') === 'approval' && approvalWaiting(event.status)) {
        waiting += 1;
        break;
      }
    }
  }
  return waiting;
}

// 探活那一节：**状态 / 地址 / 探活时间 / 可能原因 / 下一步**（下一步是真存在的命令）。
// "什么时候探的"由 `probedStamp` 给：探活挪到后台之后 `/state` 给的是最近一次结果，
// 陈旧值可以接受，**静默的**陈旧值不行。
function healthStateText(info, model) {
  if (model.ok === false) {
    const kind = model.kind || failureKind(model.detail);
    if (kind === 'slow') { return '连接超时（对方未在超时内回话）'; }
    if (kind === 'server') { return '服务异常（回话是错误）'; }
    return '未连接（连接被拒）';
  }
  if (info.state === 'unknown' || model.ok === null || model.ok === undefined) {
    return '未知（后台正在探测）';
  }
  return '正常';
}

function healthLines() {
  const info = ctx.store.get('healthInfo') || {};
  const model = info.model || {};
  const rows = [{text: '状态：' + healthStateText(info, model),
    cls: ctx.store.get('healthView').cls === 'bad' ? 'bad' : ''}];
  // 地址：优先用这一次探活报的地址，其次配置里那个。
  rows.push({text: '模型服务地址：' + (model.target || info.url || '未提供，按配置取')});
  rows.push({text: '最近一次探测：' + probedStamp(info)});
  for (const one of ctx.store.get('healthView').lines) { rows.push({text: one}); }
  return rows;
}

// 模式那一节：这一页现在能不能驱动、目标为什么切不动——"为什么点不动"的答案。
function modeLines() {
  const rows = [{text: '驱动：' + (ctx.store.get('readonly')
    ? '不可用（这一页只读，写请求会被服务端拒）'
    : '可用（本机回环）')}];
  if (ctx.store.get('readonly')) { rows.push({text: '只读'}); }
  if (ctx.store.get('readOnlyNote')) { rows.push({text: ctx.store.get('readOnlyNote')}); }
  if (ctx.store.get('pinned')) { rows.push({text: '目标已固定'}); }
  if (ctx.store.get('pinnedNote')) { rows.push({text: ctx.store.get('pinnedNote')}); }
  if (!ctx.store.get('readOnlyNote') && !ctx.store.get('pinnedNote')) {
    rows.push({text: '目标未固定：在左栏点一行即可切换。'});
  }
  const waiting = approvalCount();
  if (waiting) { rows.push({text: '待审批：' + waiting}); }
  return rows;
}

// 目标那一节：我在哪儿 + 空态怎么选 + 现在是不是正在跑（发送键为什么是灰的）。
function targetLines() {
  const t = (ctx.store.get('lastState') || {}).target || {};
  const title = String(t.session_title || '').trim();
  const session = t.session || '';
  const who = title
    ? title + (session && session !== title ? '（' + session + '）' : '')
    : session;
  const rows = [{text: t.root
    ? '目标：' + t.root + ' · 会话 ' + who
    : '目标：未选择'}];
  rows.push({text: '运行状态：' + (ctx.store.get('busy') ? '运行中' : '空闲')});
  for (const one of ctx.store.get('targetNotes')) { rows.push({text: one}); }
  return rows;
}

// 配对那一节：码必须**看得见**（人要去终端敲它），所以整条命令都写出来。
function pairSections() {
  const sections = [];
  for (const item of ctx.store.get('pairingRows')) {
    sections.push({
      title: '待批准的通道配对请求（来自手机 / 聊天通道）',
      lines: [
        {text: '配对码：' + item.code},
        {text: '在本机终端执行：spool approve ' + item.code},
        {text: '来自：' + (item.user || '未知')},
        {text: '「应用」不批准配对（那是落盘确认，批准在终端）。'},
      ],
    });
  }
  return sections;
}

// 别处那几条：标题就是"别处还有一条在跑：<工作区>"（`showElsewhere` 里拼好的）。
function elsewhereSections() {
  return ctx.store.get('elsewhereRows').map(function (one) {
    return {title: one.title, lines: one.lines.map(function (text) {
      return {text: text};
    })};
  });
}

function statusSection(title, lines, raw) {
  const box = document.createElement('div');
  box.className = 'st-sec';
  const head = document.createElement('div');
  head.className = 'st-sec-head';
  head.textContent = title;
  box.appendChild(head);
  for (const one of (lines || [])) {
    const line = document.createElement('div');
    line.className = 'st-line' + (one.cls ? ' ' + one.cls : '');
    line.textContent = one.text;
    box.appendChild(line);
  }
  if (raw) {
    const node = document.createElement('div');
    node.className = 'st-raw';
    node.textContent = '原始信息：' + raw;
    box.appendChild(node);
  }
  return box;
}

// 面板内容 = 探活 + 模式 + 目标 + 别处 + 配对。关着的时候一个节点都不建。
function renderStatusPanel() {
  if (!ctx.store.get('statusOpen')) { return; }
  const nodes = [
    statusSection('模型连接', healthLines(), ctx.store.get('healthView').raw),
    statusSection('运行模式', modeLines(), ''),
    statusSection('当前目标', targetLines(), ''),
  ];
  for (const one of elsewhereSections()) {
    nodes.push(statusSection(one.title, one.lines, ''));
  }
  for (const one of pairSections()) {
    nodes.push(statusSection(one.title, one.lines, ''));
  }
  const close = stClose;
  // 关闭键必须是面板的第一个子节点：Tab 顺序跟 DOM，不靠 `order` 把视觉和焦点拆开。
  if (close) { stPanel.replaceChildren(close, ...nodes); }
  else { stPanel.replaceChildren(...nodes); }
}

if (stClose) {
  stClose.innerHTML = ICON.close;
  stClose.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    setStatusOpen(false);
  };
}

function setStatusOpen(open) {
  ctx.store.set('statusOpen', Boolean(open));
  stPanel.classList.toggle('hidden', !ctx.store.get('statusOpen'));
  for (const badge of [stHealth, stMode, stTarget, stPair, stElsewhere]) {
    badge.setAttribute('aria-expanded', ctx.store.get('statusOpen') ? 'true' : 'false');
  }
  if (ctx.store.get('statusOpen')) { renderStatusPanel(); }
}

// 徽标 + 面板一次画出来。状态每次都从**最近一帧**重算，所以"跑完了还写着正在跑"
// 这类陈旧话不会留在上面；节点只在面板开着时才重建（关着就只改类与文字）。
function drawStatus() {
  const word = statusEntryWord();
  stHealth.className = 'st-item ' + (word === '正常' ? 'ok' : (word === '未连接' ? 'warn' : 'bad'));
  healthLabel.classList.remove('hidden');
  healthLabel.textContent = word;
  stHealth.title = '模型连接：' + word;
  stHealth.setAttribute('aria-label', stHealth.title);

  // 固定、只读、待配对、其他会话都进面板，不再各占一枚浮块。
  modeLabel.textContent = '';
  targetLabelBox.textContent = '';
  pairLabel.textContent = '';
  elsewhereLabel.textContent = '';
  for (const badge of [stMode, stTarget, stPair, stElsewhere]) {
    badge.classList.add('hidden');
    badge.title = '';
    badge.setAttribute('aria-label', '');
  }

  renderStatusPanel();
}


// 「别处还有一条在跑」：切走不杀任务（设计 §3.4），页面就必须**说得出**这件事，
// 不能让它看起来只有一条。这一条不走事件流——别的目标上起来的任务，在本目标的
// 事件流上什么都没有，只能按 `/state` 的 `running_targets` 轮出来。
// 它是"当前值"型的状态，不是"刚刚发生了什么"的事件：所以收进状态区那一行
// 的一枚徽标（点开是哪几条），而不是一条要人自己关、又一直挂着的气泡。
function showElsewhere(state) {
  const others = (state.running_targets || []).filter(function (row) {
    return !row.current;
  });
  ctx.store.set('elsewhereRows', others.map(function (row) {
    const lines = ['会话 ' + row.session + ' — ' + short(row.goal, 60)];
    if (row.awaiting) { lines.push('该会话正在等待确认'); }
    return {title: '其他会话运行中：' + (row.name || row.root), lines: lines};
  }));
  drawStatus();
}

// 重绑这一屏：目标换了，旧的输出、待确认卡片、历史进度都属于**上一个**目标。
// 配对码展示用。批准发生在另一个进程（本机敲 spool approve 加码），
// 所以还要短轮询 /state，这一枚徽标才会自己消失。
// 不能复用「应用/拒绝」：点应用会走 /confirm 去落盘改动。
// 它现在是输入条那一行里的一枚徽标（+ 点开后的详情），不是一次性气泡：
// 配对码要被抄进终端，划走就没了；而一条常驻的气泡又正是"顶部不干净"的来源。
function showPairings(items) {
  ctx.store.set('pairingRows', (items || []).slice());
  drawStatus();
}


// 探活不通那一行的开头。`slow` 是连上了、只是没在超时内回话，它对人的下一步
// 与别的不通不同：`slow` 要去看模型是不是在加载，别的不通要去把服务起起来。
// 所以不合成一句"未连接"：两句话都由 `failureCause` / `failureNext` 往下说。
function modelDownLine(kind, where, ago) {
  if (kind === 'slow') { return '本机模型连接超时' + where + ago; }
  return '本机模型未连接' + where + ago;
}

// 探活那枚徽标的文字与详情都由这里算。异常**带字**（不是一枚沉默的绿点）：
// 四件事（哪一步 / 哪个地址 / 可能为什么 / 下一步敲什么）里的后三件由 `explainFailure`
// 给（与对话流里那一块**同一套**说法，不另写一套），分类优先用服务端**只增**的 `kind`；
// "什么时候探的"由 `probedStamp` / `probedAgo` 补上——探活挪到后台之后 `/state` 给的
// 是最近一次结果，**静默的**陈旧值等于假话。
function probedStamp(info) {
  const data = info || {};
  const ago = probedAgo(data);
  const at = Number(data.probed_at);
  if (!at) { return ago; }
  return (new Date(at * 1000).toLocaleTimeString('zh-CN', {hour12: false}) + ' ' + ago).trim();
}

function showHealth(health) {
  const info = health || {};
  ctx.store.set('healthInfo', info);
  const model = info.model || {};
  // 常态：`label` 为空 = 只剩一枚小圆点。**不是**画一句"一切正常"。
  ctx.store.set('healthView', {cls: 'ok', label: '', lines: [], raw: ''});
  if (model.ok === false) {
    const where = model.target ? '（' + model.target + '）' : '';
    const kind = model.kind || failureKind(model.detail) || 'connect';
    const parts = explainFailure(model.detail, {kind: kind, url: model.target});
    // `explainFailure` 那句"模型地址"与上面 `healthLines` 的"地址："是同一件事，
    // 去掉它免得详情里同一句话出现两遍。
    const lines = (parts && parts.lines ? parts.lines : []).filter(function (one) {
      return one.indexOf('模型地址：') !== 0;
    });
    if (kind === 'slow' && lines.length) {
      // 探活超时这一档没有人在等生成，所以下一步不是让用户把活拆小重发，
      // 而是查看模型进程是否还在。
      lines[lines.length - 1] = '下一步：`spool service list` 查看模型进程是否还在；'
        + '刚启动或正在加载模型时，等它加载完成即可';
    }
    ctx.store.set('healthView', {
      cls: 'bad',
      label: kind === 'slow' ? '模型连接超时' : (kind === 'server' ? '模型服务异常' : '模型未连接'),
      lines: [modelDownLine(kind, where, probedAgo(info))].concat(lines),
      raw: parts && parts.raw ? parts.raw : redact(model.detail || ''),
    });
  } else if (info.state === 'unknown') {
    // 后台探活刚起来：说"未探测"，**不画成"通了"**（那是静默的假话），
    // 也不画成"未连接"（那是另一种假话）。
    ctx.store.set('healthView', {
      cls: 'warn', label: '未探测',
      lines: ['本机模型：未探测（后台正在探测，几秒后此处自动更新）。'],
      raw: '',
    });
  }
  // 外接配方（llama / 隧道）与通道桥也在这一节里，不另开一枚徽标：
  // 它们回答的是同一件事——"模型这一步现在通不通"。
  const extra = [];
  for (const item of (info.services || [])) {
    if (item.ok === false) {
      extra.push('外接服务未连接：' + (item.name || '未具名')
        + (item.target ? '（' + item.target + '）' : '')
        + (item.detail ? '：' + redact(item.detail) : ''));
    }
  }
  const bridge = info.bridge || {};
  if (bridge.name && bridge.ok === false) {
    extra.push('通道已退出' + (bridge.target ? '（' + bridge.target + '）' : ''));
  }
  if (extra.length) {
    ctx.store.update('healthView', function (view) {
      const next = {
        cls: view.cls, label: view.label,
        lines: (view.lines || []).slice(), raw: view.raw,
      };
      if (next.cls === 'ok') {
        next.cls = 'bad';
        next.label = '外接服务未连接';
      }
      next.lines = next.lines.concat(extra);
      return next;
    });
  }
  drawStatus();
}

// 短轮询 /state。这几样都**不一定伴随事件**：配对码是另一个进程在批，
// 而「别处还有一条在跑」说的是别的目标上的任务——它在当前目标的事件流上
// 一个字都不会出现，只轮不推的话状态区永远不会亮。
//
// 这一拍还兜一件事：**"正在停止…"不许卡住**。停止的结局靠 `final` 事件推过来，
// 而那一条正好可能因为断线漏掉（`/events` 断过、或者人点了停止就切走了）。
// 所以这一拍发现服务端说"不在跑"时，把那一枚键放回"发送"。
async function refreshStatus() {
  try {
    const response = await fetch('/state');
    if (!response.ok) { return; }
    const state = await response.json();
    showReadonly(state);
    // 只把"在不在跑 / 停没停"这两个标量并进最近一帧：**不 applyState**——
    // 那一趟会把列表、对话流、卡片全重画一遍，而这一拍只该碰状态区
    // （`test_page_dom.py::test_三秒轮询只碰状态区` 钉着这件事）。
    ctx.store.update('lastState', function (snap) {
      if (!snap || typeof snap !== 'object') { return snap; }
      const next = Object.assign({}, snap);
      next.running = Boolean(state.running);
      next.stopped = Boolean(state.stopped);
      return next;
    });
    if (!state.running) {
      // 这一轮已经结束了（而 `final` 事件可能因为断线漏掉）：那一枚键放回"发送"，
      // 顺便把"这一轮是停的"那个标记清掉——服务端说不是停的就该清，否则下一轮的
      // 一次普通失败会被当成"已停止"画出来。
      if (!state.stopped) { ctx.store.set('stoppedRun', false); }
      if (ctx.store.get('busy')) { ctx.composer.setRunning(false); }
    }
    showPairings(state.pairings);
    showHealth(state.health);
    showElsewhere(state);
  } catch (err) { /* 轮询失败就等下一拍，状态停在上一帧 */ }
}

// 「它想跑一条白名单外的命令」——把被问的内容摆进卡片：要执行什么、为什么。
// 授权和落盘是两件事：一个是问"能不能跑"，一个是问"要不要写入"，
// 混成一句「有 1 处改动待确认」，人根本不知道自己在批什么。
function showCommandAsk(detail) {
  const sig = JSON.stringify(['command', detail.command, detail.reason || '']);
  if (ctx.store.get('card') && ctx.store.get('cardAsking') && ctx.store.get('cardSig') === sig) { return; }
  const node = ctx.confirm.ensureCard();
  ctx.store.set('cardSig', sig);
  const stick = ctx.thread.atBottom();
  node.className = 'ask-card command';
  node.dataset.kind = 'command';
  node.dataset.state = 'asking';
  ctx.confirm.setAskLabels(true);
  node._what = '命令 ' + detail.command;
  node._who.textContent = ctx.confirm.targetLabel();
  node._kind.textContent = '命令授权待确认';
  node._body.replaceChildren();
  const title = document.createElement('div');
  title.className = 'ask-title';
  const mark = document.createElement('span');
  mark.className = 'ask-mark';
  mark.textContent = '⚠';
  title.appendChild(mark);
  title.appendChild(document.createTextNode(' 要执行一条命令'));
  const pre = document.createElement('pre');
  pre.className = 'ask-cmd';
  const code = document.createElement('code');
  code.textContent = String(detail.command || '');
  pre.appendChild(code);
  const why = document.createElement('div');
  why.className = 'ask-reason';
  why.textContent = '原因：' + (detail.reason || '未说明');
  const how = document.createElement('div');
  how.className = 'ask-warn';
  how.textContent = '仅允许这一次；长期允许需写入 .agent/grants.json';
  node._body.appendChild(title);
  node._body.appendChild(pre);
  node._body.appendChild(why);
  node._body.appendChild(how);
  if (node._foot) { node._foot.style.display = 'flex'; }
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

// 卡片上那两个按钮的可用态。只读壳里**连按钮都没建**（见 ensureCard），
// 所以这里第一句就是只读早退——那是既有契约：点它必被 403 拒。
function showActions() {
  if (ctx.store.get('readonly')) { return; }
  if (!ctx.store.get('card') || !ctx.store.get('cardAsking') || !ctx.store.get('card')._foot) { return; }
  ctx.store.set('decided', false);
  ctx.store.get('card')._foot.style.display = 'flex';
  if (ctx.store.get('cardApply')) { ctx.store.get('cardApply').disabled = false; }
  if (ctx.store.get('cardReject')) { ctx.store.get('cardReject').disabled = false; }
}

// 非回环绑定 = 只读（设计 §3.7）。这段话说全三件事：为什么、能看什么、
// 想驱动怎么办——只说"403"会让人以为壳坏了。它摆在输入条那行的**锁徽标**里
// （悬停看得见标题、点开是全文）：这一页为什么点不动，仍然是可发现的。
function showReadonly(state) {
  ctx.store.set('readonly', Boolean(state.readonly));
  const where = state.host ? '（绑在 ' + state.host + ' 上）' : '';
  ctx.store.set('readOnlyNote', ctx.store.get('readonly')
    ? '这条壳绑在非回环地址上' + where + '：只能看，不能驱动——'
      + '发任务与确认改动会被拒。要驱动它，就在本机开一个网页壳'
      + '（或把 --host 绑回 127.0.0.1）。工作区与会话仍可切换：那只是换"在看谁"。'
    : '');
  drawStatus();
}


  ctx.store.subscribe(function (keys) {
    if (keys.indexOf('running') >= 0 && ctx.store.get('statusOpen')) {
      renderStatusPanel();
    }
  });
  return { modeBadgeText: modeBadgeText, healthStateText: healthStateText, healthLines: healthLines, modeLines: modeLines, targetLines: targetLines, pairSections: pairSections, elsewhereSections: elsewhereSections, statusSection: statusSection, renderStatusPanel: renderStatusPanel, setStatusOpen: setStatusOpen, drawStatus: drawStatus, showElsewhere: showElsewhere, showPairings: showPairings, modelDownLine: modelDownLine, probedStamp: probedStamp, showHealth: showHealth, refreshStatus: refreshStatus, showCommandAsk: showCommandAsk, showActions: showActions, showReadonly: showReadonly };
}

// spool web · js/features/settings.js
// 归档入口只露一枚数量，不把归档行摊在左栏；行本身只在设置弹层打开时才建。
function createSettings(ctx) {
function updateArchiveBadge(rows) {
  const count = (rows || []).length;
  if (archivedCount) {
    archivedCount.textContent = String(count);
    archivedCount.classList.toggle("hidden", count === 0);
  }
  if (archivedCountInline) { archivedCountInline.textContent = String(count); }
}

function renderArchived() {
  if (!ctx.store.get('settingsOpen') || !archivedList) { return; }
  const rows = (ctx.store.get('lastState') && ctx.store.get('lastState').sessions_removed) || [];
  updateArchiveBadge(rows);
  archivedList.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "archive-empty";
    empty.textContent = "没有已归档的会话。";
    archivedList.appendChild(empty);
    return;
  }
  for (const one of rows) {
    const row = document.createElement("div");
    row.className = "archive-row";
    const main = document.createElement("div");
    main.className = "archive-main";
    const title = document.createElement("div");
    title.className = "archive-title";
    title.textContent = ctx.sessions.sessionRowTitle(one);
    title.title = title.textContent;
    const sid = document.createElement("div");
    sid.className = "archive-id";
    sid.textContent = String(one.id || "");
    main.appendChild(title);
    main.appendChild(sid);
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "arc-restore";
    restore.textContent = '恢复';
    restore.setAttribute("data-focusable", "true");
    restore.disabled = ctx.store.get('readonly');
    restore.onclick = (function (value) {
      return function () { ctx.sessions.requestRestore(value); };
    })(one.id);
    row.appendChild(main);
    row.appendChild(restore);
    archivedList.appendChild(row);
  }
}

function settingsFocusables() {
  if (!settingsBox) { return []; }
  const nodes = [settingsClose].concat(
    settingsBox.querySelectorAll ? settingsBox.querySelectorAll(".arc-restore") : []
  );
  return nodes.filter(function (node) {
    return node && !node.disabled && typeof node.focus === "function";
  });
}

function setSettingsOpen(open) {
  if (!settingsBox) { return; }
  const want = Boolean(open);
  if (want === ctx.store.get('settingsOpen')) { return; }
  ctx.store.set('settingsOpen', want);
  settingsBox.classList.toggle("hidden", !ctx.store.get('settingsOpen'));
  settingsBox.setAttribute("aria-hidden", ctx.store.get('settingsOpen') ? "false" : "true");
  if (settingsBtn) {
    settingsBtn.setAttribute("aria-expanded", ctx.store.get('settingsOpen') ? "true" : "false");
  }
  if (ctx.store.get('settingsOpen')) {
    ctx.store.set('lastSettingsFocus', document.activeElement || null);
    renderArchived();
    if (settingsClose && typeof settingsClose.focus === "function") {
      settingsClose.focus();
    }
  } else {
    const back = ctx.store.get('lastSettingsFocus');
    ctx.store.set('lastSettingsFocus', null);
    if (back && typeof back.focus === "function") { back.focus(); }
  }
}

  return { updateArchiveBadge: updateArchiveBadge, renderArchived: renderArchived, settingsFocusables: settingsFocusables, setSettingsOpen: setSettingsOpen };
}

// spool web · js/features/sessions.js
// 当前会话在页面上最该被读到的名字：Agent 标题优先，没有才退回 id。
function createSessions(ctx) {
  const titleAsked = {};
  const titleTries = {};
function sessionTitle() {
  const t = (ctx.store.get('lastState') || {}).target || {};
  return String(t.session_title || "").trim() || String(t.session || ctx.store.get('session') || "");
}

// 行里显示的名字：没标题时明说"未命名"，不要把一串 uuid 当成名字。
function sessionRowTitle(one) {
  return String((one && one.title) || "").trim() || "未命名会话";
}

// 一轮结束后请 Agent 给当前会话起个名。只在标题为空或仍是种子标题时做。
// final / state 之后先空出一小段：用户往往马上发下一轮，这段里如果已经在跑
// 就放弃本次，也不把这条会话记成"已经问过"。失败保留种子标题，不弹错、不挡输入。
const TITLE_IDLE_MS = 1300;
const titleTimers = {};

function titleSnapshot(force) {
  if (ctx.store.get('readonly') || !ctx.store.get('lastState')) { return null; }
  const hasContent = (ctx.store.get('lastState').messages || []).length > 0
    || (ctx.store.get('lastState').final && ctx.store.get('lastState').final.text);
  if (!force && !hasContent) { return null; }
  const target = ctx.store.get('lastState').target || {};
  if (!target.root || !target.session) { return null; }
  const key = target.root + "|" + target.session;
  if (titleAsked[key]) { return null; }
  const rows = ctx.store.get('lastState').sessions || [];
  let row = null;
  for (const one of rows) { if (one.id === target.session) { row = one; break; } }
  const title = String((row && row.title) || target.session_title || "").trim();
  const source = String((row && row.title_source) || target.session_title_source || "");
  if (title && source !== "seed") { return null; }
  return {key: key, session: target.session};
}

async function requestTitle(key, session, force) {
  if (ctx.store.get('running') || ctx.store.get('readonly')) { return; }
  const ready = titleSnapshot(force);
  if (!ready || ready.key !== key) { return; }
  titleAsked[key] = true;
  try {
    const response = await fetch("/session/title", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session: session}),
    });
    if (response.status === 409) {
      delete titleAsked[key];
      titleTries[key] = (titleTries[key] || 0) + 1;
      if (titleTries[key] <= 3) {
        setTimeout(function () { maybeAgentTitle(true); }, 700 * titleTries[key]);
      }
      return;
    }
    if (!response.ok) { return; }
    titleTries[key] = 0;
    const detail = await response.json().catch(function () { return {}; });
    if (detail && detail.title) {
      applySessionTitle(session, detail.title,
        detail.source || (detail.generated ? "agent" : "seed"));
    }
  } catch (err) {
    // 标题是锦上添花：网络失败保留种子标题，不打扰用户。
  }
}

function maybeAgentTitle(force) {
  if (ctx.store.get('running')) { return; }
  const ready = titleSnapshot(force);
  if (!ready || titleTimers[ready.key]) { return; }
  titleTimers[ready.key] = setTimeout(function () {
    delete titleTimers[ready.key];
    requestTitle(ready.key, ready.session, force);
  }, TITLE_IDLE_MS);
}

function applySessionTitle(id, title, source) {
  if (!ctx.store.get('lastState')) { return; }
  ctx.store.update('lastState', function (snap) {
    const next = Object.assign({}, snap || {});
    const rows = (next.sessions || []).map(function (one) { return Object.assign({}, one); });
    let found = false;
    for (let index = 0; index < rows.length; index += 1) {
      if (rows[index].id !== id) { continue; }
      rows[index].title = title;
      rows[index].title_source = source;
      found = true;
      break;
    }
    if (!found) {
      rows.unshift({
        id: id, title: title, title_source: source,
        archived: false, last_active: Date.now() / 1000, current: true,
      });
    }
    next.sessions = rows;
    const target = Object.assign({}, next.target || {});
    if (target.session === id) {
      target.session_title = title;
      target.session_title_source = source;
      next.target = target;
    }
    return next;
  });
  ctx.sidebar.drawTarget(ctx.store.get('lastState'));
}

// 「移除」这一段会话：**两次点击**才算数（第一次只是把这一行变成确认态）。
// 服务端把它按归档处理（数据不动、可恢复），正在跑的那一条会被拒。
function requestRemove(value) {
  if (ctx.store.get('readonly')) { return; }
  if (ctx.store.get('armed') !== value) {
    ctx.store.set('armed', value);
    ctx.sidebar.markArmed();
    return;
  }
  ctx.store.set('armed', '');
  ctx.sidebar.markArmed();
  act('/session/delete', {session: value}, '已移除会话 ' + value + '（归档：数据未改动）');
}

// 「恢复」：把归档过的那一条放回列表（同一个端点，另一个方向）。
function requestRestore(value) {
  if (ctx.store.get('readonly')) { return; }
  act('/session/restore', {session: value}, '已恢复会话 ' + value);
}

// 人手改标题。空白折成一格，空的和超过 40 字的就地拦住，不发给服务端，也不截断。
const TITLE_LIMIT = 40;
let renameSessionId = '';
let renameOpener = null;
let renameSaving = false;

function cleanTitle(raw) {
  return String(raw || '').trim().split(/\s+/).filter(function (part) { return part; }).join(' ');
}

function lookupTitle(id) {
  const snap = ctx.store.get('lastState') || {};
  const rows = snap.sessions || [];
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].id === id) { return String(rows[index].title || ''); }
  }
  const target = snap.target || {};
  if (target.session === id) { return String(target.session_title || ''); }
  return '';
}

function showRenameError(message) {
  if (renameError) { renameError.textContent = message || ''; }
}

function setRenameBusy(busy) {
  renameSaving = busy;
  const nodes = [renameInput, renameSave, renameCancel, renameClose];
  for (let index = 0; index < nodes.length; index += 1) {
    if (nodes[index]) { nodes[index].disabled = busy; }
  }
}

function findRenameButton(id) {
  const buttons = document.querySelectorAll('.rename');
  for (let index = 0; index < buttons.length; index += 1) {
    const button = buttons[index];
    if (button.dataset && button.dataset.value === id) { return button; }
  }
  return null;
}

function renameFocusables() {
  const nodes = [renameClose, renameInput, renameCancel, renameSave];
  return nodes.filter(function (node) {
    return node && !node.disabled && typeof node.focus === 'function';
  });
}

function openRename(id, opener) {
  if (ctx.store.get('readonly') || !renameBox) { return; }
  renameSessionId = String(id || '');
  renameOpener = opener || null;
  const title = lookupTitle(renameSessionId);
  if (renameInput) {
    renameInput.value = title;
    renameInput.selectionStart = 0;
    renameInput.selectionEnd = title.length;
  }
  showRenameError('');
  setRenameBusy(false);
  ctx.store.set('renameOpen', true);
  renameBox.classList.remove('hidden');
  renameBox.setAttribute('aria-hidden', 'false');
  if (renameInput && typeof renameInput.focus === 'function') { renameInput.focus(); }
}

function closeRename() {
  if (renameSaving || !renameBox) { return; }
  ctx.store.set('renameOpen', false);
  renameBox.classList.add('hidden');
  renameBox.setAttribute('aria-hidden', 'true');
  const back = findRenameButton(renameSessionId) || renameOpener;
  if (back && typeof back.focus === 'function') { back.focus(); }
}

async function saveRename() {
  if (renameSaving || !ctx.store.get('renameOpen')) { return; }
  const cleaned = cleanTitle(renameInput ? renameInput.value : '');
  if (!cleaned) {
    showRenameError('标题是空的。');
    return;
  }
  if (cleaned.length > TITLE_LIMIT) {
    showRenameError('标题最长 ' + TITLE_LIMIT + ' 个字。');
    return;
  }
  setRenameBusy(true);
  showRenameError('');
  try {
    const response = await fetch('/session/rename', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: renameSessionId, title: cleaned}),
    });
    let detail = {};
    try { detail = await response.json(); } catch (err) { detail = {}; }
    if (!response.ok) {
      showRenameError(String((detail && detail.error) || '重命名没有完成。'));
      return;
    }
    const title = String((detail && detail.title) || cleaned);
    applySessionTitle(renameSessionId, title, 'manual');
    setRenameBusy(false);
    closeRename();
  } catch (err) {
    showRenameError('重命名没有完成。');
  } finally {
    if (renameSaving) { setRenameBusy(false); }
  }
}

if (renameClose) {
  renameClose.innerHTML = ICON.close;
  renameClose.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    closeRename();
  };
}
if (renameCancel) {
  renameCancel.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    closeRename();
  };
}
if (renameForm) {
  renameForm.onsubmit = function (event) {
    if (event && event.preventDefault) { event.preventDefault(); }
    saveRename();
  };
}

// 会话这一类动作的公共落地：成功就按返回体重绑（它就是新的 /state）。
async function act(url, payload, note) {
  const next = await ctx.transport.post(url, payload);
  if (next === null) {
    // 被拒（409 正在跑 / 只读 403 / 400 查无此会话）：把确认态收回去，
    // 理由已经由 post() 摆进对话区了。
    ctx.store.set('armed', '');
    ctx.sidebar.drawTarget(ctx.store.get('lastState'));
    ctx.sidebar.markArmed();
    return;
  }
  ctx.store.set('armed', '');
  ctx.applyState(next);
  // 做成了的回执走**自己会消失**的那种提示（几秒）：这一件事的持久记录是左栏
  // 那两段本身（会话移到了"已移除"里 / 又回到列表里），不必在对话流里再摆一行。
  ctx.toasts.notifyInfo(note);
}

// 「新建会话」：会话**按工作区**存，而服务端有了一条真的建会话的路
// （`POST /session/new` → `kernel_sessions.create_session`）。以前这里只做
// "把目标切到一个还没跑过的新名字"：`/target` 不新建会话（`resolve_session`
// 不认识就报错、且**绝不静默新建**），于是顶部那个「＋」点下去只有 400。
//
// 两步是刻意的：① 先让服务端**真把会话建出来**（重名 / 非法名在那里被拦下，
// 给出可读说明）；② 再走**同一条**切换路径（`switchTarget` → `POST /target`）切过去
// ——"切目标"在这条壳上仍然只有一处。点完立刻亮：`chosen` 先点亮那一行，
// 不等下一轮 `/state`（也不必等，`/target` 的响应体就是新的完整状态）。
async function newSession() {
  if (ctx.store.get('readonly') || (ctx.store.get('lastState').target || {}).pinned) { return; }
  // 不再让人先起名：id 由服务端自动生成（时间戳 / uuid 都行），
  // 第一轮跑完后由 Agent 用目标与结论起一个可读标题。
  const made = await ctx.transport.post('/session/new', {});
  if (made === null) { return; }
  // `await`：这一趟做完（回帧已经重绑到新会话）才算"新建完了"，调用方
  // （与 `tests/web/page_dom.js` 那个假 DOM）据此判断页面有没有切过去。
  await ctx.switchTarget({session: made.session});
}

  return { sessionTitle: sessionTitle, sessionRowTitle: sessionRowTitle, maybeAgentTitle: maybeAgentTitle, applySessionTitle: applySessionTitle, requestRemove: requestRemove, requestRestore: requestRestore, act: act, newSession: newSession, openRename: openRename, saveRename: saveRename, closeRename: closeRename, renameFocusables: renameFocusables };
}

// spool web · js/features/thread.js
/* ══ 消息卡片上的「思考过程」：默认收起、收起时一个节点都不建、事后能再展开 ══
   数据有两个来源，各管一段：

   - **正在跑的那一轮**由事件现攒（`liveTraceLine`），所以它边跑边可看——不必等跑完；
   - **跑完之后**（刷新页面、切走再切回）一律以服务端那份为准（`messages[i].trace`，
     见 `web/runner.py::attach_traces`）。所以"看过就没了"这件事不会再有。

   两边文字刻意写得一样（步骤 / 工具成功失败 / 等待心跳的措辞与服务端 `trace_line`
   一致），免得同一轮在跑完前后看到两种写法。 */

// 事件 → 轨迹行（措辞与服务端 `runner.trace_line` 一致）。
function liveTraceLine(data) {
  const kind = String(data.type || '');
  if (kind === 'step') { return {type: 'step', text: '第 ' + data.n + ' 步'}; }
  if (kind === 'tool') {
    const ok = Boolean(data.ok);
    const line = {
      type: 'tool', ok: ok,
      // 第三档（没有校验器）：`ok=true` 只是「没有发现问题」，不是「验过了」。
      // 状态字与结论段那句「没有校验器（不是失败）」**刻意不同**——结论段回答
      // "验没验"，这一行回答"这一步真的跑了吗"（与服务端 `runner.trace_line` 同口径）。
      text: String(data.name || '工具') + ' → '
        + (data.no_checker ? NO_CHECKER_STATE : (ok ? '成功' : '失败')),
    };
    // 失败那行**必须带原因**：只写"read_file → 失败"等于把排错要的那一句丢掉。
    // 第三档同样带上——它给的是理由（"这次改的是 .txt。"）。
    if (data.detail && (!ok || data.no_checker)) { line.detail = String(data.detail); }
    return line;
  }
  if (kind === 'note') {
    return {type: 'note', ok: Boolean(data.ok), text: String(data.text || '')};
  }
  if (kind === 'usage') {
    // 用量是**详情**：它进「思考过程」这一档，不进对话流（现场夹具：它原先紧跟
    // 在收尾报告后面，读起来像报告接着说的一句）。措辞与服务端
    // `runner.trace_line` 同一份。
    return {
      type: 'usage', ok: true,
      text: '用量：' + (data.steps || 0) + ' 步 · ' + (data.calls || 0) + ' 次调用 · '
        + (data.prompt_tokens || 0) + '+' + (data.completion_tokens || 0) + ' token',
    };
  }
  if (kind === 'wait') {
    const phase = data.phase ? String(data.phase) : '';
    const head = phase ? phase + '：' : '';
    let text = head + WAIT_PREFIX + ' ' + data.elapsed + 's';
    if (data.limit) { text += ' / 上限 ' + data.limit + 's'; }
    // phase 单独带着：同一阶段的心跳要**替换**末行，不能堆几十行 15s/30s。
    return {type: 'wait', text: text, phase: phase};
  }
  if (kind === 'await') {
    return {
      type: 'await',
      text: data.command ? '等你确认：要执行 ' + data.command
        : '等你确认：' + (data.count || 1) + ' 处改动',
    };
  }
  if (kind === 'confirm') {
    return {
      type: 'confirm', ok: Boolean(data.applied),
      text: (data.applied ? '已应用 ' : '已拒绝 ') + (data.count || 0) + ' 处改动'
        + (data.auto ? '（自动）' : ''),
    };
  }
  if (kind === 'error') {
    return {type: 'error', ok: false, text: String(data.message || '')};
  }
  return null;
}

/* ══ 收尾报告的**分档**：结论段进气泡，详情行折进「思考过程」 ══
   现场（tests/fixtures/interact_probe/web-real-reports.md）：结论段里混进了
   「本次运行共落成 1 处改动 / 最终文件状态 / 指纹」这些内部账。它们是**详情**：
   报告里带 `详情：` 的那些行折进上面那块默认收起的轨迹，气泡里只留
   「做成了什么 + 验没验 + 我能做什么」。

   两个标记与服务端同一个字面量（`web/runner.py::DETAIL_MARK` /
   `NO_CHECKER_STATE`，后者同 `agent/loop.py::VERIFY_SKIPPED`）：这一层不引 python，
   所以各写一遍，靠两边的断言防走散（tests/web/test_page_dom.py）。 */
const DETAIL_MARK = '详情：';
const NO_CHECKER_STATE = '跳过（不是失败）';

/* ══ 这一轮的助手卡片：占位 → 真正的回复 ══
   `start` 一到就摆出占位（"正在思考"），回复到达时**替换同一张卡片**里的内容
   （不是另起一张），所以页面上不会留下陈旧的占位、也不会同一条回复出现两遍。
   思考过程挂在这张卡片上，随事件增量追加。 */
function createThread(ctx) {
let live = null;
// 快照里那条回复对应的卡片（已经用事件画过回复、库里的那条刚到）：按正文认领。
let pendingReplies = [];
// 刚发出去、`/state` 还没送回来的那条用户消息（乐观画过的那条，不许画第二遍）。
let pendingUser = '';
// 最近画进对话流的那条快照消息：兜底判重要用（"这句回复已经在了，不再说第二遍"）。
let lastDrawn = null;

function ensureLive() {
  if (live !== null && !live.done) { return live; }
  const wrap = makeMsg('assistant');
  const trace = makeTrace();
  const bubble = document.createElement('div');
  bubble.className = 'bubble pending';
  bubble.textContent = '生成中';
  wrap.appendChild(trace);
  wrap.appendChild(bubble);
  // `turnGoal` 是"这张卡片属于哪一轮"：刷新 / 补帧时靠它认出"还是同一轮"，
  // 不会又摆一张占位卡片出来。
  live = {wrap: wrap, trace: trace, bubble: bubble, done: false, turnGoal: '',
    streamed: false};
  if (ctx.composer && ctx.composer.syncEmpty) { ctx.composer.syncEmpty(); }
  return live;
}

// assistant delta：第一次把「生成中」清掉，之后追加纯文本（final 再整段覆盖）。
function replaceSection(index, text) {
  const stick = atBottom();
  const card = ensureLive();
  if (!card.streamed) {
    card.bubble.className = 'bubble';
    card.bubble.textContent = '';
    card.streamed = true;
  }
  let node = card.bubble.querySelector('[data-section="' + index + '"]');
  if (!node) {
    node = document.createElement('div');
    node.setAttribute('data-section', String(index));
    card.bubble.appendChild(node);
  }
  node.textContent = text;
  if (stick) { scrollDown(); }
  updateToBottom();
}

function appendDelta(piece) {
  if (!piece) { return; }
  const stick = atBottom();
  const card = ensureLive();
  if (!card.streamed) {
    card.bubble.className = 'bubble';
    card.bubble.textContent = '';
    card.streamed = true;
  }
  card.bubble.textContent += piece;
  if (stick) { scrollDown(); }
  updateToBottom();
}

function addTraceLine(line) {
  if (line === null || line === undefined) { return; }
  const stick = atBottom();
  const card = ensureLive();
  pushTraceLine(card, line);
  if (stick) { scrollDown(); }
  updateToBottom();
}

// 往一张**已经画出来的**卡片上追加一行轨迹（不摆新卡片）。
//
// 为什么需要它：用量那条事件可能在结局**之后**才到（实测现场就是这样），
// 那时 `live.done` 已经是 true，`ensureLive()` 会另起一张空占位卡——页面上于是
// 多出一张只有一行用量的"幽灵回复卡"。往已有的那张追加就没有这个问题。
//
// wait：同一 phase 替换末行（心跳计时重置时 phase 会变，例如「输出加长」）。
// 运行中自动展开思考过程，让进度轴看得见（设计 interactive-harness §3.4）。
function pushTraceLine(card, line) {
  if (card === null || card === undefined || line === null || line === undefined) { return; }
  if (card.wrap && card.wrap.querySelector('.task-card')) {
    // 这张助手消息已经有任务卡：旧轨迹不再展开，也不再往页面上画。
    return;
  }
  const lines = card.trace._lines;
  if (line.type === 'wait' && lines.length) {
    const last = lines[lines.length - 1];
    if (last.type === 'wait'
        && String(last.phase || '') === String(line.phase || '')) {
      lines[lines.length - 1] = line;
      drawTraceHead(card.trace);
      if (traceOpen(card.trace)) {
        const body = card.trace._body;
        if (body.lastChild) { body.removeChild(body.lastChild); }
        body.appendChild(traceLineNode(line));
      }
      // 仅 live 卡（done === false）自动展开；历史卡无 done 字段，保持默认收起。
      if (card.done === false && !traceOpen(card.trace)) {
        setTraceOpen(card.trace, true);
      }
      return;
    }
  }
  lines.push(line);
  drawTraceHead(card.trace);
  if (card.done === false && !traceOpen(card.trace)) {
    setTraceOpen(card.trace, true);
  } else if (traceOpen(card.trace)) {
    drawTraceBody(card.trace);
  }
}

// 按行首那个标记切开：不按内容猜（"看着像账目的行"那种判据会在模型自己写了
// 一句"本次运行共落成…"时误伤），也不删内容——详情只是换个地方摆。
function splitReport(text) {
  const head = [];
  const detail = [];
  for (const line of String(text || '').split('\n')) {
    (line.indexOf(DETAIL_MARK) === 0 ? detail : head).push(line);
  }
  return {head: head.join('\n'), detail: detail};
}

// 收尾报告上屏：结论段换掉气泡，详情行进轨迹。
function fillReport(card, text) {
  const parts = splitReport(text);
  fillReply(card, parts.head);
  for (const line of parts.detail) {
    pushTraceLine(card, {type: 'note', ok: true, text: line});
  }
}

// 对话流里最后一张**助手**卡片（用量在结局之后才到时，用它把那一行挂回去）。
function lastAssistantCard() {
  const card = lastCard();
  if (card === null) { return null; }
  return (' ' + String(card._cls || '') + ' ').indexOf(' assistant ') >= 0 ? card : null;
}

// 卡片里的气泡换成**真正的回复**。先清空再填：重复到达（事件与快照各来一次）
// 不会叠字，也不会留下那句占位。
function fillReply(card, text) {
  card.bubble.className = 'bubble';
  card.bubble.textContent = '';
  fillRich(card.bubble, text || '（没有产出）');
}

// 结局到达：把同一张卡片更新成结局（占位必须被替换掉）。返回有没有卡片可更新。
function finishLive(ok, text, extra, stopped) {
  const card = live;
  if (card === null) { return false; }
  const stick = atBottom();
  card.done = true;
  if (ok) {
    // 成功：库里那条会写 `result.final or "（没有产出）"`，这里用同一个归一化，
    // 后面认领时才对得上。**正文一个字不删**：`详情：` 那几行只是折进轨迹
    // （`fillReport`），认领比对的仍是完整那份报告。
    fillReport(card, text);
    pendingReplies.push({card: card, text: String(text || '（没有产出）')});
    if (pendingReplies.length > 4) { pendingReplies.shift(); }
    // 跑完收起思考过程（运行中展开过）；失败则保持展开。
    if (card.trace) { setTraceOpen(card.trace, false); }
  } else if (stopped) {
    // 手动停止**不是失败**：低调说一句，别给它套一个红色的失败块。
    card.bubble.className = 'bubble stopped';
    card.bubble.textContent = redact(text) || '本轮已停止（已产出的内容保留）';
    if (card.trace) { setTraceOpen(card.trace, false); }
  } else {
    ctx.toasts.notifyFailure('失败：', text, explainFailure(text, extra));
    failIntoBubble(card.bubble, '失败：', text, extra);
    if (card.trace) { setTraceOpen(card.trace, true); }
  }
  if (stick) { scrollDown(); }
  updateToBottom();
  return true;
}

// 快照里那条助手消息对应哪张卡片：已经用事件画过回复、库里那条刚到的
// （`pendingReplies`，按正文认领）。认领不到就返回 null，调用方照老样子新画一张
// （更早的轮次、别的会话的历史都走这条路）。
function liveCardFor(item) {
  for (let index = 0; index < pendingReplies.length; index += 1) {
    if (pendingReplies[index].text === item.content) {
      const hit = pendingReplies[index].card;
      pendingReplies.splice(index, 1);
      return hit;
    }
  }
  return null;
}

// 快照里这一条已经画过就认领回来（补上服务端那份轨迹），**不新建节点**。
// 乐观画过的那条用户消息、这一轮已经渲染成卡片的回复，都属于这一类：
// 这是"不许留下陈旧占位、也不许重复一条"的关键一步。
function adoptMessage(item, isLast) {
  if (item.role === 'user') {
    if (pendingUser !== '' && item.content === pendingUser) { pendingUser = ''; return true; }
    return false;
  }
  // 只有**这一帧最后一条**才可能是"这一轮刚写好的回复"：更早的那些属于上一轮，
  // 把它们塞进正在跑的卡片里就张冠李戴了（它们的归宿是按正文去 pendingReplies 里认领）。
  let card = null;
  if (isLast && live !== null) { card = live; }
  if (card === null) { card = liveCardFor(item); }
  if (card === null) { return false; }
  const tasks = item.tasks || [];
  if (tasks.length) {
    syncTaskCards(card.wrap, tasks);
    concealTrace(card);
    const parts = splitReport(item.content);
    fillReply(card, parts.head);
  } else {
    if (item.trace && item.trace.length) { fillTrace(card.trace, item.trace); }
    fillReport(card, item.content);
  }
  if (!card.done) { card.done = true; }
  // 详情行是在 `fillTrace` 之后追加的：**不许**把服务端那份轨迹盖掉
  // （它是这一轮的完整过程，详情只是加在它后面）。
  return true;
}

// 画一条快照里的往来。助手那一条如果有轨迹，就多一个**默认收起**的「思考过程」；
// "失败的那一轮"的失败原因与下一步也在这里重新画出来（刷新之后还看得见）。
function concealTrace(card) {
  if (!card || !card.trace) { return; }
  setTraceOpen(card.trace, false);
  card.trace.classList.add('hidden');
}

function historyCard(item) {
  const mine = item.role === 'user';
  const wrap = makeMsg(mine ? 'user' : 'assistant');
  wrap.classList.add('history');
  const tasks = !mine && item.tasks ? item.tasks : [];
  const lines = item.trace || [];
  let trace = null;
  // 有任务卡的助手消息不再画旧轨迹。没有任务的旧消息仍走思考过程。
  if (!mine && !tasks.length && lines.length) {
    trace = makeTrace();
    wrap.appendChild(trace);
    fillTrace(trace, lines);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  wrap.appendChild(bubble);
  const failed = mine || tasks.length ? null : failedTraceLine(lines);
  if (failed) {
    failIntoBubble(bubble, '失败：', failed.text, failed.error || null);
  } else if (tasks.length) {
    const parts = splitReport(item.content);
    fillReply({bubble: bubble}, parts.head);
    syncTaskCards(wrap, tasks);
  } else {
    // 报告的详情行要有地方落脚：这一条没有轨迹时就地建一个（默认收起）。
    if (!mine && trace === null) {
      trace = makeTrace();
      wrap.insertBefore(trace, bubble);
    }
    fillReport({bubble: bubble, trace: trace}, item.content);
  }
  return wrap;
}

function syncLiveTasks(state) {
  const groups = (state && state.task_groups) || [];
  const running = [];
  for (const group of groups) {
    if (group && group.status === 'running') { running.push(group); }
  }
  if (!running.length) { return; }
  const seen = {};
  for (const node of stream.querySelectorAll('.task-card')) {
    seen[node.getAttribute('data-task-id') || ''] = node;
  }
  const fresh = [];
  for (const group of running) {
    const id = String(group.task_id || '');
    const card = seen[id];
    if (!card) { fresh.push(group); continue; }
    card._task = group;
    card.className = taskCardClass(group.status, taskOpen(card));
    drawTaskHead(card);
    if (taskOpen(card)) { drawTaskBody(card); }
  }
  if (!fresh.length) { return; }
  const open = state.trace && state.trace.open;
  if (live === null && !state.running && !open) { return; }
  const card = ensureLive();
  if (!card.turnGoal) {
    const info = state.trace || {};
    card.turnGoal = String(info.goal || state.goal || '');
  }
  syncTaskCards(card.wrap, fresh);
  concealTrace(card);
}

// 刷新 / 切回之后，这一轮可能还在跑：把服务端那份轨迹接回来接着画。
// 三条护栏：
// ① **还是同一轮**（`turnGoal` 对得上）就只接轨迹，绝不另摆一张占位卡片；
// ② 库里那条回复**已经画出来了**（这一帧最后一条是助手那条）也不再摆占位，
//    否则一屏上会出现两张一模一样的回复卡片；
// ③ 服务端那份更长才换——本地刚攒下的行不能被一拍轮询吞掉。
function restoreLive(state) {
  const info = state.trace || {};
  if (!info.open) { return; }
  const goal = String(info.goal || state.goal || '');
  const lines = info.lines || [];
  if (live !== null && live.turnGoal === goal) {
    if (lines.length >= live.trace._lines.length) { fillTrace(live.trace, lines); }
    return;
  }
  const items = state.messages || [];
  const last = items.length ? items[items.length - 1] : null;
  if (last !== null && last.role !== 'user') { return; }
  const card = ensureLive();
  card.turnGoal = goal;
  if (lines.length >= card.trace._lines.length) { fillTrace(card.trace, lines); }
}

// 历史**增量追加**：同一个目标上，/state 的快照可能比上一帧多出几条
// （一次运行跑完之后库里的往来就长了）。以前是"画过就不再画"，于是多出来的
// 那几条永远不出现；现在只补**还没画过**的那一段，已经画好的一条都不重建。
//
// 这一批多了一道**认领**（`adoptMessage`）：这一轮已经实时画过的那些
// （乐观发出去的用户消息、已经渲染成卡片的回复）在快照里再出现时，
// 只把服务端那份轨迹补上去，**不新建节点**——否则同一句话会在页面上出现两遍。
function drawHistory(messages) {
  const items = messages || [];
  if (ctx.store.get('drawn') > items.length) {
    // 列表变短了（换了库、清过历史）：只能重来一遍。这一屏的实时状态也跟着作废，
    // 否则卡片引用会指向已经不在页面上的节点。
    stream.replaceChildren();
    ctx.store.set('drawn', 0);
    live = null;
    pendingReplies = [];
    pendingUser = '';
    lastDrawn = null;
  }
  if (!items.length || ctx.store.get('drawn') === items.length) { return; }
  const stick = atBottom();
  if (ctx.store.get('drawn') === 0) {
    // 一条**纯分隔线**：以前这里挂着一句实现说明，那不是页面文案。
    // 历史本身照旧画出来（见下面的 `.history`：略淡，但一条都不少）。
    const sep = document.createElement('div');
    sep.className = 'history-sep';
    appendNode(sep);
  }
  for (let index = ctx.store.get('drawn'); index < items.length; index += 1) {
    const item = items[index];
    lastDrawn = item;
    if (adoptMessage(item, index === items.length - 1)) { continue; }
    historyCard(item);
  }
  ctx.store.set('drawn', items.length);
  if (stick) { scrollDown(); }
}

/* ── 滚动：默认跟到底，人往上翻就不抢 ── */
function atBottom() {
  return (stream.scrollHeight - stream.scrollTop - stream.clientHeight) <= 48;
}

function scrollDown() {
  // 显式 `behavior:'auto'`：CSS 里的 `scroll-behavior:smooth` 在学习用，但**跟到底**
  // 必须是瞬时的——平滑滚动期间 `scrollTop` 还在路上，一串快速事件会让
  // `atBottom()` 判成"人不在底部"，右下角那个「回到最新」就会一闪一闪。
  if (stream.scrollTo) {
    stream.scrollTo({top: stream.scrollHeight, behavior: 'auto'});
    return;
  }
  stream.scrollTop = stream.scrollHeight;
}

// 悬浮按钮只有"人不在底部"时才摆出来。它同时管两件事：别抢滚动，和让人能回去。
function updateToBottom() {
  const away = !atBottom();
  toBottomBtn.classList.toggle('show', away);
}

function add(text, cls) {
  const stick = atBottom();
  const kind = cls === undefined ? '' : String(cls);
  let node = null;
  if (kind.indexOf('me') >= 0) {
    node = makeBubble('user', text);
  } else if (kind.indexOf('say') >= 0) {
    node = makeBubble('assistant', '');
    fillRich(node, text);
  } else {
    node = makeSystem(text, kind);
  }
  if (stick) { scrollDown(); }
  updateToBottom();
  if (ctx.composer && ctx.composer.syncEmpty) { ctx.composer.syncEmpty(); }
  return node;
}

function resetView() {
  stream.replaceChildren();
  ctx.store.set('drawn', 0);
  ctx.store.set('armed', '');
  // 卡片节点已经跟着 `replaceChildren` 一起没了：引用与状态也得一起清，
  // 否则下一帧会以为"卡片还在"，往一个已经不在页面上的节点上写内容。
  ctx.store.set('card', null);
  ctx.store.set('cardAsking', false);
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  ctx.store.set('cardSig', '');
  // 这一轮的实时卡片、待认领的回复、乐观画过的那条用户消息同样属于上一个目标。
  live = null;
  pendingReplies = [];
  pendingUser = '';
  lastDrawn = null;
  ctx.store.set('decided', false);
  ctx.store.set('pendings', []);
  updateToBottom();
}

function getLive() { return live; }
function setPendingUser(value) { pendingUser = value; }
function getPendingUser() { return pendingUser; }
function getLastDrawn() { return lastDrawn; }

  return { ensureLive: ensureLive, replaceSection: replaceSection, appendDelta: appendDelta, addTraceLine: addTraceLine, pushTraceLine: pushTraceLine, splitReport: splitReport, fillReport: fillReport, lastAssistantCard: lastAssistantCard, fillReply: fillReply, finishLive: finishLive, liveCardFor: liveCardFor, adoptMessage: adoptMessage, historyCard: historyCard, restoreLive: restoreLive, drawHistory: drawHistory, syncLiveTasks: syncLiveTasks, atBottom: atBottom, scrollDown: scrollDown, updateToBottom: updateToBottom, add: add, resetView: resetView, getLive: getLive, setPendingUser: setPendingUser, getPendingUser: getPendingUser, getLastDrawn: getLastDrawn };
}

// spool web · js/features/tasks.js
/* 任务面板。当前会话来自 state.task_groups；跨会话只读 GET /tasks。
   搜索、状态筛选、会话范围都在本地做，不再发请求。
   详情复用任务卡正文，不画 raw，也不复制应用/拒绝。 */
const TASK_FILTERS = [
  ['all', '全部'],
  ['running', '进行中'],
  ['await', '待审批'],
  ['failed', '失败'],
  ['done', '完成'],
];
const TASK_SCOPES = [
  ['current', '当前会话'],
  ['all', '全部会话'],
];

function createTasks(ctx) {
  let filter = 'all';
  let scope = 'current';
  let selected = '';
  let query = '';
  let historyState = 'idle';
  let historyKey = '';
  let historyGroups = [];
  let historySessions = [];

  function groupsOf(state) {
    const rows = ((state && state.task_groups) || []).slice();
    rows.reverse();
    return rows;
  }

  function taskWaiting(task) {
    const events = (task && task.events) || [];
    for (let index = 0; index < events.length; index += 1) {
      const event = events[index];
      if (String(event.phase || '') === 'approval' && approvalWaiting(event.status)) {
        return true;
      }
    }
    return false;
  }

  function matchesTask(task, name) {
    if (name === 'running') { return task.status === 'running'; }
    if (name === 'await') { return taskWaiting(task); }
    if (name === 'failed') { return task.status === 'failed' || task.status === 'verify_failed'; }
    if (name === 'done') { return task.status === 'done'; }
    return true;
  }

  function taskPaths(items) {
    const paths = [];
    const rows = items || [];
    for (let index = 0; index < rows.length; index += 1) {
      const item = rows[index];
      if (item && item.path) { paths.push(String(item.path)); }
    }
    return paths;
  }

  function currentSessionId() {
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    return String(target.session || state.session || '');
  }

  function targetKey() {
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    return String(target.root || '') + '\n' + currentSessionId();
  }

  function sessionTitleFor(id) {
    const sessionId = String(id || '');
    const state = ctx.store.get('lastState') || {};
    const target = state.target || {};
    if (sessionId && sessionId === currentSessionId() && target.session_title) {
      return String(target.session_title);
    }
    for (let index = 0; index < historySessions.length; index += 1) {
      const row = historySessions[index];
      if (row && String(row.id || '') === sessionId && row.title) {
        return String(row.title);
      }
    }
    const listed = (state.sessions) || [];
    for (let index = 0; index < listed.length; index += 1) {
      const row = listed[index];
      if (row && String(row.id || '') === sessionId && row.title) {
        return String(row.title);
      }
    }
    return sessionId;
  }

  function copyTask(task) {
    const copy = {};
    const source = task || {};
    const keys = Object.keys(source);
    for (let index = 0; index < keys.length; index += 1) {
      copy[keys[index]] = source[keys[index]];
    }
    return copy;
  }

  function mergeGroups(live, history) {
    const session = currentSessionId();
    const liveById = {};
    const liveList = [];
    for (let index = 0; index < live.length; index += 1) {
      const task = copyTask(live[index]);
      if (!task.session) { task.session = session; }
      liveById[String(task.task_id || '')] = task;
      liveList.push(task);
    }
    const historyList = history || [];
    const historyIds = {};
    for (let index = 0; index < historyList.length; index += 1) {
      const group = historyList[index];
      if (!group) { continue; }
      historyIds[String(group.task_id || '')] = true;
    }
    const seen = {};
    const merged = [];
    for (let index = 0; index < liveList.length; index += 1) {
      const id = String(liveList[index].task_id || '');
      if (historyIds[id] || seen[id]) { continue; }
      seen[id] = true;
      merged.push(liveList[index]);
    }
    for (let index = 0; index < historyList.length; index += 1) {
      const group = historyList[index];
      if (!group) { continue; }
      const id = String(group.task_id || '');
      if (seen[id]) { continue; }
      seen[id] = true;
      if (liveById[id]) {
        merged.push(liveById[id]);
        continue;
      }
      merged.push(group);
    }
    return merged;
  }

  function taskHaystack(task) {
    const sessionId = String((task && task.session) || currentSessionId());
    const parts = [sessionTitleFor(sessionId), String((task && task.goal) || '')];
    parts.push.apply(parts, taskPaths(task && task.artifacts));
    const events = (task && task.events) || [];
    for (let index = 0; index < events.length; index += 1) {
      const event = events[index] || {};
      parts.push(String(event.tool || ''));
      parts.push(String(event.summary || ''));
      parts.push.apply(parts, taskPaths(event.artifacts));
    }
    return parts.join('\n').toLowerCase();
  }

  function matchesQuery(task) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) { return true; }
    return taskHaystack(task).indexOf(needle) >= 0;
  }

  function paintCounts(groups) {
    let waiting = 0;
    for (let index = 0; index < groups.length; index += 1) {
      if (taskWaiting(groups[index])) { waiting += 1; }
    }
    if (tasksCount) { tasksCount.textContent = String(groups.length); }
    if (tasksWait) {
      tasksWait.textContent = waiting ? String(waiting) : '';
      tasksWait.classList.toggle('hidden', waiting === 0);
    }
    if (tasksBtn) {
      tasksBtn.setAttribute('aria-label', '任务 ' + groups.length + '，待审批 ' + waiting);
    }
  }

  function paintFilters() {
    if (!tasksFilters) { return; }
    tasksFilters.replaceChildren();
    for (let index = 0; index < TASK_FILTERS.length; index += 1) {
      const pair = TASK_FILTERS[index];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-filter' + (pair[0] === filter ? ' on' : '');
      button.textContent = pair[1];
      button.setAttribute('data-filter', pair[0]);
      button.setAttribute('aria-pressed', pair[0] === filter ? 'true' : 'false');
      button.onclick = (function (name) {
        return function () {
          filter = name;
          renderPanel();
        };
      })(pair[0]);
      tasksFilters.appendChild(button);
    }
  }

  function paintScope() {
    if (!tasksScope) { return; }
    tasksScope.replaceChildren();
    for (let index = 0; index < TASK_SCOPES.length; index += 1) {
      const pair = TASK_SCOPES[index];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-scope' + (pair[0] === scope ? ' on' : '');
      button.textContent = pair[1];
      button.setAttribute('data-scope', pair[0]);
      button.setAttribute('aria-pressed', pair[0] === scope ? 'true' : 'false');
      button.onclick = (function (name) {
        return function () {
          scope = name;
          renderPanel();
        };
      })(pair[0]);
      tasksScope.appendChild(button);
    }
  }

  function paintHistory() {
    if (!tasksHistory) { return; }
    tasksHistory.textContent = '';
    if (!ctx.store.get('tasksOpen')) { return; }
    if (historyState === 'loading') {
      tasksHistory.textContent = '正在加载历史';
      return;
    }
    if (historyState === 'error') {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tasks-history-retry';
      button.textContent = '历史加载失败，可重试';
      button.onclick = function () { loadHistory(); };
      tasksHistory.appendChild(button);
    }
  }

  function paintRow(task) {
    const id = String(task.task_id || '');
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'task-line task-card-' + taskStatusClass(task.status)
      + (id === selected ? ' on' : '');
    row.setAttribute('data-task-id', id);
    const mark = h('span', 'task-mark task-chip');
    mark.textContent = TASK_STATUS_LABEL[task.status] || TASK_STATUS_LABEL.running;
    const goalText = redact(String(task.goal || ''));
    const goal = h('span', 'task-goal');
    goal.textContent = goalText;
    goal.title = goalText;
    goal.setAttribute('title', goalText);
    const meta = h('span', 'task-line-meta');
    const steps = h('span', 'task-steps task-chip');
    steps.textContent = taskStepCount(task) + ' 步';
    const verify = h('span', 'task-verify task-chip');
    verify.textContent = taskVerifyText(task);
    meta.appendChild(steps);
    meta.appendChild(verify);
    if (taskWaiting(task)) {
      const wait = h('span', 'task-wait task-chip');
      wait.textContent = '待确认';
      meta.appendChild(wait);
    }
    if (scope === 'all') {
      const chip = h('span', 'task-session task-chip');
      const title = sessionTitleFor(task.session || currentSessionId());
      chip.textContent = title;
      chip.title = title;
      chip.setAttribute('title', title);
      meta.appendChild(chip);
    }
    row.appendChild(mark);
    row.appendChild(goal);
    row.appendChild(meta);
    row.onclick = function () {
      selected = id;
      renderPanel();
    };
    return row;
  }

  function decorate(task) {
    const view = copyTask(task);
    const mine = String(view.session || currentSessionId()) === currentSessionId();
    view.canJump = mine;
    view.currentSession = mine;
    return view;
  }

  function paintDetail(task) {
    if (!tasksDetail) { return; }
    if (!task) {
      tasksDetail.replaceChildren();
      return;
    }
    const view = decorate(task);
    const id = String(view.task_id || '');
    const existing = tasksDetail.querySelector('.task-card');
    if (existing && existing.getAttribute('data-task-id') === id) {
      existing._task = view;
      existing.className = taskCardClass(view.status, taskOpen(existing));
      drawTaskHead(existing);
      if (taskOpen(existing)) { drawTaskBody(existing); }
      return;
    }
    tasksDetail.replaceChildren();
    const card = makeTaskCard(view);
    tasksDetail.appendChild(card);
    if (!taskOpen(card)) { setTaskOpen(card, true, false); }
  }

  function inScope(task) {
    if (scope === 'all') { return true; }
    return String((task && task.session) || currentSessionId()) === currentSessionId();
  }

  function renderPanel() {
    const live = groupsOf(ctx.store.get('lastState'));
    paintCounts(live);
    if (!ctx.store.get('tasksOpen')) { return; }
    paintScope();
    paintHistory();
    paintFilters();
    const merged = mergeGroups(live, historyState === 'ready' ? historyGroups : []);
    const scoped = [];
    for (let index = 0; index < merged.length; index += 1) {
      if (inScope(merged[index])) { scoped.push(merged[index]); }
    }
    const tabbed = [];
    for (let index = 0; index < scoped.length; index += 1) {
      if (matchesTask(scoped[index], filter)) { tabbed.push(scoped[index]); }
    }
    const visible = [];
    for (let index = 0; index < tabbed.length; index += 1) {
      if (matchesQuery(tabbed[index])) { visible.push(tabbed[index]); }
    }
    if (tasksMatch) {
      tasksMatch.textContent = '共 ' + scoped.length + ' 项，匹配 ' + visible.length;
    }
    let current = null;
    for (let index = 0; index < tabbed.length; index += 1) {
      if (String(tabbed[index].task_id || '') === selected) { current = tabbed[index]; }
    }
    if (!current) { selected = ''; }
    if (tasksList) {
      tasksList.replaceChildren();
      if (!visible.length) {
        const empty = h('div', 'tasks-empty');
        empty.textContent = scoped.length
          ? '没有匹配的任务'
          : (scope === 'current' ? '这一会话还没有任务' : '没有匹配的任务');
        tasksList.appendChild(empty);
      } else {
        for (let index = 0; index < visible.length; index += 1) {
          tasksList.appendChild(paintRow(visible[index]));
        }
      }
    }
    paintDetail(current);
  }

  function clearQuery() {
    if (!String(query || '').trim()) { return false; }
    query = '';
    if (tasksSearch) { tasksSearch.value = ''; }
    renderPanel();
    return true;
  }

  function loadHistory() {
    const key = targetKey();
    historyKey = key;
    historyState = 'loading';
    renderPanel();
    const finish = function (ok, payload) {
      if (historyKey !== key) { return; }
      if (!ok || !payload || typeof payload !== 'object') {
        historyState = 'error';
        renderPanel();
        return;
      }
      historyGroups = payload.groups || [];
      historySessions = payload.sessions || [];
      historyState = 'ready';
      renderPanel();
    };
    let pending;
    try {
      pending = fetch('/tasks');
    } catch (err) {
      finish(false, null);
      return;
    }
    Promise.resolve(pending).then(function (response) {
      if (!response || !response.ok || typeof response.json !== 'function') {
        finish(false, null);
        return null;
      }
      return response.json().then(function (payload) {
        finish(true, payload);
      }, function () { finish(false, null); });
    }, function () { finish(false, null); });
  }

  function ensureHistory() {
    const key = targetKey();
    if (historyKey === key && (historyState === 'loading' || historyState === 'ready' || historyState === 'error')) {
      return;
    }
    loadHistory();
  }

  function setTasksOpen(open) {
    if (!tasksPanel) { return; }
    const want = Boolean(open);
    if (want === ctx.store.get('tasksOpen')) { return; }
    ctx.store.set('tasksOpen', want);
    tasksPanel.classList.toggle('hidden', !want);
    tasksPanel.setAttribute('aria-hidden', want ? 'false' : 'true');
    if (tasksBtn) { tasksBtn.setAttribute('aria-expanded', want ? 'true' : 'false'); }
    if (want) {
      ensureHistory();
      renderPanel();
      if (tasksClose && typeof tasksClose.focus === 'function') { tasksClose.focus(); }
    } else if (tasksBtn && typeof tasksBtn.focus === 'function') {
      tasksBtn.focus();
    }
  }

  function sync(state) {
    paintCounts(groupsOf(state));
    if (ctx.store.get('tasksOpen')) {
      ensureHistory();
      renderPanel();
    }
  }

  if (tasksClose) { tasksClose.innerHTML = ICON.close; }
  if (tasksPanel) {
    tasksPanel.classList.add('hidden');
    tasksPanel.setAttribute('aria-hidden', 'true');
  }
  if (tasksBtn) {
    tasksBtn.setAttribute('aria-expanded', 'false');
    tasksBtn.onclick = function () { setTasksOpen(!ctx.store.get('tasksOpen')); };
  }
  if (tasksClose) {
    tasksClose.onclick = function () { setTasksOpen(false); };
  }
  if (tasksSearch) {
    tasksSearch.oninput = function () {
      query = String(tasksSearch.value || '');
      renderPanel();
    };
  }
  paintCounts([]);

  return { sync: sync, setTasksOpen: setTasksOpen, renderPanel: renderPanel, clearQuery: clearQuery };
}

// spool web · js/features/confirm.js
/* ── 「撤销这一轮」：把退路做成动作，不是一条要抄的命令（问题④） ──
   服务端在 `final` 事件上只增地给出 `revert_run_id` / `revert_command`
   （`run.py`），页面据此摆一枚按钮，点一下 POST /revert——**与
   `spool revert --run <id>` 走同一份实现**，用户不需要复制命令。
   幂等：同一个 run_id 只摆一次（事件与快照各来一次也只摆一条）。 */
function createConfirm(ctx) {
let undoRow = null;
let undoRunId = '';
let heldCommand = null;

function showUndo(final) {
  const runId = String((final && final.revert_run_id) || '');
  if (!runId || runId === undoRunId) { return; }
  if (undoRow && undoRow.parentNode) { undoRow.parentNode.removeChild(undoRow); }
  undoRunId = runId;
  const stick = ctx.thread.atBottom();
  const row = document.createElement('div');
  row.className = 'undo-row';
  const label = document.createElement('span');
  label.textContent = '这一轮的成果可以退回去：';
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '撤销这一轮';
  button.onclick = async function () {
    if (button.disabled) { return; }
    button.disabled = true;
    const next = await ctx.transport.post('/revert', {run: runId});
    if (next === null) { button.disabled = false; return; }
    row.className = 'undo-row ok';
    row.replaceChildren(document.createTextNode(
      String((next && next.message) || '已撤销这一轮')));
  };
  row.appendChild(label);
  row.appendChild(button);
  appendNode(row);
  undoRow = row;
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

/* ── 对话流里的待确认卡片 ──
   有待确认内容时才建节点；没有的时候这块 UI **在页面上不存在**（HTML 里没有占位、
   也没有可聚焦的空按钮）。点完「应用/拒绝」它就变成一行状态，留在对话流里当记录。
   只读壳里那两个按钮**根本不建**：点它必被 403 拒，而藏起来的空按钮还能被 Tab 到。 */

// 卡片上写的"谁的"：机器级壳服务整台机器，只说"有 1 处改动待确认"，
// 用户根本不知道自己批的是哪个工作区、哪条会话里的内容。
function targetLabel() {
  const t = (ctx.store.get('lastState') || {}).target || {};
  if (!t.root) { return '目标：未选择'; }
  return '工作区 ' + t.root + ' · 会话 '
    + (String(t.session_title || '').trim() || t.session || '');
}


// 建（或复用）那张卡片。它挂在对话流的最后：ChatGPT 那一挂的"工具调用卡片"也是这样，
// 出现在**对话里**，而不是一个永远浮在输入条上方的空框。
function ensureCard() {
  if (ctx.store.get('card')) { return ctx.store.get('card'); }
  const node = document.createElement('div');
  node.dataset.state = 'asking';
  const head = document.createElement('div');
  head.className = 'ask-head';
  const who = document.createElement('span');
  who.className = 'ask-who';
  const kind = document.createElement('span');
  kind.className = 'ask-kind';
  head.appendChild(who);
  head.appendChild(kind);
  const body = document.createElement('div');
  body.className = 'ask-body';
  node.appendChild(head);
  node.appendChild(body);
  node._who = who;
  node._kind = kind;
  node._body = body;
  node._what = '';
  node._foot = null;
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  if (!ctx.store.get('readonly')) {
    const foot = document.createElement('div');
    foot.className = 'ask-actions';
    const apply = document.createElement('button');
    apply.type = 'button';
    apply.className = 'ask-apply';
    apply.textContent = '应用全部改动';
    apply.onclick = function () { decide(true); };
    const reject = document.createElement('button');
    reject.type = 'button';
    reject.className = 'ask-reject';
    reject.textContent = '拒绝全部';
    reject.onclick = function () { decide(false); };
    foot.appendChild(apply);
    foot.appendChild(reject);
    node.appendChild(foot);
    node._foot = foot;
    ctx.store.set('cardApply', apply);
    ctx.store.set('cardReject', reject);
  }
  node.className = 'ask-card';
  node.dataset.kind = 'diff';
  appendNode(node);
  ctx.store.set('card', node);
  ctx.store.set('cardAsking', true);
  ctx.store.set('decided', false);
  return node;
}

// 卡片上问的是什么（结算时那一行状态要用它）。
function cardWhat() {
  return ctx.store.get('card') ? String(ctx.store.get('card')._what || '') : '';
}

// 没有待确认内容了：**把这块 UI 从页面上拿掉**（不是隐藏）。
function removeCard() {
  if (ctx.store.get('card') && ctx.store.get('card').parentNode) { ctx.store.get('card').parentNode.removeChild(ctx.store.get('card')); }
  ctx.store.set('card', null);
  ctx.store.set('cardAsking', false);
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  ctx.store.set('cardSig', '');
  ctx.store.set('decided', false);
}

// 确认完了：卡片**就地**变成一行"已应用 / 已拒绝"，留在对话流里当记录
// （它不是横幅）。同时把"当前那张卡片"这个位置让出来：以后又有待确认项，
// 是另起一张新卡片，而不是把这一行状态改回问句。
function settleCard(outcome) {
  if (!ctx.store.get('card') || !ctx.store.get('cardAsking')) { return; }
  const node = ctx.store.get('card');
  const what = cardWhat();
  const stick = ctx.thread.atBottom();
  ctx.store.set('card', null);
  ctx.store.set('cardAsking', false);
  ctx.store.set('cardApply', null);
  ctx.store.set('cardReject', null);
  ctx.store.set('cardSig', '');
  const line = document.createElement('div');
  line.className = 'ask-done';
  const kind = node.dataset.kind;
  let text;
  if (kind === 'combined') {
    const count = node.dataset.changes;
    const commandText = node.dataset.command;
    text = outcome === 'applied'
      ? ('已应用 ' + count + ' 处改动并允许执行：命令 ' + commandText)
      : ('已拒绝：' + count + ' 处改动 + 命令 ' + commandText);
  } else {
    const command = kind === 'command';
    const prefix = command
      ? (outcome === 'applied' ? '已允许执行：' : '已拒绝：')
      : (outcome === 'applied' ? '已应用：' : '已拒绝：');
    text = prefix + what;
  }
  line.textContent = text + '（' + targetLabel() + '）';
  heldCommand = null;
  node.className = 'ask-row ' + (outcome === 'applied' ? 'ok' : 'bad');
  node._foot = null;
  node.replaceChildren(line);
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

function stampAskCalls(node, items, detail) {
  if (!node) { return; }
  const ids = [];
  function rememberCall(value) {
    const text = String(value || '');
    if (text && ids.indexOf(text) < 0) { ids.push(text); }
  }
  for (const item of items || []) { rememberCall(item && item.tool_call_id); }
  if (detail) {
    rememberCall(detail.tool_call_id);
    const many = detail.tool_call_ids;
    if (many && many.length) { for (const id of many) { rememberCall(id); } }
  }
  if (!ids.length) {
    node.removeAttribute('data-tool-call-id');
    node.removeAttribute('data-tool-call-ids');
    return;
  }
  node.setAttribute('data-tool-call-id', ids[0]);
  node.setAttribute('data-tool-call-ids', ids.join(' '));
}

function showPending(items) {
  if (!items.length) { clearPending(); return; }
  ctx.store.set('pendings', items);
  // 同一份待确认内容（补帧 / 重连后的一帧）只更新，不重建：
  // diff 正文可能有几百行，重建一次没有收益。
  const sig = JSON.stringify(['diffs', items.map(function (one) {
    return [one.path, one.text];
  })]);
  if (ctx.store.get('card') && ctx.store.get('cardAsking') && ctx.store.get('cardSig') === sig) {
    stampAskCalls(ctx.store.get('card'), items, null);
    return;
  }
  const node = ensureCard();
  ctx.store.set('cardSig', sig);
  const stick = ctx.thread.atBottom();
  node.className = 'ask-card';
  node.dataset.kind = 'diff';
  node.dataset.state = 'asking';
  stampAskCalls(node, items, null);
  setAskLabels(false);
  node._what = items.length + ' 处改动';
  node._who.textContent = targetLabel();
  node._kind.textContent = items.length + ' 处改动待确认';
  node._body.replaceChildren();
  for (const item of items) { node._body.appendChild(diffBox(item.path, item.text)); }
  if (node._foot) { node._foot.style.display = 'flex'; }
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

// 服务端说"没有待确认内容了"（或确认完了）。`outcome` 给 'applied' / 'rejected'
// 时卡片变成那一行状态；不给时**把这块 UI 整个拿掉**（页面上不留空框）。
function clearPending(outcome) {
  heldCommand = null;
  ctx.store.set('pendings', []);
  if (!ctx.store.get('card')) { ctx.store.set('decided', false); return; }
  // 已经结算过的那一行状态是记录，留着（它不是横幅，是"这一趟批过什么"）。
  if (!ctx.store.get('cardAsking')) { return; }
  if (outcome === 'applied' || outcome === 'rejected') { settleCard(outcome); return; }
  removeCard();
}
function setAskLabels(mode) {
  const apply = ctx.store.get('cardApply');
  const reject = ctx.store.get('cardReject');
  const command = mode === true;
  const combined = mode === 'combined';
  if (apply) {
    apply.textContent = combined ? '应用改动并允许执行'
      : (command ? '允许执行' : '应用全部改动');
  }
  if (reject) {
    reject.textContent = combined ? '全部拒绝'
      : (command ? '拒绝' : '拒绝全部');
  }
}

function showCombined(items, detail) {
  ctx.store.set('pendings', items);
  const sig = JSON.stringify(['combined', items.map(function (one) {
    return [one.path, one.text];
  }), detail.command, detail.reason || '']);
  if (ctx.store.get('card') && ctx.store.get('cardAsking') && ctx.store.get('cardSig') === sig) {
    stampAskCalls(ctx.store.get('card'), items, detail);
    return;
  }
  const node = ensureCard();
  ctx.store.set('cardSig', sig);
  const stick = ctx.thread.atBottom();
  node.className = 'ask-card combined';
  stampAskCalls(node, items, detail);
  node.dataset.kind = 'combined';
  node.dataset.state = 'asking';
  setAskLabels('combined');
  node.dataset.changes = String(items.length);
  node.dataset.command = String(detail.command);
  node._what = items.length + ' 处改动 + 命令 ' + detail.command;
  node._who.textContent = targetLabel();
  node._kind.textContent = items.length + ' 处改动 + 1 条命令待确认';
  node._body.replaceChildren();
  for (const item of items) { node._body.appendChild(diffBox(item.path, item.text)); }
  const title = document.createElement('div');
  title.className = 'ask-title';
  const mark = document.createElement('span');
  mark.className = 'ask-mark';
  mark.textContent = '⚠';
  title.appendChild(mark);
  title.appendChild(document.createTextNode(' 要执行一条命令'));
  const pre = document.createElement('pre');
  pre.className = 'ask-cmd';
  const code = document.createElement('code');
  code.textContent = String(detail.command || '');
  pre.appendChild(code);
  const why = document.createElement('div');
  why.className = 'ask-reason';
  why.textContent = '原因：' + (detail.reason || '未说明');
  const how = document.createElement('div');
  how.className = 'ask-warn';
  how.textContent = '仅允许这一次；长期允许需写入 .agent/grants.json';
  node._body.appendChild(title);
  node._body.appendChild(pre);
  node._body.appendChild(why);
  node._body.appendChild(how);
  if (node._foot) { node._foot.style.display = 'flex'; }
  if (stick) { ctx.thread.scrollDown(); }
  ctx.thread.updateToBottom();
}

function showApproval(spec) {
  const diffs = (spec && spec.diffs) || [];
  const detail = (spec && spec.commandDetail) || null;
  const command = detail && detail.command ? detail : null;
  heldCommand = command;
  if (diffs.length && command) { showCombined(diffs, command); return; }
  if (command) {
    ctx.status.showCommandAsk(command);
    stampAskCalls(ctx.store.get('card'), [], command);
    return;
  }
  if (diffs.length) { showPending(diffs); return; }
}

function currentCommand() {
  if (!ctx.store.get('card') || !ctx.store.get('cardAsking')) { return null; }
  return heldCommand;
}

// 两个按钮是在建卡片时现绑的（`ensureCard`）——没有待确认内容时
// 页面上根本没有这两个按钮，也就没有可聚焦的空控件。
async function decide(apply) {
  // 点一次就禁用：重复写 stdin 会让后续的 input() 拿到意外的输入。
  if (ctx.store.get('decided') || !ctx.store.get('cardAsking')) { return; }
  ctx.store.set('decided', true);
  if (ctx.store.get('cardApply')) { ctx.store.get('cardApply').disabled = true; }
  if (ctx.store.get('cardReject')) { ctx.store.get('cardReject').disabled = true; }
  const next = await ctx.transport.post('/confirm', {apply});
  if (next === null) {
    // 被拒（409 没有待确认 / 只读 403）：卡片退回"等你点"，别错误地停在"已应用"。
    // 理由已经由 post() 摆进对话区与通知里了。
    ctx.store.set('decided', false);
    if (ctx.store.get('cardApply')) { ctx.store.get('cardApply').disabled = false; }
    if (ctx.store.get('cardReject')) { ctx.store.get('cardReject').disabled = false; }
    return;
  }
  settleCard(apply ? 'applied' : 'rejected');
}

  return { showUndo: showUndo, targetLabel: targetLabel, ensureCard: ensureCard, cardWhat: cardWhat, removeCard: removeCard, settleCard: settleCard, showPending: showPending, showApproval: showApproval, currentCommand: currentCommand, clearPending: clearPending, setAskLabels: setAskLabels, decide: decide };
}

// spool web · js/features/sidebar.js
/* ── 顶上的目标选择（原来是两条下拉，现在是左栏两段） ──
   「我在哪个工作区」必须直接可读：批次一出现过绑错工作区、把 __init__.py 写空的故障，
   而机器级壳服务的是整台机器（设计 §3.3）。 */
function createSidebar(ctx) {
// 可见行自己就是按钮。切目标只走 `ctx.switchTarget` 这一条。
function target(workspace, name) {
  if (workspace !== '') { ctx.switchTarget({workspace: workspace}); return; }
  if (name !== '') { ctx.switchTarget({session: name}); }
}

// ── 左栏的"行复用" ──
// 每一行先描述成一份 spec（文字 / 高亮 / 在不在跑 / 那个小按钮），
// **只有 sig 变了才建节点**：同一帧状态重复到达时，一个节点都不新建、也不替换。
function lineSpec(sig, make) { return {sig: sig, make: make}; }

function actSpec(label, kind, value, onClick) {
  return lineSpec(JSON.stringify(['act', label, kind, value]), function () {
    return rowAction(label, kind, value, onClick);
  });
}

function actGroup(parts) {
  const items = parts.filter(Boolean);
  return lineSpec(items.map(function (item) { return item.sig; }).join('|'), function () {
    const box = document.createElement('span');
    box.className = 'row-acts';
    for (let index = 0; index < items.length; index += 1) {
      box.appendChild(items[index].make());
    }
    return box;
  });
}

function renameSpec(value) {
  return lineSpec(JSON.stringify(['rename', value]), function () {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'row-act rename';
    button.tabIndex = 0;
    button.dataset.act = 'rename';
    button.dataset.value = value;
    button.setAttribute('aria-label', '重命名会话');
    button.title = '重命名会话';
    button.innerHTML = ICON.pencil;
    button.onclick = function (event) {
      if (event && event.stopPropagation) { event.stopPropagation(); }
      ctx.sessions.openRename(value, button);
    };
    button.onkeydown = function (event) {
      if (!event) { return; }
      if (event.stopPropagation) { event.stopPropagation(); }
      if (event.key !== 'Enter' && event.key !== ' ') { return; }
      if (event.preventDefault) { event.preventDefault(); }
      ctx.sessions.openRename(value, button);
    };
    return button;
  });
}

function rowSpec(label, title, on, onClick, key, kind, act, running) {
  // 签名里**不含**"亮不亮"：切一行的高亮只改类，不重建节点（`markChosen` 收尾）。
  return lineSpec(
    JSON.stringify(['row', label, title, key || '', kind || '', running ? 1 : 0,
      act ? act.sig : '']),
    function () { return row(label, title, on, onClick, key, kind, act, running); }
  );
}

// 左栏的一行。点它就是切目标（经 `target()` 落到原生控件上）。
// `key`/`kind` 是给"乐观选中"用的：点下去立刻按它们把这一行点亮，不等回帧。
function row(label, title, on, onClick, key, kind, act, running) {
  const line = document.createElement('div');
  line.className = 'li grp-name';
  line.dataset.key = key || '';
  line.dataset.kind = kind || '';
  line.title = label + (title ? '  —  ' + title : '');
  // 「已移除」那一行不是可点的目标：它只是摆在那里等「恢复」。
  // 行里面还有「移除」按钮，不能再套一层 button，所以这一行用 role=button。
  if (kind === 'gone') {
    line.classList.add('gone');
    line.tabIndex = -1;
  } else {
    line.setAttribute('role', 'button');
    line.tabIndex = 0;
    line.onkeydown = function (event) {
      if (!event) { return; }
      const target = event.target;
      if (target && (target.tagName === 'BUTTON' || (target.closest && target.closest('button')))) { return; }
      if (event.key !== 'Enter' && event.key !== ' ') { return; }
      if (event.preventDefault) { event.preventDefault(); }
      onClick();
    };
  }
  if (on) { line.classList.add('on'); }
  if (on && kind !== 'gone') { line.setAttribute('aria-current', 'true'); }
  const mark = document.createElement('span');
  mark.className = 'caret';
  mark.textContent = on ? '▸' : '·';
  const name = document.createElement('span');
  name.className = 'nm';
  name.textContent = label;
  name.title = label;
  const body = document.createElement('div');
  body.className = 'nm-wrap';
  body.appendChild(name);
  if (title) {
    const src = document.createElement('span');
    src.className = 'src';
    src.textContent = title;
    src.title = title;
    body.appendChild(src);
  }
  line.appendChild(mark);
  line.appendChild(body);
  markRunning(line, Boolean(running));
  if (act) { line.appendChild(act.make()); }
  line.onclick = onClick;
  return line;
}

// 行上那个小按钮（移除 / 恢复）。点第一下只是**变成确认态**，第二下才发请求：
// 一点就删太容易误伤（这一行和"切到这条会话"只差几个像素）。
function rowAction(label, kind, value, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'row-act' + (kind === 'restore' ? ' back' : '');
  button.dataset.act = kind;
  button.dataset.value = value;
  button.textContent = label;
  button.onclick = function (event) {
    event.stopPropagation();   // 别把"点按钮"当成"切到这一条"
    onClick();
  };
  return button;
}

// 确认态（`armed`）就地改文字与类，不重建行——重建会把整段列表推倒重来。
function markArmed() {
  for (const button of document.querySelectorAll('.row-act')) {
    if (button.dataset.act !== 'remove') { continue; }
    const ready = button.dataset.value === ctx.store.get('armed');
    button.textContent = ready ? '确认移除' : '移除';
    button.classList.toggle('armed', ready);
  }
}

// 一段列表（段名 + 若干行）按 spec 同步：同 sig 的那一行原样留着。
function syncGroup(box, specs) {
  for (let index = 0; index < specs.length; index += 1) {
    const spec = specs[index];
    const have = box.children[index];
    if (have && have._sig === spec.sig) { continue; }
    const node = spec.make();
    node._sig = spec.sig;
    if (have) { box.replaceChild(node, have); } else { box.appendChild(node); }
  }
  while (box.children.length > specs.length) { box.removeChild(box.lastChild); }
}

function headSpec(text) {
  return lineSpec('head|' + text, function () {
    const head = document.createElement('div');
    head.className = 'grp-name dim';
    head.textContent = text;
    return head;
  });
}

// 「正在跑」的标记。`state.running_targets` 是**唯一**能说出这件事的地方
// （别的目标上的任务，在本目标的事件流上一个字都不会出现，而 `running` 只讲当前那个）。
function runningRows(state) {
  const rows = state.running_targets || [];
  const found = {};
  for (const one of rows) {
    found[(one.root || '') + '|' + (one.session || '')] = true;
  }
  return found;
}

function markRunning(line, on) {
  if (!on) { return; }
  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.title = RUN_MARK;
  line.appendChild(dot);
}

// 页面上"选中的那一行"：优先人刚点的那个（乐观），否则是服务端确认的目标。
function selectedKeys() {
  if (ctx.store.get('chosen').root || ctx.store.get('chosen').session) { return ctx.store.get('chosen'); }
  const t = ctx.store.get('lastState').target || {};
  return {root: t.root || '', session: t.session || ''};
}

// 只改类的"点亮/熄灭"，不碰结构：点一行的那一下必须**立刻**看得见选中态
// （回帧要等服务端，模型不通时那一趟能有好几秒，人只会以为壳卡死了）。
function markChosen() {
  const want = selectedKeys();
  for (const box of [workspaceList, sessionList]) {
    for (const line of box.children) {
      if (!line.dataset || !line.dataset.kind) { continue; }
      const kind = line.dataset.kind;
      // 「已移除」那几行不是目标（点了也没用），不参与点亮。
      const mine = kind === 'ws'
        ? line.dataset.key === want.root
        : (kind === 'sess' && line.dataset.key === want.session);
      line.classList.toggle('on', mine);
      if (kind !== 'gone') {
        if (mine) { line.setAttribute('aria-current', 'true'); }
        else { line.removeAttribute('aria-current'); }
      }
      const caret = line.querySelector('.caret');
      if (caret) { caret.textContent = mine ? '▸' : '·'; }
    }
  }
}

let sideQuery = '';
let lastLists = null;

function baseName(path) {
  const text = String(path || '').replace(/[\\/]+$/, '');
  const parts = text.split(/[\\/]/);
  return parts.length ? parts[parts.length - 1] : '';
}

// 只认名字或路径最后一段的前缀。名字中间出现 tmp 不算临时目录。
function isTempName(value) {
  const text = String(value || '').trim().toLowerCase();
  if (!text) { return false; }
  if (text.indexOf('spool-chat-') === 0) { return true; }
  if (text.indexOf('temp-') === 0) { return true; }
  if (text.indexOf('tmp') === 0) { return true; }
  return false;
}

function isTempWorkspace(one) {
  return isTempName(one.name) || isTempName(baseName(one.value));
}

function queryHit(query, fields) {
  if (!query) { return false; }
  for (let index = 0; index < fields.length; index += 1) {
    if (String(fields[index] || '').toLowerCase().indexOf(query) >= 0) { return true; }
  }
  return false;
}

function renderSidebar(wrows, srows, sremoved, key, locked, query) {
  lastLists = {wrows: wrows, srows: srows, sremoved: sremoved, key: key, locked: locked};
  const q = String(query || '').trim().toLowerCase();
  const live = runningRows(ctx.store.get('lastState'));
  const here = (ctx.store.get('lastState').target || {}).root || '';
  // 同键跳过：这一份签名没变就**一个节点都不动**。签名里带上"谁在跑"、当前目标与搜索词，
  // 所以该重画的时候（数据真变了，或搜索词变了）一定会重画。
  const sig = JSON.stringify([wrows, srows, sremoved, key, locked, here, ctx.store.get('session'), live, q]);
  if (sig === ctx.store.get('sidebarSig')) { return; }
  ctx.store.set('sidebarSig', sig);

  // 「工作区」这一段的标题已经在 `.side-head` 里了，这里不再画第二遍。
  let wsMatched = false;
  const wsSpecs = [];
  for (const one of wrows) {
    if (one.value === '') { continue; }
    const current = one.value === here;
    const matched = queryHit(q, [one.name, one.alias, one.value]);
    if (matched) { wsMatched = true; }
    if (q) {
      if (!matched && !current) { continue; }
    } else if (isTempWorkspace(one) && !current) {
      continue;
    }
    const on = (one.value + '|' + ctx.store.get('session')) === key;
    wsSpecs.push(rowSpec(
      one.name + ((one.ready === false) ? '（没 init）' : ''),
      '',
      on,
      (function (value) {
        return function () { target(value, ''); };
      })(one.value),
      one.value,
      'ws',
      null,
      Boolean(live[one.value + '|' + ctx.store.get('session')])
    ));
  }
  let sessMatched = false;
  if (q) {
    for (const one of srows) {
      const label = ctx.sessions.sessionRowTitle({id: one.value, title: one.title});
      if (queryHit(q, [one.title, label, one.value])) { sessMatched = true; break; }
    }
  }
  if (q && !wsMatched && !sessMatched) {
    wsSpecs.unshift(lineSpec('no-match|' + q, function () {
      const none = document.createElement('div');
      none.className = 'empty-line';
      none.textContent = '没有匹配的工作区/会话';
      return none;
    }));
  } else if (wsSpecs.length === 0) {
    wsSpecs.push(lineSpec('empty-line', function () {
      const none = document.createElement('div');
      none.className = 'empty-line';
      none.textContent = '（登记表为空：先在工作区目录里执行 spool init）';
      return none;
    }));
  }
  syncGroup(workspaceList, wsSpecs);
  // 钉住（--root）时两段列表都只读：`LOCK` 走 CSS 的 pointer-events，
  // 行本身还是画出来（人要看得到"这里有哪几条"），只是点不动，也不进 Tab。
  workspaceList.classList.toggle(LOCK, locked);
  lockRows(workspaceList, locked);

  const sessSpecs = [headSpec('会话')];
  for (const one of srows) {
    if (one.value === '') { continue; }
    const on = one.value === ctx.store.get('session');
    const label = ctx.sessions.sessionRowTitle({id: one.value, title: one.title});
    if (q && !queryHit(q, [one.title, label, one.value]) && !on) { continue; }
    const removable = !locked && !ctx.store.get('readonly');
    const acts = [];
    // 只给当前这条、且不是只读/归档的行摆铅笔。点它不能冒泡成切换。
    if (on && !ctx.store.get('readonly') && !one.archived) {
      acts.push(renameSpec(one.value));
    }
    if (removable) {
      acts.push(actSpec('移除', 'remove', one.value, (function (value) {
        return function () { ctx.sessions.requestRemove(value); };
      })(one.value)));
    }
    // 标题优先、id 退到右边的小字：uuid / 时间戳只用来定位，不该当名字读。
    sessSpecs.push(rowSpec(
      label,
      String(one.value || ''),
      on,
      (function (value) {
        return function () { target('', value); };
      })(one.value),
      one.value,
      'sess',
      acts.length ? actGroup(acts) : null,
      // 「正在跑」的键是 (工作区, 会话)：别的会话在跑也要标出来
      // （切走不杀任务，见设计 §3.4）。
      Boolean(live[keyFor(here, one.value)])
    ));
  }
  // 已归档会话**不在左栏摊开**：只更新页脚「设置」上的数量，列表在设置里。
  ctx.settings.updateArchiveBadge(sremoved);
  syncGroup(sessionList, sessSpecs);
  sessionList.classList.toggle(LOCK, locked);
  lockRows(sessionList, locked);
  markArmed();
  // 收尾点亮选中态：复用的那几行（签名没变、节点没重建）也要跟着高亮走。
  markChosen();
}

function drawTarget(state) {
  const t = state.target || {};
  const key = (t.root || '') + '|' + (t.session || '');
  // 第一次画不算"变了"：那时页面上本来就什么都没有，清一遍只会白闪一下。
  const changed = ctx.store.get('bound') !== '' && key !== ctx.store.get('bound');
  ctx.store.set('bound', key);

  const workspaces = (state.workspaces || []).slice();
  // 当前目标不在登记表里（指针指着一个后来被删掉的工作区）：也得显示得出来。
  if (t.root && !workspaces.some(function (row) { return row.root === t.root; })) {
    workspaces.push({root: t.root, name: t.root + '  ——  当前（不在登记表里）', ready: true});
  }

  const sessions = (state.sessions || []).slice();
  // 当前会话**不一定**在会话表里：那张表按"跑过没有"记，`cli` 这种还没跑过的名字
  // 不会出现。权威值是 `target.session`；补一条当前会话，左栏才看得到"我现在在哪"，
  // 也才有可点的当前行。标题随 target 一起给（`session_title`）。
  if (t.session && !sessions.some(function (row) { return row.id === t.session; })) {
    sessions.unshift({
      id: t.session,
      title: t.session_title || '',
      title_source: t.session_title_source || '',
      archived: false,
      last_active: 0,
      current: true,
    });
  }
  // `--root` 起的壳只有一个目标：两段列表都只读，并把原因写出来。
  const locked = Boolean(t.pinned);

  if (t.root) {
    let wsName = '';
    for (const row of (state.workspaces || [])) {
      if (row.root === t.root && String(row.name || '').trim()) {
        wsName = String(row.name).trim();
        break;
      }
    }
    if (!wsName) { wsName = baseName(t.root) || t.root; }
    const sessName = String(t.session_title || '').trim() || t.session || '';
    whereBox.textContent = wsName + ' · ' + sessName;
    whereBox.title = t.root;
    whereBox.className = 'where';
  } else {
    whereBox.textContent = '还没有目标';
    whereBox.title = '';
    whereBox.className = 'where empty';
  }

  // 左栏那两段：工作区在前、它会话在后。点一下就是一次 POST /target。
  renderSidebar(
    workspaces.map(function (w) {
      return {value: w.root, name: w.name || w.root, alias: w.alias || '', ready: w.ready};
    }),
    sessions.map(function (s) {
      return {value: s.id, title: s.title || '', text: s.title || s.id, archived: !!s.archived};
    }),
    // 已移除（归档）的那几条单独一段：删了还能放回来，才算"移除"而不是"毁掉"。
    (state.sessions_removed || []).map(function (s) {
      return {id: s.id, title: s.title || ''};
    }),
    key,
    locked,
    sideQuery
  );
  // 「新建会话」也归这两条判据管：钉住的壳只有一个目标，只读的壳点了会被拒。
  const dead = locked || ctx.store.get('readonly');
  newBtn.classList.toggle(LOCK, dead);
  newBtn.disabled = dead;

  // `--root` 钉住与"还没有目标"以前是顶部横幅上的两句常驻提示（一出现就把对话流
  // 往下推）。现在它们落在状态区：`--root` 是锁徽标上的"已钉住" + 详情里的一段解释，
  // 空态引导是"未选工作区"那枚徽标 + 详情。判据一个字都没丢，占位没了。
  ctx.store.set('pinned', locked);
  ctx.store.set('pinnedNote', locked
    ? '这条壳由 --root 启动：只有这一个目标，无法切换。'
      + '去掉 --root 重启即为机器级。'
    : '');
  ctx.store.set('targetMissing', !t.root);
  const notes = [];
  if (!t.root) {
    // 空态要**引导人选**，不是摆一堆空白：候选清单就在左边那条工作区段里。
    // 但那张表也可能是**空的**（一个工作区都没登记过）——那时说"在左边选一个"
    // 就是死路，所以补一句怎么造出候选来（评审逮到的就是这条）。
    notes.push(t.note || '未选择工作区：在左侧「工作区」段选一项；'
      + '登记表为空时先 `spool init`。');
  }
  if (ctx.store.get('busy')) {
    // 运行中那一枚键变成「停止」（`syncSend`）：这里只说清"点了会发生什么"，
    // 因为"停止"这两个字本身回答不了"已产出的内容会不会丢失"。
    notes.push('本轮运行中：点输入条右侧的「停止」可中断它'
      + '（温和停止，已产出的内容保留）。');
  }
  if (!ctx.store.get('busy') && (ctx.store.get('lastState') || {}).stopped) {
    notes.push('上一轮已手动停止（已产出的内容保留），可直接发送下一条。');
  }
  ctx.store.set('targetNotes', notes);
  // 状态区收尾重画：目标（空态）、模式（钉住）、运行中那几句都在这一下里对齐。
  ctx.status.drawStatus();
  return changed;
}


function narrow() {
  return typeof matchMedia === 'function' && matchMedia('(max-width:860px)').matches;
}

function lockRows(box, locked) {
  for (const line of box.children) {
    if (!line.dataset || line.dataset.kind !== 'ws' && line.dataset.kind !== 'sess') { continue; }
    line.tabIndex = locked ? -1 : 0;
  }
}

function syncDrawer() {
  const open = document.body.classList.contains('drawer');
  if (menuBtn) { menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  if (!sidebarBox) { return; }
  if (narrow() && !open) {
    sidebarBox.setAttribute('inert', '');
    sidebarBox.setAttribute('aria-hidden', 'true');
  } else {
    sidebarBox.removeAttribute('inert');
    sidebarBox.removeAttribute('aria-hidden');
  }
}

function openDrawer() {
  document.body.classList.add('drawer');
  scrim.classList.add('show');
  syncDrawer();
  focus(newBtn);
}

function closeDrawer() {
  const was = document.body.classList.contains('drawer');
  document.body.classList.remove('drawer');
  scrim.classList.remove('show');
  syncDrawer();
  if (was) { focus(menuBtn); }
}

// 窄屏且抽屉关着：离屏侧栏不进 Tab。宽度变化（含测试里改视口）也要重算，
// 否则桌面宽度下侧栏会一直 inert。
if (typeof matchMedia === 'function') {
  const drawerQuery = matchMedia('(max-width:860px)');
  if (drawerQuery && typeof drawerQuery.addEventListener === 'function') {
    drawerQuery.addEventListener('change', syncDrawer);
  }
}
syncDrawer();

const sideSearch = document.getElementById('side-search');

function applySideQuery(value) {
  sideQuery = String(value || '');
  if (!lastLists) { return; }
  renderSidebar(
    lastLists.wrows, lastLists.srows, lastLists.sremoved,
    lastLists.key, lastLists.locked, sideQuery
  );
}

if (sideSearch) {
  sideSearch.oninput = function () { applySideQuery(sideSearch.value); };
  sideSearch.onkeydown = function (event) {
    if (!event || event.key !== 'Escape') { return; }
    if (event.preventDefault) { event.preventDefault(); }
    if (event.stopPropagation) { event.stopPropagation(); }
    sideSearch.value = '';
    applySideQuery('');
    focus(goalBox);
  };
}

  return { target: target, lineSpec: lineSpec, actSpec: actSpec, rowSpec: rowSpec, row: row, rowAction: rowAction, markArmed: markArmed, syncGroup: syncGroup, headSpec: headSpec, runningRows: runningRows, markRunning: markRunning, selectedKeys: selectedKeys, markChosen: markChosen, renderSidebar: renderSidebar, drawTarget: drawTarget, openDrawer: openDrawer, closeDrawer: closeDrawer };
}

// spool web · js/features/composer.js
function createComposer(ctx) {
function setRunning(value) {
  const was = ctx.store.get('busy');
  const fields = {running: value, busy: value};
  if (!value) { fields.stopping = false; }
  ctx.store.patch(fields);
  goalBox.readOnly = value || ctx.store.get('readonly');
  syncSend();
  setThinking(value && ctx.store.get('thinking'));
  // 运行中那句提示在状态详情里（见 drawTarget）：这一格变了就要重画一次，
  // 否则它会停在上一帧——"跑完了还写着正在跑"比不写还坏。
  if (was !== ctx.store.get('busy') && ctx.store.get('lastState') && ctx.store.get('lastState').target) { ctx.sidebar.drawTarget(ctx.store.get('lastState')); }
}

// 那一枚圆键的三态：空闲=发送（箭头），运行中=**停止**（方块，可点），
// 停止请求已发出=正在停止…（禁用一下，免得连点发两次）。只读壳里它一律禁用。
//
// 为什么不是"运行中禁用 + 说明"：服务端现在有停止入口了（`POST /run/stop`，
// 复用 `Runner.stop()` 的温和停止语义），再摆一个灰键没有意义。
function syncSend() {
  const mode = ctx.store.get('busy') ? (ctx.store.get('stopping') ? 'stopping' : 'stop') : 'send';
  sendBtn.disabled = ctx.store.get('stopping') || ctx.store.get('readonly');
  sendBtn.dataset.mode = mode;
  sendBtn.title = ctx.store.get('readonly') ? '只读：这条壳绑在非回环地址上，写请求会被拒'
    : ctx.store.get('busy') ? (ctx.store.get('stopping') ? '停止中' : '停止本轮（已产出的内容保留）')
    : '发送';
  // 按钮里只有一个图标：读屏得有个名字（只给 title 是不够稳的）。
  sendBtn.setAttribute('aria-label', sendBtn.title);
  const icon = mode === 'send' ? ICON.send : ICON.stop;
  // 图标只在**换了那一个**的时候重写：`innerHTML` 每次都写会让按钮闪一下。
  if (sendBtn._icon !== icon) { sendBtn.innerHTML = icon; sendBtn._icon = icon; }
}

// 点那一枚键：空闲时是发送，运行中就是**停止**。
// 返回值就是那一条 POST 的 promise：调用方（与假 DOM 那一层）要能 `await` 到
// "请求真的发出去了、回话真的到了"，而不是只看按钮上那一瞬间的样子。
function onSendClick() {
  if (ctx.store.get('busy')) { return requestStop(); }
  return say();
}

// 手动中断这一轮：`POST /run/stop` → 服务端温和地收掉子进程（已产出的内容保留）。
// 点了立刻有反馈（按钮进"正在停止…"），真正回到可发送由 `final` 事件或下一拍
// `/state` 的 `running` 落回假决定；所以这里**不等**它，也不假装已经停了。
async function requestStop() {
  if (ctx.store.get('readonly') || ctx.store.get('stopping')) { return; }
  ctx.store.set('stopping', true);
  syncSend();
  const body = await ctx.transport.post('/run/stop', {});
  if (body === null) {
    // 被拒（409 没有在跑 / 403 只读）：理由已经由 post() 摆进对话区与通知里了。
    ctx.store.set('stopping', false);
    syncSend();
    return;
  }
  ctx.store.set('stoppedRun', true);
  ctx.thread.add('已请求停止本轮：服务端正在收尾，已产出的内容保留。', 'dim');
}

// 「正在思考」：由既有的 wait / start / final 驱动，不新造协议。
function setThinking(value) {
  ctx.store.set('thinking', Boolean(value));
  thinkingBox.classList.toggle('show', ctx.store.get('thinking') && !ctx.store.get('readonly'));
  syncSend();
}

function setMeta(steps) {
  meta.textContent = ctx.sessions.sessionTitle() + ' · ' + (steps || 0) + ' 步';
}

// 草稿按「工作区|会话」分开。内存是当前页的权威，sessionStorage 只为刷新留一份。
// 存储不可用或超限时静默退回内存，不把草稿发给服务端。
let shownKey = '';
const DRAFT_KEPT = '草稿还在输入框里，可以重试';
const DRAFT_STORAGE_PREFIX = 'spool:draft:';

function currentKey() {
  const target = (ctx.store.get('lastState') || {}).target || {};
  const root = target.root || '';
  const name = target.session || ctx.store.get('session') || '';
  if (!root && !name) { return ''; }
  return keyFor(root, name);
}

function draftStorageName(key) {
  return DRAFT_STORAGE_PREFIX + key;
}

function readStoredDraft(key) {
  try {
    if (typeof sessionStorage === 'undefined' || !sessionStorage || typeof sessionStorage.getItem !== 'function') {
      return null;
    }
    const value = sessionStorage.getItem(draftStorageName(key));
    if (value === null || value === undefined) { return null; }
    return String(value);
  } catch (err) {
    return null;
  }
}

function syncStoredDraft(key, text) {
  try {
    if (typeof sessionStorage === 'undefined' || !sessionStorage) { return; }
    const name = draftStorageName(key);
    if (text) {
      if (typeof sessionStorage.setItem === 'function') { sessionStorage.setItem(name, text); }
    } else if (typeof sessionStorage.removeItem === 'function') {
      sessionStorage.removeItem(name);
    }
  } catch (err) {
    // 配额满或隐私模式：这一份留在内存里，输入不受影响。
  }
}

function ensureStoredDraft(key) {
  const prev = ctx.store.get('drafts') || {};
  if (Object.prototype.hasOwnProperty.call(prev, key)) { return; }
  const stored = readStoredDraft(key);
  if (!stored) { return; }
  const drafts = Object.assign({}, prev);
  drafts[key] = stored;
  ctx.store.set('drafts', drafts);
}

function writeDraft(key, text) {
  if (!key) { return; }
  const prev = ctx.store.get('drafts') || {};
  const nextText = String(text || '');
  const had = Object.prototype.hasOwnProperty.call(prev, key);
  if (nextText) {
    if (!had || prev[key] !== nextText) {
      const drafts = Object.assign({}, prev);
      drafts[key] = nextText;
      ctx.store.set('drafts', drafts);
    }
    syncStoredDraft(key, nextText);
    return;
  }
  if (!had) { return; }
  const drafts = Object.assign({}, prev);
  delete drafts[key];
  ctx.store.set('drafts', drafts);
  syncStoredDraft(key, '');
}

function rememberDraft() {
  writeDraft(currentKey(), goalBox.value);
}

function restoreDraft() {
  const key = currentKey();
  if (!key || key === shownKey) { return; }
  // 输入框里还有字才顺手记下：乐观清空（还没成功）时不要用空串把草稿抹掉。
  if (shownKey && goalBox.value !== '') { writeDraft(shownKey, goalBox.value); }
  const leaving = shownKey;
  shownKey = key;
  ensureStoredDraft(key);
  const drafts = ctx.store.get('drafts') || {};
  if (Object.prototype.hasOwnProperty.call(drafts, key)) {
    const next = String(drafts[key] || '');
    if (goalBox.value !== next) {
      goalBox.value = next;
      autoGrow();
    }
    return;
  }
  if (leaving === '') {
    if (goalBox.value) { writeDraft(key, goalBox.value); }
    return;
  }
  if (goalBox.value !== '') {
    goalBox.value = '';
    autoGrow();
  }
}

function noteDraftKept() {
  ctx.thread.add(DRAFT_KEPT, 'dim');
}

function clearUnsent(bubble) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || !wrap.classList) { return; }
  wrap.classList.remove('failed');
  wrap._retrying = false;
  const kids = wrap.children || [];
  for (let index = kids.length - 1; index >= 0; index -= 1) {
    const cls = String(kids[index].className || '');
    if (cls.indexOf('msg-retry') >= 0 || cls.indexOf('unsent') >= 0) {
      wrap.removeChild(kids[index]);
    }
  }
}

function markUnsent(bubble, goal) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || !wrap.classList) { return; }
  wrap.classList.add('failed');
  let noted = false;
  let button = null;
  const kids = wrap.children || [];
  for (let index = 0; index < kids.length; index += 1) {
    const cls = String(kids[index].className || '');
    if (cls.indexOf('unsent') >= 0) { noted = true; }
    if (cls.indexOf('msg-retry') >= 0) { button = kids[index]; }
  }
  if (!noted) {
    const note = document.createElement('div');
    note.className = 'unsent';
    note.textContent = '未发送';
    wrap.appendChild(note);
  }
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'msg-retry';
    button.textContent = '重试';
    wrap.appendChild(button);
  }
  button.onclick = function (event) {
    if (event && event.stopPropagation) { event.stopPropagation(); }
    return retrySend(bubble, goal);
  };
}

async function retrySend(bubble, goal) {
  const wrap = bubble && bubble.parentNode;
  if (!wrap || wrap._retrying) { return; }
  wrap._retrying = true;
  const button = wrap.querySelector ? wrap.querySelector('.msg-retry') : null;
  if (button) { button.disabled = true; }
  const sent = await ctx.transport.post('/run', {goal: goal});
  wrap._retrying = false;
  if (button) { button.disabled = false; }
  if (!sent) { return; }
  if (sent.title) {
    ctx.sessions.applySessionTitle(ctx.store.get('session'), sent.title, sent.title_source || 'seed');
  }
  clearUnsent(bubble);
  ctx.thread.setPendingUser(goal);
  if (currentKey() && goalBox.value.trim() === goal) {
    goalBox.value = '';
    autoGrow();
    writeDraft(currentKey(), '');
  }
}

async function say() {
  if (ctx.store.get('readonly')) { return; }
  const original = goalBox.value;
  const goal = original.trim();
  if (!goal || ctx.store.get('busy')) { return; }
  const key = currentKey();
  ctx.sidebar.closeDrawer();
  goalBox.value = '';
  autoGrow();
  // 自己说的话就是自己说的：气泡底色 + 右对齐已经是"这条是你发的"。
  // 这一行原来在 goal 前面拼了一个黑色三角形符号——它什么也没多说，
  // 所以没了，也没有换成别的符号。
  const bubble = ctx.thread.add(goal, 'me');
  // 这一条已经画出来了；等 `/state` 把它送回来时（子进程开工第一件事就是写库）
  // 要认出来是同一句，不许画第二遍。失败则清掉，避免下一帧把没发出去的话认领掉。
  ctx.thread.setPendingUser(goal);
  const sent = await ctx.transport.post('/run', {goal});
  // 服务端在 /run 里先给一个种子标题；本地立刻换上，左栏不必等下一帧 state。
  if (sent && sent.title) {
    ctx.sessions.applySessionTitle(ctx.store.get('session'), sent.title, sent.title_source || 'seed');
  }
  if (sent) {
    if (currentKey() === key && goalBox.value === '') { writeDraft(key, ''); }
    return;
  }
  ctx.thread.setPendingUser('');
  markUnsent(bubble, goal);
  if (currentKey() !== key) {
    writeDraft(key, original);
    return;
  }
  if (goalBox.value === '') {
    goalBox.value = original;
    autoGrow();
    writeDraft(key, original);
    focus(goalBox);
  }
  noteDraftKept();
}

// Enter 发送 / Shift+Enter 换行 / Esc 收起状态详情。
function onKeydown(event) {
  if (event.key === 'Escape') { ctx.status.setStatusOpen(false); return; }
  if (event.key !== 'Enter') { return; }
  if (event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) { return; }
  event.preventDefault();
  say();
}

function autoGrow() {
  goalBox.style.height = 'auto';
  goalBox.style.height = Math.max(26, Math.min(goalBox.scrollHeight, 190)) + 'px';
}

function setGoal(value) {
  goalBox.value = String(value || '');
  autoGrow();
  rememberDraft();
  focus(goalBox);
}

function useExample(value) {
  setGoal(value);
}

let emptyNode = null;

function stillMounted(node) {
  let cur = node;
  while (cur) {
    if (cur === stream) { return true; }
    const parent = cur.parentNode;
    if (!parent || (parent.children || []).indexOf(cur) < 0) { return false; }
    cur = parent;
  }
  return false;
}

function contentOnStream() {
  const col = stream.querySelector('.col');
  if (!col) { return false; }
  const kids = col.children || [];
  for (let index = 0; index < kids.length; index += 1) {
    if (stillMounted(kids[index])) { return true; }
  }
  return false;
}

function workspaceName(state) {
  const target = (state && state.target) || {};
  const rows = (state && state.workspaces) || [];
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].root === target.root && rows[index].name) { return rows[index].name; }
  }
  return target.name || target.root || '';
}

function sessionLabel(state) {
  const target = (state && state.target) || {};
  if (target.session_title) { return String(target.session_title); }
  const rows = (state && state.sessions) || [];
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].id === target.session && rows[index].title) { return rows[index].title; }
  }
  return target.session || '';
}

function dropEmpty() {
  if (stillMounted(emptyNode)) { emptyNode.parentNode.removeChild(emptyNode); }
  emptyNode = null;
}

function pickEmpty() {
  const box = document.createElement('div');
  box.className = 'empty-state';
  const title = document.createElement('div');
  title.className = 'empty-title';
  title.textContent = '先选一个工作区和会话';
  const note = document.createElement('p');
  note.className = 'empty-note';
  note.textContent = '在左侧选工作区，再选一条会话。选好之后就可以给 Agent 派任务。';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'empty-go';
  button.textContent = '去选择工作区';
  button.onclick = function () { ctx.sidebar.openDrawer(); };
  box.appendChild(title);
  box.appendChild(note);
  box.appendChild(button);
  return box;
}

function readyEmpty(state) {
  const box = document.createElement('div');
  box.className = 'empty-state';
  const title = document.createElement('div');
  title.className = 'empty-title';
  title.textContent = '让 Agent 做点什么';
  const where = document.createElement('p');
  where.className = 'empty-where';
  where.textContent = workspaceName(state) + ' · ' + sessionLabel(state);
  const actions = document.createElement('div');
  actions.className = 'empty-actions';
  const samples = ['解释这个项目结构', '跑测试并修失败', '找我最近改过的文件'];
  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'empty-sample';
    button.textContent = sample;
    button.onclick = function () { useExample(sample); };
    actions.appendChild(button);
  }
  box.appendChild(where);
  box.appendChild(title);
  box.appendChild(actions);
  return box;
}

function syncEmpty() {
  const state = ctx.store.get('lastState') || {};
  const target = state.target || {};
  const live = ctx.thread.getLive();
  const show = !ctx.store.get('running') && !(live && live.done === false) && !contentOnStream();
  if (!show) { dropEmpty(); return; }
  const mode = target.root ? 'ready' : 'pick';
  const key = mode + '|' + (target.root || '') + '|' + (target.session || '')
    + '|' + workspaceName(state) + '|' + sessionLabel(state);
  if (emptyNode && stillMounted(emptyNode) && emptyNode.dataset.key === key) { return; }
  dropEmpty();
  emptyNode = mode === 'pick' ? pickEmpty() : readyEmpty(state);
  emptyNode.dataset.mode = mode;
  emptyNode.dataset.key = key;
  if (stream.firstChild) { stream.insertBefore(emptyNode, stream.firstChild); }
  else { stream.appendChild(emptyNode); }
}

  return { setRunning: setRunning, syncSend: syncSend, onSendClick: onSendClick, requestStop: requestStop, setThinking: setThinking, setMeta: setMeta, say: say, onKeydown: onKeydown, autoGrow: autoGrow, rememberDraft: rememberDraft, restoreDraft: restoreDraft, setGoal: setGoal, useExample: useExample, syncEmpty: syncEmpty };
}

// spool web · js/core/transport.js
// 事件流只连**一次**。切换目标**不重连**：服务端发现目标变了会退订旧的、
// 订阅新的、并补一帧 `state`（设计 §3.3），页面要做的只是把这一屏重绑到新目标上。
function createTransport(ctx) {
  function connect() {
    const source = new EventSource('/events');
    source.onmessage = onEvent;
    source.onerror = () => { conn.style.display = 'block'; };
    source.onopen = () => { conn.style.display = 'none'; };
  }

  // 一个 POST：成功返回响应体（`/target` 的响应体就是新的完整状态），
  // 失败**给出可读说明**：既摆在对话区里（记录），也**弹**一条只能手动关的错误通知
  // （不往控制台一丢了事：本地单机壳没人开着 devtools，"点了一下什么都没发生"
  // 是最坏的那种失败）。
  async function post(url, payload) {
    function rejectPost(why) {
      ctx.thread.add('请求被拒绝：' + why, 'bad');
      ctx.toasts.notifyFailure('请求被拒绝：', why, explainFailure(why, null));
      return null;
    }
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
    } catch (err) {
      return rejectPost('网络异常');
    }
    let detail;
    try {
      detail = await response.json();
    } catch (err) {
      return rejectPost('响应无法解析');
    }
    if (!response.ok) {
      return rejectPost(String((detail && detail.error) || response.status));
    }
    return detail;
  }

  return { connect: connect, post: post };
}

// spool web · js/main.js
// 执行顺序只认 static.JS_ORDER。这里不再写 import：打包时那些行会被删掉，
// 留着会让人以为改 import 顺序就能改运行顺序。
function applyState(state) {
  const target = state.target || {};
  const runningNow = Boolean(state.running);
  const chosen = store.get('chosen');
  const nextSession = target.session || state.session || store.get('session');
  const settled = (chosen.root === '' && chosen.session === '')
    || keyFor(chosen.root, chosen.session) === keyFor(target.root, target.session);
  const fields = {
    lastState: state,
    running: runningNow,
    busy: runningNow,
    session: nextSession,
  };
  if (!runningNow) {
    // 服务端说它已经不在跑了：停止请求那一档要落回去。服务端说不是停的
    // （`stopped` 假）时，"这一轮是被停掉的"也要清掉，不然下一轮的普通失败
    // 会被画成"已停止"。
    fields.stopping = false;
    if (!state.stopped) { fields.stoppedRun = false; }
  }
  if (settled) { fields.chosen = {root: '', session: ''}; }
  store.patch(fields);
  // 只读与"在不在跑"必须在 drawTarget **之前**定下来：右上角那两枚徽标
  // （只读 / 运行中）与那一枚键的两态都按它们算，晚一步就会画成上一帧的样子。
  app.status.showReadonly(state);
  // 目标先画：它变了就**整屏重绑**（服务端在目标变了时会补一帧 state，
  // 那一帧也要走这条重绑，不能只更新状态区）。
  if (app.sidebar.drawTarget(state)) { app.thread.resetView(); }
  app.sidebar.markChosen();
  const usage = state.usage || {};
  app.composer.setMeta(usage.steps);
  app.composer.setRunning(store.get('running'));
  // 断线重连时事件已经漏掉了，只能按快照把那张卡片整个重建出来。
  // 改动和命令走同一个入口：两边都有时合成一张卡，不能先画 diff 再被命令盖掉。
  // 两边都空时不动卡片（等命令授权时快照里可能还没有 diff）。
  const pendingItems = state.diffs || [];
  const asking = state.awaiting_detail || {};
  const commandDetail = state.awaiting && asking.command ? asking : null;
  if (pendingItems.length || commandDetail) {
    app.confirm.showApproval({diffs: pendingItems, commandDetail: commandDetail});
  }
  // `awaiting` 缺了 / 是空 = 服务端说"没有待确认内容"：卡片收掉（缺字段按没有处理）。
  if (state.awaiting) { app.status.showActions(); } else { app.confirm.clearPending(); }
  // 历史先画：`adoptMessage` 要用到"这一帧最后一条是不是助手那条"来决定
  // 哪张卡片该被认领。**认领之后再补实时卡片**，它的节点才会落在对话流最后。
  app.thread.drawHistory(state.messages);
  app.thread.restoreLive(state);
  app.thread.syncLiveTasks(state);
  app.tasks.sync(state);
  // ⑤ 可做的动作（问题④）：这一轮有可退的成果时，摆一枚**真能点**的
  // 「撤销这一轮」。刷新 / 切走再切回也照旧——它读的是快照里 `final` 上那个
  // 只增的 `revert_run_id`（服务端在 `final` 事件上给的，见 `run.py`）。
  app.confirm.showUndo(state.final);
  app.status.showPairings(state.pairings);
  app.status.showHealth(state.health);
  app.status.showElsewhere(state);
  // 设置弹层开着时，归档列表跟着状态走（恢复/移除后立刻消掉/加回）。
  if (store.get('settingsOpen')) { app.settings.renderArchived(); }
  // 首次打开一条有历史的会话：如果标题还是空的/种子，顺手请 Agent 起个名。
  app.sessions.maybeAgentTitle(false);
  app.composer.restoreDraft();
  app.composer.syncEmpty();
}

function onEvent(event) {
  const data = JSON.parse(event.data);
  if (data.type === 'state') { applyState(data); return; }
  // 点下「开始」到服务回话之间按钮还是可点的，再点一次会被拒（409）。
  // 拿事件里的 start 当权威信号，按钮状态就不会失真。
  if (data.type === 'start') {
    app.composer.setRunning(true);
    app.composer.setThinking(true);
    app.confirm.clearPending();
    // 这一轮的助手卡片**立刻**摆出来（占位"正在思考"）：回复到达时它会被替换成
    // 真正的回复——同一张卡片、同一个位置，不是另起一张。
    const stick = app.thread.atBottom();
    app.thread.ensureLive().turnGoal = String(data.goal || '');
    if (stick) { app.thread.scrollDown(); }
    app.thread.updateToBottom();
    return;
  }
  // 步骤 / 工具 / 系统说的一句话 / 等待心跳：全部进这张卡片的「思考过程」
  // （运行中自动展开；成功收尾后再收起）。
  if (data.type === 'section_start') {
    app.thread.addTraceLine({
      type: 'note',
      text: '第 ' + (Number(data.index) + 1) + '/' + data.total + ' 节 ' + (data.title || '')
    });
    return;
  }
  if (data.type === 'section_done') {
    app.thread.replaceSection(Number(data.index), String(data.text || ''));
    return;
  }
  if (data.type === 'delta' || data.type === 'final_delta') {
    // 正文流式：主气泡增量追加（final 再用完整文本覆盖，防漏片）。
    app.composer.setThinking(true);
    app.thread.appendDelta(String(data.text || ''));
    return;
  }
  if (data.type === 'step' || data.type === 'tool'
      || data.type === 'note' || data.type === 'wait') {
    // 工具回来就说明模型那一口气过去了，思考指示先灭；下一拍 wait 会再亮
    // ——同一件事的两种画法，不新造协议。
    if (data.type === 'tool') { app.composer.setThinking(false); }
    if (data.type === 'wait') { app.composer.setThinking(true); }
    app.thread.addTraceLine(liveTraceLine(data));
    return;
  }
  if (data.type === 'diff') {
    store.update('pendings', function (rows) {
      const next = (rows || []).slice();
      next.push({
        path: data.path,
        text: data.text,
        tool_call_id: String(data.tool_call_id || ''),
      });
      return next;
    });
    app.confirm.showApproval({
      diffs: store.get('pendings'),
      commandDetail: app.confirm.currentCommand(),
    });
    return;
  }
  if (data.type === 'await') {
    app.composer.setThinking(false);
    app.thread.addTraceLine(liveTraceLine(data));
    if (data.command) {
      app.confirm.showApproval({
        diffs: store.get('pendings') || [],
        commandDetail: data,
      });
    }
    app.status.showActions();
    return;
  }
  if (data.type === 'confirm') {
    // auto 策略下改动是静默落盘的，界面上必须留一句话，
    // 否则「它自己改了文件」这件事只有翻 git 才知道。
    if (data.auto) {
      app.thread.add('自动应用 ' + data.count + ' 处改动（策略 auto 且在授权范围内）', 'dim');
    }
    app.thread.addTraceLine(liveTraceLine(data));
    app.confirm.clearPending();
    return;
  }
  if (data.type === 'usage') {
    app.composer.setMeta(data.steps);
    // 用量是**详情**：进「思考过程」那一档，不进对话流（现场夹具：它原先紧跟在
    // 收尾报告后面，读起来像报告接着说的一句）。结局之后才到的那种，挂回刚刚
    // 那张助手卡片上——`ensureLive()` 会另起一张空占位卡，那就成了幽灵卡片。
    const usageLine = liveTraceLine(data);
    const card = app.thread.getLive();
    if (card !== null) { app.thread.pushTraceLine(card, usageLine); }
    else {
      const last = app.thread.lastAssistantCard();
      if (last !== null) { app.thread.pushTraceLine(last, usageLine); }
    }
    return;
  }
  if (data.type === 'final') {
    const stopped = store.get('stoppedRun');
    app.composer.setThinking(false);
    app.composer.setRunning(false);
    store.set('stoppedRun', false);
    // 有卡片就把**同一张卡片**更新成结局（占位被替换掉、回复不重复）；
    // 没有卡片（事件先于 start 到、老页面）才退回"另起一行"的老画法。
    if (!app.thread.finishLive(Boolean(data.ok), data.text, data.error, stopped)) {
      if (data.ok) {
        // 库里那条如果已经是这句回复（同一轮），就不再重复说一遍。
        if (!replyAlreadyVisible(data.text)) { app.thread.add('完成：' + data.text, 'ok'); }
      } else if (stopped) {
        app.thread.add('已停止：' + data.text, 'dim');
      } else {
        app.failure.showFailure('失败：', data.text, data.error);
      }
    }
    app.confirm.clearPending();
    // 这一轮有回退命令：把那个动作用**可点**的形式摆出来（用户不必去抄
    // 命令）。幂等：同一个 run_id 只摆一次（事件与快照各来一次也只摆一条）。
    app.confirm.showUndo(data);
    // 跑完了：请 Agent 用这一轮的目标与结论给会话起一个可读标题。
    app.sessions.maybeAgentTitle(true);
    return;
  }
  if (data.type === 'error') {
    app.composer.setThinking(false);
    // 中途的一条错误：进轨迹（事后回看得到），同时照旧弹出来 + 在对话流里留一块
    // ——失败必须看得见，收敛到"思考过程"里就等于藏起来了。
    app.thread.addTraceLine(liveTraceLine(data));
    app.failure.showFailure('错误：', data.message, data.error);
  }
}

// 这句回复是不是已经画在页面上了（快照比事件先到的情况）。
function replyAlreadyVisible(text) {
  const lastDrawn = app.thread.getLastDrawn();
  if (lastDrawn === null || lastDrawn.role === 'user') { return false; }
  return lastDrawn.content === String(text || '（没有产出）');
}

// 切换当前目标：`POST /target`。成功后**立刻按返回体重绑**（它就是新的 /state），
// 不等 SSE 补帧——补帧还会来，但页面不该有那一秒的旧内容。
//
// 点了就先亮选中态：回帧是一趟服务端往返，高亮不该落后于点击。探活**不在**那一趟里
// （服务端挪到后台了），所以这一步现在是几十毫秒——见 `web/probe.py`。
async function switchTarget(payload) {
  app.sidebar.closeDrawer();
  store.set('chosen', wantedKey(payload));
  app.sidebar.markChosen();
  const next = await app.transport.post('/target', payload);
  if (next === null) {
    // 没切成（409/400）：把选中态拨回当前目标，别停在假的选中态。
    store.set('chosen', {root: '', session: ''});
    app.sidebar.drawTarget(store.get('lastState'));
    app.sidebar.markChosen();
    return;
  }
  applyState(next);
}

// 这一次点的是谁（用在乐观选中上）。工作区那一列给的是**根路径**（下拉的值就是它）。
function wantedKey(payload) {
  const t = store.get('lastState').target || {};
  if (payload.workspace) { return {root: payload.workspace, session: payload.session || store.get('session')}; }
  if (payload.session) { return {root: t.root || '', session: payload.session}; }
  return {root: '', session: ''};
}

const app = createApp(store);
wireFeatures();

document.getElementById('menu').innerHTML = ICON.menu;
toBottomBtn.innerHTML = ICON.down;
if (settingsIcon) { settingsIcon.innerHTML = ICON.gear; }
if (settingsClose) {
  settingsClose.innerHTML = ICON.close;
  settingsClose.setAttribute('data-focusable', 'true');
}
if (settingsBtn) {
  settingsBtn.setAttribute('aria-expanded', 'false');
  settingsBtn.onclick = function () { app.settings.setSettingsOpen(true); };
}
if (settingsClose) {
  settingsClose.onclick = function () { app.settings.setSettingsOpen(false); };
}
if (settingsBox) {
  // 静态 HTML 里已经有 class="hidden"；这里再写一次，让假 DOM（不解析 class）
  // 与真实页面从同一个初始状态出发。
  settingsBox.classList.add('hidden');
  settingsBox.setAttribute('aria-hidden', 'true');
  settingsBox.onclick = function (event) {
    if (event && event.target === settingsBox) { app.settings.setSettingsOpen(false); }
  };
}
// 发送 / 停止那一枚键的图标**不在这里写**：它有三档（发送 / 停止 / 正在停止…），
// 唯一决定它长什么样的地方是 `syncSend()`（文件末尾的 `syncSend()` 会先画一遍）。

// 两条原生控件是"切目标"的唯一入口（左栏的行经 `target()` 落到它们身上）。
// 守卫盯着**当前目标**：派发 change 不会把同一个目标再切一次，也不会在
// 切换失败（409/400）之后被`drawTarget` 拨回时**再发一次**请求。
newBtn.onclick = app.sessions.newSession;
sendBtn.onclick = app.composer.onSendClick;
goalBox.oninput = function () {
  app.composer.rememberDraft();
  app.composer.autoGrow();
};
goalBox.onkeydown = app.composer.onKeydown;
stream.onscroll = app.thread.updateToBottom;
toBottomBtn.onclick = function () {
  app.thread.scrollDown();
  app.thread.updateToBottom();
};
document.getElementById('menu').onclick = function () {
  if (document.body.classList.contains('drawer')) { app.sidebar.closeDrawer(); } else { app.sidebar.openDrawer(); }
};
scrim.onclick = app.sidebar.closeDrawer;
toastAllBtn.onclick = app.toasts.closeAllToasts;

// 状态区：五枚徽标都是同一个开关——点开 / 收起详情面板（它们都在右上角那一列里）。
// 都在一个盒子里，所以"点外面"就是点到 `#corner` 之外（用 `closest` 判）。
for (const badge of [stHealth, stMode, stTarget, stPair, stElsewhere]) {
  badge.onclick = function () { app.status.setStatusOpen(!store.get('statusOpen')); };
}
document.onclick = function (event) {
  if (!store.get('statusOpen')) { return; }
  const node = event && event.target;
  const inside = node && node.closest && node.closest('#corner');
  if (!inside) { app.status.setStatusOpen(false); }
};
// Esc 也收起：焦点不一定在输入框里（点过徽标之后焦点就在按钮上），
// 所以除了 `goalBox.onkeydown` 那条，这里再挂一道。
document.onkeydown = function (event) {
  if (!event) { return; }
  // 重命名开着时先吃掉 Esc / Enter / Tab，避免落到设置、任务或抽屉上。
  if (store.get('renameOpen')) {
    if (event.key === 'Escape') {
      app.sessions.closeRename();
      return;
    }
    if (event.key === 'Enter') {
      if (event.preventDefault) { event.preventDefault(); }
      app.sessions.saveRename();
      return;
    }
    if (event.key === 'Tab') {
      const nodes = app.sessions.renameFocusables();
      if (nodes.length < 2) { return; }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }
    return;
  }
  if (store.get('settingsOpen')) {
    if (event.key === 'Escape') {
      app.settings.setSettingsOpen(false);
      return;
    }
    if (event.key === 'Tab') {
      const nodes = app.settings.settingsFocusables();
      if (nodes.length < 2) { return; }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }
    return;
  }
  if (store.get('tasksOpen')) {
    if (event.key === 'Escape' && !app.tasks.clearQuery()) { app.tasks.setTasksOpen(false); }
    return;
  }
  if (document.body.classList.contains('drawer') && event.key === 'Escape') {
    app.sidebar.closeDrawer();
    return;
  }
  if (event.key === 'Escape' && store.get('statusOpen')) { app.status.setStatusOpen(false); }
};

function wireFeatures() {
  app.toasts = createToasts(app);
  app.status = createStatus(app);
  app.settings = createSettings(app);
  app.sessions = createSessions(app);
  app.thread = createThread(app);
  app.tasks = createTasks(app);
  app.confirm = createConfirm(app);
  app.sidebar = createSidebar(app);
  app.composer = createComposer(app);
  app.failure = createFailure(app);
  app.transport = createTransport(app);
  app.applyState = applyState;
  app.switchTarget = switchTarget;
}

// 状态区先画一遍（"探活中"那枚小圆点 + 收起详情），等第一帧 `/state` 到位再对齐。
// `setStatusOpen(false)` 是**显式**收起的：面板的初始态不靠 HTML 里的类名，
// 免得脚本与页面对"现在开着没有"各说各话。
app.status.drawStatus();
app.status.setStatusOpen(false);
buildColumn();
app.composer.syncSend();
app.transport.connect();
setInterval(app.status.refreshStatus, 3000);
