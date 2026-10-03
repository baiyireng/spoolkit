# expenses

个人记账的小工具，账本就是 `data/expenses.csv`。

```
python -m expenses.cli total
python -m expenses.cli by-category
```

`by-category` 每行输出 `分类名 金额`，金额降序、同额按名字升序——我按这个格式
往报销单里贴，格式别乱动。

测试：

```
python -m unittest discover -s tests
```
