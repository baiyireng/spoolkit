"""状态与时序：`entity.attribute` 的有序事件链与由它推出的当前值（设计稿 §15）。

`memory_event`（原始观察，append-only）与 `memory_state`（推导值）**分开存**——
时序性的根因是「值被存成了句子」：把「还剩 80」当事实存下来，第二天「我赚了 50」
就没有任何东西可以作用在它上面（§15.1）。键（`entity.attribute`）是归约器的入口，
别名映射是入口的门牌号翻译（§15.2）。

本包的四个模块分工（T8 / T9）：

- `clock.py`：可注入时钟（`Clock` / `SystemClock` / `FakeClock`）。**全包唯一**允许读
  真实时间的地方（§15.5 硬约束 1：不许在解析/归约时现取 `datetime.now()`）。
- `parse.py`：§15.5 的四步时间解析，歧义落 `unknown` 并给理由；日界按 `tz_offset_min` 算。
- `registry.py`：`entity_attribute` 注册表 + 两张别名表；冲突**拒绝合并**（§15.2）。
- `reduce.py`：六个确定性归约器、有效期与取代链（关旧行 → 插新行 → 旧行指向新行）、
  冲突六类与三条出口里的前两条（`state.status/reason` + `memory_audit`）（§15.3/§15.4/§15.6）。
- `db.py`：时序六表的读写薄层（**只读** `store.py` 的建表入口，DDL 一个字不改）。
"""
