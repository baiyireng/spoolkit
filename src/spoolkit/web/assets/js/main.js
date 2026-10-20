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
      next.push({path: data.path, text: data.text});
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
