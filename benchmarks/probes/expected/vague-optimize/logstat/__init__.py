"""日志统计包：把 access log 汇总成一份可以贴进日报的摘要。"""

from .report import collect, path_counts, summarize, top_paths

__all__ = ["collect", "path_counts", "summarize", "top_paths"]
