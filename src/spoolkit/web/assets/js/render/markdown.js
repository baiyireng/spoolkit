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

export { parseMarkdown, hasBlockMarkdown, hasInlineMarkdown, safeHref, parseInlines };
