// spool web · js/features/settings.js
import "../core/dom.js";
import "../core/store.js";
// 归档入口只露一枚数量，不把归档行摊在左栏；行本身只在设置弹层打开时才建。
function createSettings(ctx) {
function updateArchiveBadge(rows) {
  const count = (rows || []).length;
  if (archivedCount) {
    archivedCount.textContent = String(count);
    archivedCount.classList.toggle("hidden", count === 0);
  }
  if (archivedCountInline) { archivedCountInline.textContent = String(count); }
}

function renderArchived() {
  if (!ctx.store.get('settingsOpen') || !archivedList) { return; }
  const rows = (ctx.store.get('lastState') && ctx.store.get('lastState').sessions_removed) || [];
  updateArchiveBadge(rows);
  archivedList.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "archive-empty";
    empty.textContent = "没有已归档的会话。";
    archivedList.appendChild(empty);
    return;
  }
  for (const one of rows) {
    const row = document.createElement("div");
    row.className = "archive-row";
    const main = document.createElement("div");
    main.className = "archive-main";
    const title = document.createElement("div");
    title.className = "archive-title";
    title.textContent = ctx.sessions.sessionRowTitle(one);
    title.title = title.textContent;
    const sid = document.createElement("div");
    sid.className = "archive-id";
    sid.textContent = String(one.id || "");
    main.appendChild(title);
    main.appendChild(sid);
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "arc-restore";
    restore.textContent = '恢复';
    restore.setAttribute("data-focusable", "true");
    restore.disabled = ctx.store.get('readonly');
    restore.onclick = (function (value) {
      return function () { ctx.sessions.requestRestore(value); };
    })(one.id);
    row.appendChild(main);
    row.appendChild(restore);
    archivedList.appendChild(row);
  }
}

const THEME_KEY = 'spool:theme';

function readStoredTheme() {
  try {
    if (typeof localStorage === 'undefined' || !localStorage || typeof localStorage.getItem !== 'function') {
      return 'system';
    }
    const value = localStorage.getItem(THEME_KEY);
    if (value === 'dark' || value === 'light' || value === 'system') { return value; }
  } catch (err) { /* 存储不可用就当跟随系统 */ }
  return 'system';
}

function themeColor(theme) {
  if (theme === 'light') { return '#ffffff'; }
  if (theme === 'dark') { return '#212121'; }
  let light = false;
  try {
    light = Boolean(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
  } catch (err) { light = false; }
  return light ? '#ffffff' : '#212121';
}

function markThemeChoice(mode) {
  const pairs = [[themeSystem, 'system'], [themeDark, 'dark'], [themeLight, 'light']];
  for (const pair of pairs) {
    const button = pair[0];
    if (!button || typeof button.setAttribute !== 'function') { continue; }
    const on = pair[1] === mode;
    button.setAttribute('aria-checked', on ? 'true' : 'false');
    if (button.classList) { button.classList.toggle('on', on); }
  }
}

function applyTheme(theme) {
  const mode = theme === 'dark' || theme === 'light' ? theme : 'system';
  const root = document.documentElement;
  if (root && typeof root.setAttribute === 'function') {
    if (mode === 'system') {
      if (typeof root.removeAttribute === 'function') { root.removeAttribute('data-theme'); }
    } else {
      root.setAttribute('data-theme', mode);
    }
  }
  const meta = document.getElementById('theme-color');
  if (meta && typeof meta.setAttribute === 'function') {
    meta.setAttribute('content', themeColor(mode));
  }
  markThemeChoice(mode);
  return mode;
}

function setTheme(theme) {
  const mode = applyTheme(theme);
  try {
    if (typeof localStorage !== 'undefined' && localStorage && typeof localStorage.setItem === 'function') {
      localStorage.setItem(THEME_KEY, mode);
    }
  } catch (err) { /* 写不进也先把这一下画出来 */ }
  return mode;
}

function applyStoredTheme() {
  return applyTheme(readStoredTheme());
}

function wireTheme(button, value) {
  if (!button) { return; }
  button.onclick = function () { setTheme(value); };
}

function settingsFocusables() {
  if (!settingsBox) { return []; }
  const nodes = [settingsClose, themeSystem, themeDark, themeLight].concat(
    settingsBox.querySelectorAll ? settingsBox.querySelectorAll(".arc-restore") : []
  );
  return nodes.filter(function (node) {
    return node && !node.disabled && typeof node.focus === "function";
  });
}

function setSettingsOpen(open) {
  if (!settingsBox) { return; }
  const want = Boolean(open);
  if (want === ctx.store.get('settingsOpen')) { return; }
  ctx.store.set('settingsOpen', want);
  settingsBox.classList.toggle("hidden", !ctx.store.get('settingsOpen'));
  settingsBox.setAttribute("aria-hidden", ctx.store.get('settingsOpen') ? "false" : "true");
  if (settingsBtn) {
    settingsBtn.setAttribute("aria-expanded", ctx.store.get('settingsOpen') ? "true" : "false");
  }
  if (ctx.store.get('settingsOpen')) {
    ctx.store.set('lastSettingsFocus', document.activeElement || null);
    renderArchived();
    if (settingsClose && typeof settingsClose.focus === "function") {
      settingsClose.focus();
    }
  } else {
    const back = ctx.store.get('lastSettingsFocus');
    ctx.store.set('lastSettingsFocus', null);
    if (back && typeof back.focus === "function") { back.focus(); }
  }
}

  wireTheme(themeSystem, 'system');
  wireTheme(themeDark, 'dark');
  wireTheme(themeLight, 'light');
  applyStoredTheme();
  try {
    if (window.matchMedia) {
      const query = window.matchMedia('(prefers-color-scheme: light)');
      const follow = function () {
        if (readStoredTheme() === 'system') { applyTheme('system'); }
      };
      if (query && typeof query.addEventListener === 'function') {
        query.addEventListener('change', follow);
      }
    }
  } catch (err) { /* 没有媒体查询就保持启动时那一档 */ }

  return { updateArchiveBadge: updateArchiveBadge, renderArchived: renderArchived, settingsFocusables: settingsFocusables, setSettingsOpen: setSettingsOpen, setTheme: setTheme, applyStoredTheme: applyStoredTheme };
}
