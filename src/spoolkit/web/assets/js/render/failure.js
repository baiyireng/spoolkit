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
export { redact, explainFailure, failureKind, failureCause, failureNext, usefulLine };
