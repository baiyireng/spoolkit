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
export { diffSummary };
