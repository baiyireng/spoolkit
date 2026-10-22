// spool web · js/core/format.js
function keyFor(root, name) {
  return (root || '') + '|' + (name || '');
}

function short(text, limit) {
  const line = String(text || '');
  return line.length > limit ? line.slice(0, limit) + '…' : line;
}
// 「这是什么时候探的」必须说得出：探活挪到后台之后，`/state` 给的是**最近一次**
// 结果，可能是几秒前那一份。陈旧值可以接受，**静默的**陈旧值不行——不然一个
// 几分钟前的结论看起来就像刚刚测的。用服务端算好的 `age_s`（两边的墙钟不保证
// 同步，页面拿它换算成"3 秒前"就行），页面只负责给出可读说明。
function probedAgo(health) {
  const info = health || {};
  const age = info.age_s;
  // 既没有 `age_s` 也没有 `probed_at`：**不说**"未探测"——有结果而没时间戳时
  // 那句话会和"状态：正常"打架。照实说时间戳缺失。
  if (age === null || age === undefined) {
    return info.probed_at ? '' : '（时间戳缺失）';
  }
  const seconds = Math.max(0, Math.round(Number(age)));
  if (seconds < 2) { return '（刚刚）'; }
  if (seconds < 60) { return '（' + seconds + ' 秒前）'; }
  return '（' + Math.round(seconds / 60) + ' 分钟前）';
}
export { keyFor, short, probedAgo };
