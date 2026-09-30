"""后台探活：网页壳的请求路径**永不等探测**。

为什么要这一层：`/state` 与 `/target` 的响应里带着"模型通不通"（页面顶部那条横幅），
而探活是**发 HTTP**。模型地址连不上时（本机没起、地址是个黑洞、超时类异常），
一次探测要等到超时——实测（同一口径，见 `.tmp/web-latency.py`）：

| 场景 | 改前 `GET /state` | 改前 `POST /target` |
|---|---|---|
| 探活打桩成"睡 3 秒" | 3033ms | 3035ms |
| 黑洞地址（连接超时） | 1298ms | 1252ms（缓存过期那一下）|

人在页面上"切个工作区 / 会话"就要等这么久，而那跟切换本身一点关系都没有。

所以探测挪到一条后台线程上，请求路径只读**最近一次结果**，并且把"这是什么
时候探的"一起交出去（`probed_at` / `age_s`）：陈旧值可以接受，**静默的陈旧值**
不行——页面要能说"3 秒前探过"，而不是把一个几分钟前的结论当成刚刚测的。

三件事是这块的判据：

1. **请求路径不阻塞**：`snapshot()` 只读一次内存里的字典，绝不去探；
2. **`unknown` 与 `down` 分得开**：还没探过时 `model.ok` 是 `None`（不是 `False`）
   ——报"连不上"是假话，报"通"也是；
3. **线程收得干净**：`stop()` 之后线程会退（`WebServer.server_close` 里调，
   有用例按线程名量"回到基线"）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from spoolkit.health import PROBE_TIMEOUT_SECONDS
from spoolkit.web.runner import health_snapshot

#: 两次探测之间隔多久。
#:
#: 依据是**原来那条路的实际节拍**：页面每 3 秒轮一次 `/state`
#: （`page.py` 的 `setInterval(refreshBanners, 3000)`），而 `health.CACHE_TTL`
#: 也是 3 秒——等于"每次轮询都有一份新结果，且最多只探这么勤"。
#: 照抄这个节拍：页面上看到的鲜度不降级，锤模型的次数也不增加。
HEALTH_INTERVAL_SECONDS = 3.0

#: 探测线程收工最多等多久才放手。
#:
#: 探测里可能有最长 `PROBE_TIMEOUT_SECONDS` 的 HTTP 等待（还有每个外接配方各
#: 一次），所以 `stop()` 的 join 必须有个上限，不能把关闭服务这件事挂在一次
#: 网络上。到点没退也不留隐患：线程是 daemon，而且 `_loop` 每一圈都会看一次
#: 停止事件——那一口网络一结束它就自己收工。
STOP_JOIN_SECONDS = 5.0

#: 探测线程的名字。用例按它数"线程有没有回到基线"。
PROBE_THREAD_NAME = "spool-health-probe"

#: 探活结果里的三个字：模型通（up）/ 在但没在超时内回话（slow）/ 不通（down）。
#: `unknown` 不在这个函数里出——那是"还没探过"，由 `_unknown()` 给。
STATE_UP = "up"
STATE_SLOW = "slow"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


def state_of(model: dict) -> str:
    """把一次模型探测结果翻成 `up` / `slow` / `down`。

    `slow` 与 `down` 靠探活自己给的 `kind`（`health.KIND_SLOW` 等）分开：
    两者对人的下一步不一样（去起服务 vs 去看模型是不是在加载），混成一个
    "不通"就把那点信息丢了。认不出 `kind` 的老结果按 `down` 办。
    """
    if model.get("ok") is True:
        return STATE_UP
    return STATE_SLOW if str(model.get("kind") or "") == STATE_SLOW else STATE_DOWN


def _unknown() -> dict:
    """还没探过的那一份：字段一个不少，但**不假装**通或断。

    `model.ok` 是 `None`（不是 `False`）：页面上的红横幅按 `ok is False` 亮，
    `None` 就不会亮——"还没探过"不该长得像"连不上"。到底怎么显示由
    `state: "unknown"` 说，页面据此说"还没探过（后台正在探）"。
    """
    return {
        "model": {
            "ok": None,
            "name": "model",
            "target": "",
            "detail": "还没探过（后台探活刚起来）",
            "kind": "",
        },
        "services": [],
        "state": STATE_UNKNOWN,
        "probed_at": None,
        "age_s": None,
        "url": "",
    }


class HealthProbe:
    """一条后台线程按固定节拍探活，请求路径只读它的最近一次结果。

    `extra_args` / `extra_health` 与 `Runner` 收到的是同一组：探的就是这次运行
    真正生效的那个模型地址，页面横幅与终端那一行因此说的是同一件事。
    """

    def __init__(
        self,
        extra_args: Sequence[str] = (),
        extra_health: Callable[[], dict] | None = None,
        *,
        interval: float = HEALTH_INTERVAL_SECONDS,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._extra_args = tuple(extra_args)
        self._extra_health = extra_health
        self._interval = float(interval)
        self._timeout = float(timeout)
        # `probed_at` 给人看（epoch 秒），`age_s` 用单调钟算：墙上时钟被改过
        # （对时、夏令时）时"多久以前"不该跟着跳。
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._payload: dict | None = None
        self._probed_mono: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- 生命周期 ---

    def start(self) -> None:
        """起探测线程（幂等；`stop()` 之后再 `start()` 会重新起一条）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # 事件与线程一一对应：`stop()` 之后再起一条时不能用上一轮那个已经
            # 置位的事件，否则新线程第一圈就退出去了。
            stop = threading.Event()
            self._stop = stop
            thread = threading.Thread(
                target=self._loop,
                args=(stop,),
                name=PROBE_THREAD_NAME,
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = STOP_JOIN_SECONDS) -> None:
        """让探测线程收工。可以随时调、可以调多次。

        先置停止事件再 join：探测本身可能在等一口网络（最长一次超时），所以
        join 有上限——到点就放手，线程是 daemon，而且它下一圈会自己看到事件。
        """
        with self._lock:
            stop, thread = self._stop, self._thread
        stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._probe_once()
            if stop.wait(self._interval):
                return

    def _probe_once(self) -> None:
        try:
            payload = health_snapshot(
                self._extra_args, self._extra_health, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - 探活自己崩了不该让这条线程死掉
            payload = {
                "model": {
                    "ok": False,
                    "name": "model",
                    "target": "",
                    "detail": f"探活失败：{exc}",
                    "kind": "",
                },
                "services": [],
            }
        model = payload.get("model")
        model = dict(model) if isinstance(model, dict) else {}
        ready = dict(payload)
        ready["model"] = model
        ready["state"] = state_of(model)
        ready["url"] = str(model.get("target") or "")
        ready["probed_at"] = self._clock()
        with self._lock:
            self._payload = ready
            self._probed_mono = self._monotonic()

    # --- 给页面看的 ---

    def snapshot(self) -> dict:
        """最近一次结果。**永不等探测、也不自作主张去探**（那是线程的事）。

        `age_s` 每读一次现算：它是"这一份结果离现在多久"，不是探测那一刻的常量。
        """
        with self._lock:
            payload, probed = self._payload, self._probed_mono
        if payload is None or probed is None:
            return _unknown()
        out = dict(payload)
        out["age_s"] = max(0.0, round(self._monotonic() - probed, 3))
        return out
