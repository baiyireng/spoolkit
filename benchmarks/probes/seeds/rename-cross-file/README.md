# 数据管道

```
python -m pipeline.cli data/parts.csv
python -m unittest discover -s tests
```

分三层：`fetch` 只管读，`transform` 只管洗，`report` 只管渲染。

上游换过两次导出格式，读那一层（`pipeline/fetch.py` 里的 `get_rows`）是唯一的
入口，所以内部改名会牵动 `transform` / `report` / `cli` / 测试四处。
