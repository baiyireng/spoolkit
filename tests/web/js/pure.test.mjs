import assert from "node:assert/strict";
import { test } from "node:test";

import { diffSummary } from "../../../src/spoolkit/web/assets/js/render/diff.js";
import {
  explainFailure,
  failureKind,
  redact,
} from "../../../src/spoolkit/web/assets/js/render/failure.js";
import { probedAgo, short } from "../../../src/spoolkit/web/assets/js/core/format.js";
import {
  hasBlockMarkdown,
  hasInlineMarkdown,
  parseInlines,
  parseMarkdown,
  safeHref,
} from "../../../src/spoolkit/web/assets/js/render/markdown.js";

test("标题、列表、粗体、链接、表格", () => {
  const blocks = parseMarkdown([
    "# 标题",
    "",
    "- **甲**",
    "- 乙",
    "",
    "1. 第一步",
    "2. 第二步",
    "",
    "| 名 | 值 |",
    "| --- | --- |",
    "| a | b |",
    "",
    "见 [说明](http://example.test/a)",
  ].join("\n"));
  assert.equal(blocks[0].type, "heading");
  assert.equal(blocks[0].level, 1);
  assert.equal(blocks[0].inlines[0].text, "标题");
  assert.equal(blocks[1].type, "ul");
  assert.equal(blocks[1].items[0][0].type, "strong");
  assert.equal(blocks[1].items[0][0].text, "甲");
  assert.equal(blocks[2].type, "ol");
  assert.equal(blocks[2].items.length, 2);
  assert.equal(blocks[3].type, "table");
  assert.deepEqual(blocks[3].header.map((cell) => cell.map((part) => part.text).join("")), ["名", "值"]);
  assert.deepEqual(blocks[3].rows[0].map((cell) => cell.map((part) => part.text).join("")), ["a", "b"]);
  assert.equal(blocks[4].type, "paragraph");
  const link = blocks[4].inlines.find((item) => item.type === "link");
  assert.equal(link.href, "http://example.test/a");
});

test("块级和行内判定", () => {
  assert.equal(hasBlockMarkdown("# 标题"), true);
  assert.equal(hasBlockMarkdown("普通一句话"), false);
  assert.equal(hasInlineMarkdown("这是 **粗**"), true);
  assert.equal(hasInlineMarkdown("没有标记"), false);
});

test("行内代码先于粗体和链接", () => {
  const parts = parseInlines("先跑 `spool status`，**注意** 看 [文档](https://example.test/docs)");
  assert.equal(parts.find((item) => item.type === "code").text, "spool status");
  assert.equal(parts.find((item) => item.type === "strong").text, "注意");
  assert.equal(parts.find((item) => item.type === "link").href, "https://example.test/docs");
  const shielded = parseInlines("保持 `a **b** [c](http://x)`");
  const code = shielded.find((item) => item.type === "code");
  assert.equal(code.text, "a **b** [c](http://x)");
  assert.equal(shielded.some((item) => item.type === "strong" || item.type === "link"), false);
});

test("标题列表表格里的行内代码", () => {
  const blocks = parseMarkdown([
    "## 结论",
    "",
    "- 先跑 `spool status`",
    "- **注意** 看 [文档](https://example.test/docs)",
    "",
    "| 项 | 值 |",
    "| --- | --- |",
    "| `a` | **b** |",
  ].join("\n"));
  assert.equal(blocks[0].type, "heading");
  assert.equal(blocks[0].level, 2);
  assert.equal(blocks[0].inlines[0].text, "结论");
  assert.equal(blocks[1].type, "ul");
  assert.equal(blocks[1].items[0].find((item) => item.type === "code").text, "spool status");
  assert.equal(blocks[1].items[1].find((item) => item.type === "strong").text, "注意");
  assert.equal(blocks[1].items[1].find((item) => item.type === "link").text, "文档");
  assert.equal(blocks[2].type, "table");
  assert.equal(blocks[2].rows[0][0][0].type, "code");
  assert.equal(blocks[2].rows[0][0][0].text, "a");
  assert.equal(blocks[2].rows[0][1][0].type, "strong");
  assert.equal(blocks[2].rows[0][1][0].text, "b");
});

test("代码围栏不交给块级解析", () => {
  const src = ["## 标题", "", "```", "# not a heading", "```", "", "- item `code`"].join("\n");
  const parts = src.split("```");
  const prose = parts.filter((_, index) => index % 2 === 0).join("\n");
  const blocks = parseMarkdown(prose);
  assert.equal(parts[1].includes("# not a heading"), true);
  assert.equal(blocks.some((block) => (
    block.type === "heading" && block.inlines.some((item) => String(item.text).includes("not a heading"))
  )), false);
  assert.equal(blocks[0].type, "heading");
  assert.equal(blocks[1].items[0].find((item) => item.type === "code").text, "code");
});

test("链接只留 http(s) 和 mailto", () => {
  assert.equal(safeHref("javascript:alert(1)"), "");
  assert.equal(safeHref("data:text/html,hi"), "");
  assert.equal(safeHref("//example.test"), "");
  assert.equal(safeHref("http://example.test/a"), "http://example.test/a");
  assert.equal(safeHref("mailto:a@example.test"), "mailto:a@example.test");
});

test("时间与截断", () => {
  assert.equal(short("abcdef", 3), "abc…");
  assert.equal(short("ab", 3), "ab");
  assert.equal(probedAgo({ age_s: 0 }), "（刚刚）");
  assert.equal(probedAgo({ age_s: 5 }), "（5 秒前）");
  assert.equal(probedAgo({ age_s: 120 }), "（2 分钟前）");
  assert.equal(probedAgo({}), "（时间戳缺失）");
});

test("diff 摘要不计文件头", () => {
  const text = "+++ a\n--- b\n+one\n-two\n context";
  assert.equal(diffSummary(text), "+1 -1 行");
});

test("store 的 get/set/subscribe 与 patch 只通知一次", async () => {
  const { createStore } = await import(
    "../../../src/spoolkit/web/assets/js/core/store.js"
  );
  const box = createStore({ running: false, chosen: null, lastState: null, busy: false });
  const seen = [];
  const stop = box.subscribe((keys, partial) => {
    seen.push([keys, partial]);
  });
  box.set("running", true);
  assert.equal(box.get("running"), true);
  assert.deepEqual(seen, [[["running"], { running: true }]]);
  box.set("running", true);
  assert.equal(seen.length, 1);
  box.set("chosen", { root: "a", session: "b" });
  assert.deepEqual(box.get("chosen"), { root: "a", session: "b" });
  box.set("lastState", { target: { session: "b" } });
  assert.equal(box.get("lastState").target.session, "b");
  assert.equal(box.set("other", 1), 1);
  assert.equal(box.get("other"), undefined);
  assert.equal(seen.length, 3);
  let patches = 0;
  const stopPatch = box.subscribe(() => { patches += 1; });
  box.patch({ running: false, busy: true, other: 9 });
  assert.equal(patches, 1);
  assert.equal(box.get("running"), false);
  assert.equal(box.get("busy"), true);
  assert.equal(box.get("other"), undefined);
  stopPatch();
  stop();
  box.set("running", true);
  assert.equal(box.get("running"), true);
  assert.equal(seen.length, 4);
});

test("失败说明遮蔽密钥并带上地址", () => {
  assert.equal(redact("sk-abcdefghijklmnop"), "sk-abcd***");
  assert.match(redact("Bearer abcdefgh"), /Bearer \*\*\*/);
  assert.equal(failureKind("ConnectTimeout"), "connect");
  const explained = explainFailure("ConnectTimeout while dialing", {
    kind: "connect",
    url: "http://127.0.0.1:9",
  });
  assert.ok(explained);
  assert.match(explained.head, /连接模型/);
  assert.match(explained.lines[0], /127\.0\.0\.1:9/);
  assert.equal(explained.raw.includes("sk-"), false);
});
