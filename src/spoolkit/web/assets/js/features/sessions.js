// spool web · js/features/sessions.js
import "../core/dom.js";
import "../core/store.js";
import "../core/transport.js";
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

// 一轮结束后请 Agent 给当前会话起个名。只在标题为空或仍是种子标题时做；
// 同一条会话每页只请求一次；失败就保留种子标题，不弹错、不阻塞对话。
async function maybeAgentTitle(force) {
  if (ctx.store.get('readonly') || ctx.store.get('running') || !ctx.store.get('lastState')) { return; }
  // 没有可命名的内容时别去打扰服务端（尤其是还没有跑过第一轮的新会话）。
  const hasContent = (ctx.store.get('lastState').messages || []).length > 0
    || (ctx.store.get('lastState').final && ctx.store.get('lastState').final.text);
  if (!force && !hasContent) { return; }
  const t = ctx.store.get('lastState').target || {};
  if (!t.root || !t.session) { return; }
  const key = t.root + "|" + t.session;
  if (titleAsked[key]) { return; }
  const rows = ctx.store.get('lastState').sessions || [];
  let row = null;
  for (const one of rows) { if (one.id === t.session) { row = one; break; } }
  const title = String((row && row.title) || t.session_title || "").trim();
  const source = String((row && row.title_source) || t.session_title_source || "");
  if (title && source !== "seed") { return; }
  titleAsked[key] = true;
  try {
    const response = await fetch("/session/title", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session: t.session}),
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
      applySessionTitle(t.session, detail.title,
        detail.source || (detail.generated ? "agent" : "seed"));
    }
  } catch (err) {
    // 标题是锦上添花：网络失败保留种子标题，不打扰用户。
  }
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
