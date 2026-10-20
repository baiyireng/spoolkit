// spool web · js/features/sidebar.js
import "../core/dom.js";
import "../core/store.js";
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
      removable
        ? actSpec('移除', 'remove', one.value, (function (value) {
            return function () { ctx.sessions.requestRemove(value); };
          })(one.value))
        : null,
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
      return {value: s.id, title: s.title || '', text: s.title || s.id};
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
