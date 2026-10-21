// spool web · js/features/status.js
import "../core/dom.js";
import "../core/store.js";
import "../core/format.js";
import "../render/failure.js";
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
