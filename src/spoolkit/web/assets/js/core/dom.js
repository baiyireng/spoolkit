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
const tasksFilters = document.getElementById('tasks-filters');
const tasksList = document.getElementById('tasks-list');
const tasksDetail = document.getElementById('tasks-detail');

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
