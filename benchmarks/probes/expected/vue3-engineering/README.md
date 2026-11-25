# 后台管理前端（工程骨架）

Vue 3 + TypeScript + Vite + Vue Router + Pinia 的后台管理前端骨架。
这一轮只有工程结构，没有真实业务与后端。

## 目录结构

```
src/
  api/          请求层（统一 baseURL、超时、错误归一）
  assets/       静态资源与全局样式
  components/   可复用组件
  composables/  组合式函数
  router/       路由表
  stores/       Pinia 状态
  utils/        纯工具函数
  views/        页面
  __tests__/    单元测试
```

## 脚本

| 命令             | 作用                            |
| ---------------- | ------------------------------- |
| `pnpm install`   | 安装依赖                        |
| `pnpm dev`       | 本地开发（默认 127.0.0.1:5173） |
| `pnpm build`     | 类型检查 + 生产构建             |
| `pnpm preview`   | 预览构建产物                    |
| `pnpm typecheck` | 只做类型检查（应用 + 构建配置） |
| `pnpm lint`      | ESLint 检查                     |
| `pnpm format`    | Prettier 格式化                 |
| `pnpm test`      | 单元测试（Vitest + jsdom）      |

## 配置约定

- 路径别名 `@` → `src`：`vite.config.ts` 的 `resolve.alias` 与 `tsconfig.json` 的
  `paths` **两处必须一致**。
- 环境变量：以 `VITE_` 开头，`.env.example` 是模板，类型写在 `src/env.d.ts`。
- TypeScript 严格模式打开；`vue-tsc --noEmit` 必须零错误。
- CI（`.github/workflows/ci.yml`）顺序：install → lint → typecheck → test → build。

## 说明

后端接口尚未提供，`src/api/http.ts` 只做了一个可替换的 fetch 封装，
示例页面用的是本地假数据。
