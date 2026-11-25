# 交接：心跳落地、weft 长文、多模态

日期：2026-09-24  
仓库：`D:\workSpace\agents_dev`（spoolkit）  
对照：`D:\workSpace\weft\docs\SPOOLKIT.md`

本文是内部进度，不是对外手册。不替代设计 spec；未拍板的选项标成「未决」。

---

## 1. 接手后做了什么

### 1.1 对外文档（已进 `main`，已推过一部分）

对齐 hub / service / 假模型口径：README、上手、CLI、手册、通道、dogfooding。  
`main` 相对 `origin/main` **超前 2 个提交**（含心跳**设计** `68b363e`、忽略 `.worktrees` 的 `412e639`）。心跳**代码**不在 `main`。

### 1.2 模型等待心跳（已实现，未并进 `main`）

目标：llama.cpp `stream: false` 时，人能看见「还在等」，不改超时、不接 SSE。

| 项 | 内容 |
|---|---|
| 分支 | `feat/model-wait-heartbeat` |
| worktree | `D:\workSpace\agents_dev\.worktrees\model-wait-heartbeat` |
| HEAD | `c980fe2` |
| spec | `docs/superpowers/specs/2026-09-21-model-wait-heartbeat-design.md`（feat 上标「已定，已实施」；`main` 上仍是较早稿） |
| 计划 | `docs/superpowers/plans/2026-09-22-model-wait-heartbeat.md`（feat 已提交；`main` 工作区可能还有一份未跟踪拷贝） |

提交：

1. `8a7eb99` 实施计划  
2. `01784d9` `WaitingGateway`：满 15s 心跳，`Event.wait` 可中断  
3. `46f90e7` 协议登记 `wait`  
4. `6b8476c` `provider_gateway` 包装；run / plan / chat 绑 `bind_model_wait`  
5. `a5ac16b` 网页灰字；不改 `_absorb`、不用探活红横幅  
6. `c980fe2` 对外说明回写  

行为：散文走 stderr；`--events` 发 `wait` JSON；QQ 不每 15 秒推手机。  
测试：心跳相关 **73 passed**。整仓库套件在嵌套 worktree 里会误碰到父目录 `.agent`，那批失败**不是**心跳逻辑本身。  
审查：整分支 **Ready to merge: Yes**。你选过「1 本地并进 main」，切工作区被中断，**合并未执行**。worktree 里有 `.pytest-tmp` 和未提交 `uv.lock`，删树前要清掉。

### 1.3 T8 / T5 / weft（只设计对话，未写产品代码）

曾打算 T8 本地切块 + T5 多模态同一窗口、两张 spec。你后来改为：**长文走 weft，不要在 spool 再造一套切块。**

本机模型文件（已确认存在）：

- `C:\Users\32615\Downloads\Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf`（约 8.2G）
- `C:\Users\32615\Downloads\mmproj-Qwen3.8-27B-BF16.gguf`（约 889MB）

已锁定：

- spool **不** `import weftkit`；只 HTTP，默认 `http://127.0.0.1:8770`，建议 `WEFT_BASE`。
- 图 **不进 weft**（库无隔离；运行时图多为临时信息）。
- 图用工具 `read_image(path)`：这一轮 `chat()` 带像素，下一轮丢掉，只留「已看过 path」；要再看就再调。
- 未挂 `--mmproj` 时明确说不能看图，不要把像素塞进纯文本。

未决（你问「Agent 会不会自动调 weft」之后还没选）：

- Agent 是否只问不写（现协议），还是允许点名入库，还是等 weft 会话隔离再自动 ingest。
- 工作区里**尚未 ingest** 的大手册：继续整份 `read_file`、超阈值拒绝、或仍做 spool 本地 T8。

---

## 2. 接下来要做什么

建议顺序。前两项是收尾，后两项是新功能。

### 2.1 把心跳并进 `main`（未做完的选项 1）

1. 在 `D:\workSpace\agents_dev` 上 `git checkout main`。  
2. 若存在未跟踪的计划文件与即将合并进来的同路径冲突，先挪走或删掉拷贝。  
3. `git merge feat/model-wait-heartbeat`。  
4. 在 **main 工作区**（不要在嵌套 worktree 里）跑 pytest。  
5. `git worktree remove` 那个心跳 worktree，再 `git branch -d feat/model-wait-heartbeat`。  
6. 未要求则不要推远程。

dogfood 27B 时最好带着心跳，否则又会「等几分钟零输出」。

### 2.2 拍板 weft 写入权（挡着长文 spec）

三选一：

| 选项 | 含义 |
|---|---|
| A | 只问不写：人 `weft ingest`，Agent 只 `ask_corpus` / `read_chunk`（与 `SPOOLKIT.md` 一致） |
| B | Agent 可对**用户点名的一个文件**请求入库（weft 要开受控接口；默认库仍无隔离） |
| C | 先做 weft 会话级隔离，再让 Agent 自动 ingest 超长本地文件；spool 等隔离就绪再接 |

未入库工作区手册的 A/B/C（照旧 `read_file` / 拒绝 / spool T8）依赖这一项。

### 2.3 spec → 计划 → 实现：spool 接 weft（长文）

协议已写在 weft 仓，spool 尚未挂工具。

必做：`ask_corpus` → `POST /v1/ask`（键名：`doc`/`chunk`/`continue`，不要写成 `doc_id`/`index`/`continue_`）。  
建议：`read_chunk` → `GET /v1/chunk`。可选：`list_docs`。  
禁止给 Agent：`/v1/chat`、`/v1/probe`、`/v1/ingest`、删除。  
失败：连不上 →「资料库不可用」，不编造；无命中 → `ok==true` 且 `citations==[]`，转述 `answer`/`uncertain`。  
测试：假 HTTP，不启真 `weft serve`、不打真 llama-server。  
可选：`[[service]]` 配方一键 `weft serve`（端口 **8770**，不要和 spool 网页壳 **8765** 抢）。

### 2.4 spec → 计划 → 实现：`read_image` + `--mmproj`（临时图）

- `Message.content` 今日仍是纯字符串，要能带一轮图像 part。  
- `LlamaCppGateway` 走 OpenAI 兼容多模态；`stream` 仍为 false。  
- `spool service` 配方加上你这两份 GGUF 的 `--mmproj`（**不要写进仓库**，路径在用户配置）。  
- 历史里不保留像素；不写 `memory.md`；不进 weft。  
- Gemini 这刀可以不做，或明确「无图则当纯文本」。

### 2.5 明确不做（本阶段）

- 在 spool 仓建 `src/weftkit`。  
- 把图或临时截图 ingest 进 `.weft/`。  
- SSE / 把 token 打到屏幕（心跳那刀已否决）。  
- 浏览器级验证、weft 独立仓的 RP / 写回 spool 记忆。

---

## 3. 可能存在的问题

### 心跳

- **未合并**：`main` 跑 27B 仍可能长时间无输出。  
- **嵌套 worktree 测不准**：`.worktrees/` 在仓库内，向上找会命中父级 `.agent`；子进程偶发用到系统 Python 找不到 `spoolkit`。合并后在主工作区复测。  
- **次要债**（不挡合入）：`run.py` 的 wait 监听器没共用后续 `EventWriter`（两边默认都是 stdout）；入口接线无集成测；心跳线程与主线程并发写 stdout 无锁。

### weft 长文

- **Agent 默认不能入库**：没有人先 `weft ingest`，`ask_corpus` 只能问到空库。这和「Agent 会不会自动处理长文」的预期容易打架。  
- **无数据隔离**：自动 ingest 可能把密钥、日志、半成品写进同一 `.weft/`。你已因此否决「图进库」。  
- **工作区手册仍会撑爆窗口**：若选「只问不写」且没人 ingest，大 `README` 仍走整份 `read_file`。原 T8 就是为这个。  
- **键名易写错**：`continue` vs `continue_`；ask 的 `chunk` vs GET chunk 的 `index`。  
- **weft 与 spool 抢端口 / 抢模型**：weft 默认 embeddings/`--base-url` 也是 `127.0.0.1:8080`。

### 多模态

- **显存**：8.2G IQ2_XS + 889MB BF16 mmproj，和 weft 向量、对话模型同卡时可能挤爆。可能需要两套 `[[service]]`（纯文本 vs 带 mmproj），而不是永远挂投影器。  
- **图只活一轮**：实现上要在工具结果进历史之前剥掉图像 part，只留短说明；漏剥会把上下文和显存打满。  
- **编码与上限**：走 llama-server 的 image_url / base64 形态、分辨率、张数都还没定；假模型测试不能当视觉能力。  
- **通道**：QQ/网页暂不附带图（已选只要 `read_image`）；用户从聊天丢图进来会落空，除非以后加 C。

### 流程与仓库

- Cursor 工作区若仍停在心跳 worktree，合并删树会把当前根拆掉，需先回到 `D:\workSpace\agents_dev`。  
- 公开文档不要写内部路线图文件名。  
- weft 与 spool 两仓、两进程；改协议要改 weft 的 `SPOOLKIT.md` / `PROTOCOL.md`，不要只改 spool。

---

## 4. 建议你先拍的两件事

1. **心跳**：是否仍按「本地并进 main」做完（推荐，便于 27B dogfood）。  
2. **weft 写入**：A 只问不写 / B 点名入库 / C 先隔离再自动 ingest。

拍板后按 2.3、2.4 各写一张 spec，再施工。
