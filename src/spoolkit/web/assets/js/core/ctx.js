// spool web · js/core/ctx.js
// 装配根创建的那一个实例。feature 工厂用参数接住它，不读全局表。
function createApp(store) {
  return { store: store };
}
