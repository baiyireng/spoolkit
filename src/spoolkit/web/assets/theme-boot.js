// 样式表之前执行：先写上 data-theme，避免先画成另一档再跳过去。
(function () {
  var theme = 'system';
  try {
    if (typeof localStorage !== 'undefined' && localStorage && localStorage.getItem) {
      theme = localStorage.getItem('spool:theme') || 'system';
    }
  } catch (err) { theme = 'system'; }
  var root = document.documentElement;
  if (theme === 'dark' || theme === 'light') { root.setAttribute('data-theme', theme); }
  else { root.removeAttribute('data-theme'); }
  var color = '#212121';
  if (theme === 'light') { color = '#ffffff'; }
  else if (theme !== 'dark') {
    try {
      if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        color = '#ffffff';
      }
    } catch (err) { color = '#212121'; }
  }
  var meta = document.getElementById('theme-color');
  if (meta) { meta.setAttribute('content', color); }
})();
