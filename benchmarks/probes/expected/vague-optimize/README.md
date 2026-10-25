# logstat

把 access log 汇总成一份能贴进日报的摘要。

```
python stat.py data/app.log
```

`data/app.log` 只是随手留的一小段样本。线上那份是几万行，本地复现要用：

```
python gen_log.py 40000 > data/big.log
python stat.py data/big.log
```

摘要里的次序是结果的一部分（`top_paths` 按次数降序、同次数按路径字典序），
换实现也不能动这个次序，日报的历史数据要对得上。
