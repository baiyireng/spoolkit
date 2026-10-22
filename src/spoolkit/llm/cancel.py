"""进行中的模型请求可以取消。

取消只关掉当前这一次连接。已经提交的章节不在这里处理。
"""

import threading
from collections.abc import Callable


class GenerationCancelled(Exception):
    """用户停掉了正在生成的这一次请求。"""

    def __init__(self, text: str = "") -> None:
        super().__init__("用户停止")
        self.text = text


class CancelToken:
    """一个开关。取消时调用正在读的响应的 close，让阻塞的读马上返回。"""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._armed = threading.Event()
        self._close: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._flag.clear()
            self._close = None
            self._armed.clear()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    @property
    def armed(self) -> bool:
        return self._armed.is_set()

    def arm(self, close: Callable[[], None]) -> None:
        with self._lock:
            self._close = close
            self._armed.set()
            should_close = self._flag.is_set()
        if should_close:
            close()

    def disarm(self) -> None:
        with self._lock:
            self._close = None
            self._armed.clear()

    def cancel(self) -> None:
        with self._lock:
            self._flag.set()
            close = self._close
        if close is not None:
            close()
