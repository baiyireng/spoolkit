"""能力标定值的登记表。

系统里有六十来个数字常量，它们不是一类东西。判据只有一句（见设计文档 2.7）：

> **这个数字只会因为模型更强而被突破吗？**

是 → 它是**能力标定**，必须可调，而且默认不该压低——同一个数字用在强模型上
就是天花板。外部 API 的窗口、预算与判断力都比本机小模型强得多，替它砍掉
能力是纯损失。

防的是不可逆后果 → 它是**安全边界**，写死，不提供覆盖入口。

这张表的作用不是「集中管理」这种好听的话，而是回答两个具体问题：

1. **现在生效的是多少、从哪来**——一条看不见来源的限制，用起来和写死的
   限制一样难受：你不知道该不该动它、动到多少合适。
2. **不改代码能不能动它**——能力标定的值可以按工作区覆盖
   （`.agent/limits.json`），试一版不用改代码、也不用重装。

安全边界也列在这里，但标成不可覆盖：把「哪些能改」摆明，比让人去猜强。
"""

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Mapping

CAPABILITY = "capability"
MECHANICAL = "mechanical"
SAFETY = "safety"


@dataclass(frozen=True)
class Knob:
    """一个标定值。"""

    name: str
    default: float
    kind: str
    note: str

    @property
    def overridable(self) -> bool:
        return self.kind != SAFETY


# 默认值是**按本机 27B + 20 步预算**实测出来的那一套。换模型时该动的就是
# 这里面的 capability 项——这也是它们必须可覆盖的理由。
KNOBS: tuple[Knob, ...] = (
    Knob(
        "repeat_warn_at", 2, CAPABILITY,
        "同一个调用连续重复几次开始提醒（实测 7B 会连续 10 次一字不差）",
    ),
    Knob(
        "repeat_block_at", 3, CAPABILITY,
        "重复到几次不再执行：第三次中间没有任何别的动作，结果不可能变",
    ),
    Knob(
        "repeat_intervene_at", 4, CAPABILITY,
        "重复到几次升级给督导（机械层管不住的那部分交给判断）",
    ),
    Knob(
        "no_edit_limit", 4, CAPABILITY,
        "连续几步不提出改动就收窄成只能写（实测模型对文字提醒免疫）",
    ),
    Knob(
        "no_edit_intervene_at", 8, CAPABILITY,
        "漫游到几步升级给督导：每步换一个查询的绕法，机械计数器抓不到。"
        "**只看不写本身不算打转**（见 no_edit_explore_grace）：这条只兜"
        "「读了很多步却一步新信息都没进来」",
    ),
    Knob(
        "no_edit_explore_grace", 1, CAPABILITY,
        "「连续只看不写」到干预线时，要不要先看**看的那几步有没有拿到新信息**。"
        "1 = 最近 no_edit_look_window 步里有任何一次**成功**的查看，"
        "就按正常探索处理：交给督导的措辞变成「它还在探索，判断该不该继续」，"
        "而不是「它只看不写」。依据：dogfood 那条「优化一下旧文件的代码」轨迹里，"
        "它连读 4 步（本来要先读再计划）就被判成打转、督导直接建议收手——"
        "把正常的先读后计划当成原地打转。0 = 退回老行为（只看不写即打转）",
    ),
    Knob(
        "no_edit_look_window", 6,
        CAPABILITY,
        "上面那条往回看几步。它是能力标定值：上下文越长/模型越强，"
        "「还在探索」的窗口可以放宽；取 6 与 RECENT_WINDOW 同量级",
    ),
    Knob(
        "failed_repeat_at", 2, CAPABILITY,
        "同一个动作**连续失败**几次就升级给督导。它是「同一失败动作反复重试」"
        "这条判据的 Mech 阈值——也就是用户说的「后一种才算打转」。"
        "取 2 = 第一次原样重试就交给判断：mechanical 层已经硬拦了原样重提"
        "（failed_repeat_block_at），这一步只是把「它在同一个坑里反复试」"
        "作为**给督导的问题**说出来，不再是「只看不写」这种含糊信号。"
        "调 0 = 关掉这条升级路径",
    ),
    Knob(
        "empty_turn_limit", 4, CAPABILITY,
        "连续几个空回合判它卡死并收尾（协议里没有「我在等你确认」的表达）",
    ),
    Knob(
        "step_ceiling_factor", 8, CAPABILITY,
        "督导续期的总上限 = 基础步数 × 这个倍数（安全线，不是任务预算）",
    ),
    Knob(
        "failed_repeat_block_at", 2, CAPABILITY,
        "同一个调用**失败之后**，第几次原样重提开始硬拦。依据：dogfood 里 write_file "
        "被拒后模型一字不改地又提了一次，白吃一步、也白等一次模型生成；"
        "失败过的参数重提不会得到别的结果，所以默认 2 = 第一次重提即拦。"
        "调 3 = 允许它再试一次（文件可能被别的动作改过，那时指纹会在成功改动后清零）",
    ),
    Knob(
        "supervisor_max_grant", 20, CAPABILITY,
        "督导一次最多给多少步：给太多等于取消上限",
    ),
    Knob(
        "explore_grant_steps", 3, CAPABILITY,
        "督导说「这是正常探索」却给了 stop 时，我们替它续几步。"
        "依据：本机 27B 复跑「优化一下旧文件的代码」（预算 3 步）时，它的 reason "
        "写的是「任务处于正常探索阶段，尚未提出改动……总步数预算充足（剩余 21 步）」，"
        "action 却是 stop——自己说的和做的是两件事，而 stop 把一次尚未开始的探索判死。"
        "取 3 = 够它落到一处改动上，不是给出一轮新预算；调 0 = 关掉这条兜底",
    ),
    Knob(
        "output_boost_min_lines", 120, CAPABILITY,
        "目标产出达到这么多行时，**一开始**就把输出预留抬到 boost 档（不等截断）。"
        "依据：dogfood 实测，一份 120 行的整份产出在 0.15 档（本机 8K 窗口 "
        "≈ 1225 token）就是不够——那一次被截断、重试才拿到 2455，而重试的那一轮"
        "有可能丢掉 rewrite_reason 参数（丢了就被护栏拒掉、卡在确认通道外面）。"
        "阈值取 120 就是这次实测的读数：120 行装不进 0.15 档。"
        "**本实现初值**：调大 = 更晚才抬，调 0 = 关掉（退回「截断后才抬」的老行为）",
    ),
    Knob(
        "step_lines_per_bonus", 60, CAPABILITY,
        "**步数预算随产出规模放大**：目标文件每多少行给 1 步额外预算。"
        "依据：本仓 dogfood 实测，303 行的两份 HTML 合并成单文档这种任务，"
        "10 步基线用满仍然失败（模型被护栏挡住后原样重试），而整份产出的速度"
        "按 303 行 ÷ 10 步 ≈ 30 行/步量级，取 60 = 留一倍余量。"
        "**本实现初值，不是测出来的数**：调大 = 更省步数，调小 = 给得更多",
    ),
    Knob(
        "step_lines_bonus_cap", 12, CAPABILITY,
        "上一条最多补几步。没有它，一个万行文件会把步数上限抬到没有边界——"
        "而「一次跑多久」的天花板必须存在（与 step_ceiling_factor 同一性质）",
    ),
    Knob(
        "blocked_refund_limit", 3, CAPABILITY,
        "被硬拦（原样重试、大重写被拒）的动作最多退还几步预算。"
        "被拦的动作没有任何产出，不该白吃预算；但退还要有上限，"
        "否则模型可以靠不断被拦把预算变成无限。依据：dogfood 那次两次被拒的 "
        "write_file 白吃了 2 步。0 = 不退还（退回老行为）",
    ),
    Knob(
        "rewrite_stage_limit", 3, CAPABILITY,
        "同时最多暂存几份「已声明、清单还没确认完」的重写正文。依据：一次运行"
        "里真正会大改的文件通常个位数（本仓 dogfood 就 1 份 snake.html），"
        "3 份足够覆盖「同时合并两个文件」，而条数上界正是暂存区不被当垃圾场的"
        "第一道闸——没有它，模型反复提重写就能把内存填满。满了不是静默丢："
        "话术会点名「先生效或放弃已有暂存」",
    ),
    Knob(
        "rewrite_stage_chars", 400000, CAPABILITY,
        "**单份**暂存正文最多多少字符（≈ 一个 8000 行的文件）。依据：要暂存的"
        "是模型已经在一次输出里交出来的东西，而输出预算按 token 算（本机 8K 窗口"
        "下最多 2457 token），所以这个上界实际上够不着；它是防「把整个大文件当"
        "一次重写塞进来」的安全网，不是性能参数",
    ),
    Knob(
        "rewrite_stage_total_chars", 800000, CAPABILITY,
        "全部暂存正文的**合计**上界（等于条数上界 × 单份上界的一半）。"
        "单独设一条的理由：条数上界挡不住「3 份都贴着单份上限」这种组合，"
        "而内存只认总字节",
    ),
    Knob(
        "rewrite_stage_ttl_seconds", 1800, CAPABILITY,
        "暂存令牌的时效（秒）。依据：本仓 dogfood 一次运行实测 315.9s 墙钟，"
        "取 1800 = 6 倍余量——一次运行内足够用，运行结束后暂存随进程消失。"
        "过期即失效并清理（时效本身就是上界的一部分），令牌失效会给"
        "「怎么重新发起」的人话，不静默当成功、也不静默丢弃",
    ),
    Knob(
        "name_cycle_window", 12, CAPABILITY,
        "工具名短环看最近多少步（按本机 27B / 8K 标定）",
    ),
    Knob(
        "name_cycle_at", 1, CAPABILITY,
        "检测到工具名短环即升级给督导（1 = 有环就叫）",
    ),
    Knob(
        "oscillation_at", 1, CAPABILITY,
        "同路径内容来回覆盖几次就升级给督导",
    ),
    Knob(
        "same_query_at", 3, CAPABILITY,
        "同一检索词出现几次就升级给督导",
    ),
    Knob(
        "done_inline", 8, CAPABILITY,
        "状态块里「已完成」列几条；全量在 .agent/progress.md（每轮注入 = 每个请求的税）",
    ),
    Knob(
        "max_scopes", 5, CAPABILITY,
        "一次改多处时最多逐处验几个范围：再散，验证比任务本身还贵",
    ),
    Knob(
        "max_targets", 10, CAPABILITY,
        "一次派发的绝对上限（比 review_limit 更宽，给强模型留的余地）",
    ),
    Knob(
        "review_limit", 5, CAPABILITY,
        "派发超过几件就在结果里提醒「审查会吃力」。**只是提醒，不是上限**："
        "曾经按「大批次审不出结论」改成硬拦，那条证据后来被推翻了——真因是"
        "督导缺少 finish 结论，修好后同一批 9 件连过两次",
    ),
    Knob(
        "review_fix_rounds", 0, CAPABILITY,
        "「不许宣称完成」时先自动修几轮（设计稿 §3.1 处理顺序第 1 条）。"
        "**默认 0 = 关**：自动修要**再花一轮模型调用**，短预算的运行（探针的 "
        "8 步、用例的 2–4 步）付不起这个价，而长任务/无人值守的值可以调大。"
        "调大之后它仍然是有界的：同一份发现清单连着报两次（说明它什么都没动）"
        "就停，不会一直复读",
    ),
    Knob(
        "behavior_entry_timeout", 60, CAPABILITY,
        "L2.2 对照运行里一条入口命令的墙钟上限（秒）。一次对照跑两次子进程，"
        "超时**不算行为差异、也不当通过**，只报「这一步没验」。默认 60s 是按"
        "「入口是小脚本、输入是仓库里现成的小样本」标定的；项目入口本身很重时"
        "要么调大它，要么把 README 里的用法示例换成小样本",
    ),
    Knob(
        "survey_budget", 1200, CAPABILITY,
        "survey 一次最多收多少 token 的上下文",
    ),
    Knob(
        "output_reserve_ratio", 0.15, CAPABILITY,
        "为输出预留的窗口比例；被截断后抬到 boost（比例，随窗口伸缩）",
    ),
    Knob(
        "output_reserve_boost_ratio", 0.30, CAPABILITY,
        "截断过一次之后本次任务的输出预算上限",
    ),
    Knob(
        "soft_trigger_ratio", 0.70, CAPABILITY,
        "上下文达到窗口的多少就开始整理历史（不打断任务）",
    ),
    Knob(
        "hard_trigger_ratio", 0.90, CAPABILITY,
        "达到多少直接重置上下文（保留任务状态）",
    ),
    Knob(
        "hot_memory_ratio", 0.15, CAPABILITY,
        "热记忆占有效预算的比例（每轮注入）",
    ),
    Knob("task_state_ratio", 0.05, CAPABILITY, "任务状态块占有效预算的比例"),
    Knob(
        "code_ratio", 0.35, CAPABILITY,
        "代码片段占有效预算的比例：小窗口时会自动再加一档（把配额从历史挪向代码）",
    ),
    Knob("retrieval_ratio", 0.15, CAPABILITY, "检索内容占有效预算的比例"),
    Knob("lessons_ratio", 0.10, CAPABILITY, "历史教训占有效预算的比例"),
    Knob(
        "recent_chat_turns", 6, CAPABILITY,
        "通道/网页带进本轮提示的最近几条往来（不是工具往返；强模型可加）",
    ),
    Knob(
        "recent_chat_chars", 240, CAPABILITY,
        "上述每条往来最多留多少字；再长截断，避免把整段答复塞回窗口",
    ),
    Knob(
        "history_digest_turns", 12, CAPABILITY,
        "「更早那一段」压进本轮提示的摘要最多几条（在最近 recent_chat_turns 条"
        "之前那一段里取）。12 是「一次会话大致聊多少轮」的量级：让模型天然知道"
        "我们聊到哪儿了，而不是只能靠它主动检索——实测（本机 27B）它不会主动查。"
        "强模型/大窗口可以调大；调 0 = 不带摘要",
    ),
    Knob(
        "history_digest_chars", 400, CAPABILITY,
        "上述摘要的**总字符预算**（不是每条）：每条先按它截（助手那侧取一半），"
        "再按这个总预算从最老的开始丢。于是摘要正文总量 ≤ 400 字符、行数 ≤ "
        "history_digest_turns 条——中文大致 1 字 = 1 token，最坏约 300 token。"
        "它必须有界：摘要进的是每轮都付的提示，而「历史原文不进上下文」这条"
        "设计不能被摘要绕过去",
    ),
    Knob(
        "history_search_limit", 5, CAPABILITY,
        "search_history 一次默认回几条历史消息。与 recall 的 5 对齐：每条都要"
        "占上下文，而历史检索答的是「来源」这种定位问题，不需要一次给全",
    ),
    Knob(
        "history_search_max", 20, CAPABILITY,
        "search_history 一次最多几条。再多就是把整段历史灌回窗口——"
        "而历史不进上下文正是短上下文成立的前提；窗口大的模型可以调大",
    ),
    Knob(
        "history_snippet_chars", 240, CAPABILITY,
        "search_history 每条片段最多多少字。与 recent_chat_chars 对齐："
        "同一份往来的两种看法，单条截断的口径不该有两套",
    ),
    Knob(
        "episode_final_chars", 400, CAPABILITY,
        "事件摘要里任务结论最多留多少字（按本机 27B / 8K 标定）",
    ),
    Knob(
        "remember_max_chars", 400, CAPABILITY,
        "进热记忆的一条最多多少字（设计稿 §4.3 定案的 remember_max_chars）。"
        "它是 J4「非一次性产物」的长度线：设计稿 §1 记录的真实工作区里，"
        "12 城市天气表与三段交付答复都长于它，而可复用的真事实都在一句话量级。"
        "超线的条目不进热记忆、降级为 episode（由写入门禁判，见 memory/gate.py）",
    ),
    Knob(
        "remember_table_lines", 3, CAPABILITY,
        "一条记忆里出现几行「同构表格」（每行至少两个 `|`）就不算一句话事实"
        "（设计稿 §5.1 的 J4：≥3 行同构表格判为一次性产物）。与 remember_max_chars"
        "同属形态判据，所以同样可覆盖——按工作区调一版判据不该改代码",
    ),
    # 去重指纹的口径（设计稿 §4.3 / §5.3 ④ 第 1 级）。
    # 四个开关一起决定「什么算同一条」：命中同一条不新增，只在报告里记一次。
    # 它们是标定值而不是实现细节——按工作区调一版去重口径不该改代码。
    Knob(
        "dedup_casefold", 1, CAPABILITY,
        "计算去重指纹时是否不分大小写（1 = 不分，设计稿 §4.3 定案："
        "「测试命令是 pytest -q」与「测试命令是 PYTEST -Q」视为同一条）。"
        "0 = 大小写不同即算两条",
    ),
    Knob(
        "dedup_collapse_ws", 1, CAPABILITY,
        "计算去重指纹时空白怎么算（1 = **整体去掉**，设计稿 §4.3 的「去空白」口径："
        "换行、Tab、全角空格、零宽字符都不参与比较。0 = 只把连续空白折成一个半角"
        "空格并去掉首尾——这时「a b」与「ab」是两条）",
    ),
    Knob(
        "dedup_strip_punct", 1, CAPABILITY,
        "计算去重指纹时是否去掉标点与符号（1 = 去掉，设计稿 §4.3 的「去标点」口径："
        "「测试命令是 pytest -q」与「测试命令是 pytest -q。」算同一条）。"
        "去掉之前先把全角字符转半角（（ 转 (、「 转 \" 等），所以全角/半角标点等价。"
        "0 = 标点参与比较。**只影响指纹，不改写落库/落盘的原文**",
    ),
    Knob(
        "dedup_hash_chars", 16, CAPABILITY,
        "去重指纹（sha1 十六进制）取前多少位（设计稿 §4.3 定案 16 位 = 64 bit："
        "这个规模的记忆库碰撞概率远低于手工改错一条的概率）。调大只影响新写指纹，"
        "存量条目要重新归档才会换指纹",
    ),
    # 热记忆的容量口径（设计稿 §4.3 块 4 / §7.1）。两个值都来自设计稿的定案：
    # 40 条 / 单条 200 字，按真实工作区 9 条的规模留了足够余量（§1 实测）。
    Knob(
        "hot_max_items", 40, CAPABILITY,
        "热记忆最多几条（设计稿 §4.3 / §7.1 定案的 hot_max_items=40）。"
        "热记忆每轮全量注入，token 预算（hot_memory_ratio）只约束总字数，"
        "约束不了「条数」：40 条各一行仍然读得动，400 条一行也读不动。"
        "超限时按重要度从低到高下沉（不是先进先出，见 memory/hot.py）",
    ),
    Knob(
        "hot_item_chars", 200, CAPABILITY,
        "热记忆**单条**多少字以上就不收（设计稿 §4.3 / §7.1 定案的 hot_item_chars=200）。"
        "它是写入侧的拒绝线，比 remember_max_chars=400 更紧：一条占半屏的条目"
        "即使只算一条，也把同节里几条真事实挤了出去。被拒条目下沉为 episode"
        "（理由 `too_long`），不丢；存量超长条目由重要度里的长度扣分先挤出去",
    ),
    Knob(
        "hot_importance_long_step", 200, CAPABILITY,
        "重要度启发式里「越长越扣分」的步长：每满这么多字扣 1 分"
        "（默认 200 与 hot_item_chars 同量级，所以刚过单条线的条目扣 1 分，"
        "400 余字的天气表扣 2 分、排在任何一条短事实后面）。"
        "它是「可解释」的一半：分值差要能说清是哪条规则给的",
    ),
    Knob("system_quota", 900, CAPABILITY, "系统提示的固定配额（token）"),
    Knob(
        "fixed_quota_cap", 0.20, CAPABILITY,
        "固定配额也得随窗口伸缩：小窗口下它最多只能占有效预算的两成",
    ),
    Knob(
        "compact_threshold", 3000, CAPABILITY,
        "有效预算低于这个值就进入小窗口模式（配额从历史挪向代码）",
    ),
    Knob("compact_code_boost", 0.10, CAPABILITY, "小窗口模式下代码配额额外加多少"),
    Knob(
        "max_index_files", 2000, CAPABILITY,
        "超过这么多 Python 文件就不建索引（先数一遍再决定，数一遍很便宜）",
    ),
    Knob("index_seconds", 20.0, CAPABILITY, "建索引的时间预算，到点停下并说明"),
    Knob(
        "max_output_chars", 4000, CAPABILITY,
        "工具输出回灌给模型时最多留多少字符（超出的截掉并标注）",
    ),
    Knob("prefetch_budget", 400, CAPABILITY, "开局预取（符号表）花多少 token"),
    Knob(
        "prefetch_content_budget", 1000, CAPABILITY,
        "开局预取还带上多少 token 的代码片段",
    ),
    Knob(
        "decompose_material_budget", 3000, CAPABILITY,
        "拆解时最多读多少 token 的任务材料（题目说明与验收测试）——"
        "在这里读一次比在每个子任务里各读一遍便宜得多",
    ),
    Knob(
        "decompose_output_budget", 3072, CAPABILITY,
        "拆解单次生成最多多少 token。**它不是步骤数的上限**：排不完就再排"
        "一批（每批按 140 token/步折算）。实测 2048 在第十八步被截断，"
        "五十步的计划于是整体解析失败",
    ),
    Knob(
        "research_page_limit", 3, CAPABILITY,
        "一次 research 最多看几页。按本机 27B 标定：T1 一页就够，3 是给"
        "「先搜再读」留的余地；强模型/远程 API 可以调大",
    ),
    Knob(
        "research_output_budget", 800, CAPABILITY,
        "research 交回主循环最多多少 token。主循环上下文最贵，网页目录动辄"
        "上千 token，所以只交短结论+引用；要核对用 web_read（缓存几乎免费）",
    ),
    Knob(
        "research_steps", 8, CAPABILITY,
        "研究会话每一页（以及最后一次合并）的步数。T1 答对的题是 3 步，"
        "取错块打转会烧到上限——所以这是能力标定，不是写死的安全线",
    ),
    Knob(
        "search_result_limit", 3, CAPABILITY,
        "一次网页搜索最多几条。和 research_page_limit 对齐：先搜再读时"
        "一条结果一页，多了合并步会变重；强模型/远程 API 可以调大",
    ),
    Knob("command_timeout", 180, MECHANICAL, "单条命令的默认超时"),
    Knob("max_command_timeout", 600, SAFETY, "命令超时上限：防止挂死"),
    Knob(
        "path_confinement",
        1,
        SAFETY,
        "**符号项**（描述一条规则，不是一个可调数值）：写操作不出工作区",
    ),
    Knob(
        "denied_commands",
        1,
        SAFETY,
        "**符号项**：永久禁止清单——提权与系统级操作，不提供申请入口",
    ),
    # ---- P1（记忆架构）新增的 22 个旋钮 -----------------------------------
    #
    # **一次登记、分批消费**：P1 计划 Task 1 把它们一次性登记在这里，批次 2–4
    # 的任务只 `resolve` 不追加（`limits.py` 是这一批的单写点，见计划「并行与
    # 串行」的单写者清单）。每条 note 的「依据」一律写设计稿章节号或计划任务号
    # ——不许写死魔法数不留出处（Task 1 要点第 1 条）。
    #
    # 计划 Task 1 的旋钮表里还有第 23 行 `retrieval_ratio=0.15`：它就是上面
    # 既有的那一项（P0 已登记、已被 `context/budget.py` 消费），**不重复登记**
    # ——重复登记会让 `_BY_NAME` 静默取后者，Task 1 验收 5（无重名）会红。
    #
    # 嵌入只留 `w_embed` 一个旋钮、默认 0.0（设计稿 §13.11 / §13.13 第 6 条
    # 定案：P1 不引嵌入）：它是五维公式里的系数，且 `why` 必须能打出这一维
    # （§14.4 规矩 1、§6.3）。`embed_candidates_n` / `embed_rerank_k` /
    # `embed_min_cos` / `embed_timeout_ms` 四个选型旋钮**不登记**（属嵌入第③步，
    # §14.4 规矩 3 + 计划「范围」右列的「不做」）。
    Knob(
        "w_bm25", 1.0, CAPABILITY,
        "五维公式里 bm25 那一维的系数（设计稿 §14.4 规矩 1："
        "rel = normalize(w_bm25*bm25_norm + w_embed*embed_cos)）。"
        "**本计划初值，不是设计稿数值**（设计稿没给这个数）：取 1.0 时"
        "单信号等价于纯 bm25。消费：T5/T6",
    ),
    Knob(
        "w_embed", 0.0, CAPABILITY,
        "五维公式里嵌入那一维的系数（设计稿 §14.4 规矩 1）。**默认 0.0 是"
        "定案**（§13.11 的顺序 + §13.13 第 6 条：先分词口径、再 bm25，嵌入"
        "推迟）：P1 里这一维只会是「关」，但 `why` / `memory_decision` / "
        "`--events memory` 三个出口必须**可见地**报出来（§14.4 规矩 6 的"
        "「不许静默降级」）。消费：T6",
    ),
    Knob(
        "rank_w_relevance", 0.40, CAPABILITY,
        "五维打分里相关度的权重（设计稿 §6.1 的权重块）。"
        "五个权重与半衰期全部从登记表取，一个都不许写死在 rank.py 里。消费：T6",
    ),
    Knob(
        "rank_w_recency", 0.20, CAPABILITY,
        "五维打分里新近度的权重（设计稿 §6.1 的权重块）。消费：T6",
    ),
    Knob(
        "rank_w_importance", 0.25, CAPABILITY,
        "五维打分里重要度的权重（设计稿 §6.1 的权重块）。消费：T6",
    ),
    Knob(
        "rank_w_usage", 0.10, CAPABILITY,
        "五维打分里使用次数的权重（设计稿 §6.1 的权重块）。消费：T6",
    ),
    Knob(
        "rank_w_penalty", 0.05, CAPABILITY,
        "五维打分里惩罚项的权重（设计稿 §6.1：penalty = applied_count / "
        "(use_count + 1)）。消费：T6",
    ),
    Knob(
        "rank_recency_half_life_days", 30, CAPABILITY,
        "新近度那一维的半衰期天数（设计稿 §6.1：rec = exp(-age_days / "
        "half_life_days)）。它是能力标定值：外部 API 的模型不该照搬本机 "
        "27B 的 30 天。消费：T6",
    ),
    Knob(
        "recall_candidates_n", 50, CAPABILITY,
        "两阶段检索的**候选池**大小（设计稿 §14.4 规矩 3 的 N；设计稿里的名字"
        "是 embed_candidates_n，P1 不引嵌入所以按计划「口径裁决 3」改成中性名）。"
        "候选池只在内存里过一遍、**不渲染进 prompt**（§6.2 表：0 token）。"
        "消费：T6",
    ),
    Knob(
        "recall_rerank_k", 5, CAPABILITY,
        "两阶段检索的**重排后取几条**（设计稿 §14.4 规矩 3 的 K；设计稿里的"
        "名字是 embed_rerank_k，「口径裁决 3」改成中性名）。硬约束："
        "**K ≤ recall_limit**，且重排只能减少进上下文的条数、不能增加。消费：T6",
    ),
    Knob(
        "recall_limit", 5, CAPABILITY,
        "recall 一次默认回几条（设计稿 §6.2 表：一次 5 条 × 240 字 ≈ 1200 "
        "token，与 history_search_limit=5 同一口径）。消费：T2（semantic 检索的"
        "默认条数）/T6/T11",
    ),
    Knob(
        "recall_entry_chars", 240, CAPABILITY,
        "recall 每条最多多少字（设计稿 §6.2 表；与既有 history_snippet_chars=240 "
        "对齐：同一份内容的两种看法，单条截断的口径不该有两套）。消费：T11",
    ),
    Knob(
        "dedupe_similarity", 0.90, CAPABILITY,
        "近似去重的相似度线（设计稿 §5.3 ④ 第 2 级 + §6.1）。它是写入侧第二级"
        "去重的判据：精确级由 content_hash 唯一部分索引负责，这一级抓同义改写，"
        "**抓不到的要如实写在 --explain 里、不假装**（计划「不做」清单）。消费：T7",
    ),
    Knob(
        "remember_min_chars", 8, CAPABILITY,
        "一条记忆的**最短长度**（设计稿 §4.3 容量上限段定案的 "
        "remember_min_chars=8）：比它短的条目不是一个可复用事实。与既有 "
        "remember_max_chars=400 是同一条长度线的两端，所以同样可覆盖。消费：T7",
    ),
    Knob(
        "memory_budget_ratio", 0.30, CAPABILITY,
        "语义记忆（三层注入里的检索层）占有效预算的比例上限（设计稿 §6.2 表）。"
        "消费：T12",
    ),
    Knob(
        "event_chain_max_units", 2, CAPABILITY,
        "一轮最多取几个「实体·属性的有序事件链」单元（设计稿 §6.7 / §6.2 表）。"
        "链作为**一个单元**参与候选与重排，不是把事件逐条塞进去。消费：T11",
    ),
    Knob(
        "event_chain_render_max_chars", 400, CAPABILITY,
        "一个事件链单元渲染进上下文的最多字数（设计稿 §6.7 / §6.2 表）。"
        "消费：T11",
    ),
    Knob(
        "state_direct_inject_min_confidence", 0.8, CAPABILITY,
        "允许**直接注入** state 行的 confidence 门槛（设计稿 §6.6 + §13.13 第 7 "
        "条：直注要同时过「重要度达标 + 在时间窗内 + 过冲突检查」三关）。"
        "设计取值，不是测出来的数。消费：T12",
    ),
    Knob(
        "state_direct_inject_max", 1, CAPABILITY,
        "一轮最多直接注入几条 state 行（设计稿 §6.6：只开这一道口，"
        "`semantic` 的普通条目一律不做直接注入）。消费：T12",
    ),
    Knob(
        "state_direct_inject_max_age_days", 30, CAPABILITY,
        "直接注入的 state 行必须有多新（设计稿 §13.13 第 7 条只写「最近 N 天」，"
        "**没给 N**）。**本计划初值，不是设计稿数值**：取 30 与 "
        "rank_recency_half_life_days 同量级。消费：T12",
    ),
    Knob(
        "state_auto_pick", 0, CAPABILITY,
        "冲突的 state 值要不要自动选一个（设计稿 §15.6 第 3 条：默认 0 = 关，"
        "**不许静默选一个**；开启时也必须在输出里标明是自动选的）。消费：T9",
    ),
    Knob(
        "event_archive_threshold", 1000, CAPABILITY,
        "memory_event 超过几条就把最旧的搬进归档表（设计稿 §13.13 第 5 条："
        "与 episode_archive_threshold=1000 同口径，阈值进登记表可覆盖）。"
        "**待标定初值**：事件增长速率**无实测**（§13.13 第 5 条点名「本轮没测」；"
        "§7.1 的 episode 26 条/18 天不能外推到 event）。消费：T3/T9",
    ),
    # ---- T11 补缺轮登记的两个旋钮 -----------------------------------------
    #
    # T11 交回来时如实报了两条缺口，这两项就是其中的「阈值写成了裸常数」：
    # `rank.CHAIN_INCOMPLETE_FACTOR = 0.5` 与 `session.MIN_MATCHED_TOKENS = 2`
    # 都**没有**设计稿数值（设计稿 §6.7 表只说「缺基准值就扣分」、判据②只说
    # 「逐字召回要够区分度」），是本实现的裁决。裁决可以是初值，但不该是**写死**
    # 的值：本仓的规矩是「阈值不许写死、必须进登记表可覆盖」（设计文档 2.7 的
    # 判据 + P1 计划 Task 1 的「一次登记」）。所以这一轮把它们补登进表。
    Knob(
        "event_chain_incomplete_factor", 0.5, CAPABILITY,
        "链单元**时序完整度**不足时，重要度乘的扣分系数（设计稿 §6.7 表 / §15.6："
        "`derived_from` 没覆盖写链 / 缺基准值 / 状态未定时扣分并标 needs_review，"
        "**不假装完整**）。设计稿只说「扣」、**没给数**——0.5 是 T11 的实现裁决，"
        "不是设计稿数值：它要能看得见地扣，又不能把一条信息量仍然很大的链打到与"
        "「没有这条」等价。1.0 = 不扣（排查用），越接近 0 扣得越狠。"
        "消费：T11 补缺轮（rank._chain_imp_part）",
    ),
    Knob(
        "event_chain_min_matched_tokens", 2, CAPABILITY,
        "事件侧「逐字召回」把键带进候选所需的**最少不同 token 数**"
        "（设计稿 §6.7 判据②的实现口径）。为什么默认不是 1：逐字口径把"
        "「现在还剩多少」拆成 `现 在 还 剩 多 少`，单个「在」几乎每句话里都有"
        "——只命中一个字就建链会把链单元灌满候选池；两个字符的命中"
        "（事件「还剩 80」命中 `还`+`剩`）才有区分度。它是**候选选择判据、不是"
        "打分**（打分只有 rank.py 的五维一条路）；调大 = 更严（键更难被建链）。"
        "消费：T11 补缺轮（session._keys_recalled_by_events）",
    ),
)

_BY_NAME = {knob.name: knob for knob in KNOBS}


def knob(name: str) -> Knob | None:
    return _BY_NAME.get(name)


def resolve(name: str, overrides: Mapping[str, float] | None = None) -> tuple[float, str]:
    """返回值与**来源**。来源要能说出来，否则调参就是猜。"""
    entry = _BY_NAME.get(name)
    if entry is None:
        raise KeyError(f"没有登记过这个标定值: {name}")
    if entry.overridable and overrides and name in overrides:
        return float(overrides[name]), "配置"
    return float(entry.default), "默认"


def path_for(project_root: Path) -> Path:
    return project_root / ".agent" / "limits.json"


def load_overrides(project_root: Path) -> dict[str, float]:
    path = path_for(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[str(key)] = float(value)
    return found


def save_overrides(project_root: Path, overrides: Mapping[str, float]) -> Path:
    path = path_for(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(sorted(overrides.items())), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def render(overrides: Mapping[str, float] | None = None) -> str:
    """现在生效的是多少、从哪来。"""
    lines = [f"{'值':>8}  {'来源':<6} {'能不能改':<8} 名字", "-" * 72]
    for entry in KNOBS:
        value, source = resolve(entry.name, overrides)
        shown = f"{value:g}"
        can = "可覆盖" if entry.overridable else "写死"
        lines.append(f"{shown:>8}  {source:<6} {can:<8} {entry.name}")
        lines.append(f"{'':>8}  {entry.note}")
    return "\n".join(lines)
