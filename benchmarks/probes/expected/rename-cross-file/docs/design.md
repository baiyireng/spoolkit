# 管道设计说明

## 层次

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 抓取 | `pipeline/fetch.py` | 把上游 CSV 读成统一的行（`load_rows`） |
| 变换 | `pipeline/transform.py` | 丢空行、金额归一 |
| 报表 | `pipeline/report.py` | 渲染纯文本 |
| 入口 | `pipeline/cli.py` | 命令行 |

抓取层是唯一的读入口，其余层一律通过 `load_rows` 拿数据，不自己开文件。
