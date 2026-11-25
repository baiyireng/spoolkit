// 只检查 Python 打出来的经典脚本能被 esbuild 解析。
// 不在这里重新打包：esbuild 会收成严格模式作用域，共享的 let 赋值会坏。

import * as esbuild from "esbuild";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "src", "spoolkit", "web", "assets", "dist");
const js = readFileSync(join(dist, "app.js"), "utf8");
const css = readFileSync(join(dist, "app.css"), "utf8");

await esbuild.transform(js, { loader: "js", target: "es2022" });
await esbuild.transform(css, { loader: "css" });

if (js.includes("__spoolTest") || js.includes("test-entry")) {
  console.error("生产包里混进了测试入口");
  process.exit(1);
}

console.log("esbuild parsed dist/app.js and dist/app.css");
