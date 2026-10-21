// spool web · js/features/thread.js
import "../core/dom.js";
import "../core/store.js";
import "../render/code.js";
import "../render/failure.js";
import "../render/trace_box.js";
import "../render/task_card.js";
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
let lastMessageDay = '';

function messageDate(item) {
  if (!item || item.created_at === undefined || item.created_at === null || item.created_at === '') {
    return null;
  }
  const stamp = Number(item.created_at);
  if (!Number.isFinite(stamp) || stamp <= 0) { return null; }
  const date = new Date(stamp * 1000);
  if (Number.isNaN(date.getTime())) { return null; }
  return date;
}

function pad2(value) {
  const number = Number(value) || 0;
  return number < 10 ? '0' + number : String(number);
}

function placeMessageTime(wrap, item) {
  const date = messageDate(item);
  if (!date || !wrap) { return; }
  const key = date.getFullYear() + '-' + date.getMonth() + '-' + date.getDate();
  if (key !== lastMessageDay) {
    const sep = document.createElement('div');
    sep.className = 'day-sep';
    const month = (date.getMonth() + 1) + '月' + date.getDate() + '日';
    sep.textContent = date.getFullYear() === new Date().getFullYear()
      ? month
      : date.getFullYear() + '年' + month;
    if (wrap.parentNode) { wrap.parentNode.insertBefore(sep, wrap); }
    lastMessageDay = key;
  }
  const time = document.createElement('time');
  time.className = 'msg-time';
  time.setAttribute('datetime', date.toISOString());
  time.textContent = pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  wrap.appendChild(time);
}

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
  placeMessageTime(wrap, item);
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
    lastMessageDay = '';
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
  lastMessageDay = '';
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
