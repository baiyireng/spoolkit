// spool web · js/features/toasts.js
import "../core/dom.js";
import "../core/store.js";
import "../render/failure.js";
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
