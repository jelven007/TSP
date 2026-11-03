# Bug: S02滚动MA浮点噪声误判为上升

> Status: FIXED
> Mode: --quick
> Severity: functional
> Author: user
> Last updated: 2026-11-03

## Symptom

自定义S02直接判断滚动MA差值大于0。理论持平的两个MA可能因浮点计算得到
极小正数，从而错误通过双周期MA严格递增条件。

## Expected

相邻MA差值必须严格大于`1e-10`才算上升；差值小于或等于该阈值按持平淘汰。

## Reproduction

- 命令：`backend/.venv/bin/python -m pytest tests/test_late_session_strength.py::test_ma_increase_rejects_floating_point_noise_at_flat_boundary -q`
- 测试位置：`backend/tests/test_late_session_strength.py`
- 复现值：理论持平边界计算为`1.7763568394002505e-15`
- 复现稳定性：修复前连续3/3失败，旧逻辑均错误返回`{"a"}`

## Hypotheses & diagnosis

| # | Hypothesis | Verdict | Evidence |
| --- | --- | --- | --- |
| H1 | `_rising_symbols`使用`diff > 0`，将滚动计算噪声解释为上升 | confirmed | 回归夹具最后一段理论持平，实际差值为`1.7763568394002505e-15`，旧谓词放行 |
| H2 | 上游行情排序或缺口导致错误通过 | eliminated | 直接调用纯计算函数，时间连续、唯一且价格有限，只有MA差值边界决定结果 |

## Root cause

`_rising_symbols`先计算5周期滚动均值，再用浮点差值直接与0比较。
滚动求和的舍入误差可让理论为0的差值成为极小正数，因此“严格上升”的实现
没有落实业务需要的数值容差。

## Fix

- 改动文件：`backend/app/strategy/late_session_strength.py`
- 将每个相邻MA差值的通过条件从`> 0`收紧为`> 1e-10`
- 不改变MA窗口、分钟聚合、时间、广度、涨幅、评分或交易参数

## Verification

- V-1：新增测试在旧实现连续3/3 RED
- V-2：修复后定向测试 GREEN，1项通过
- V-3：临时恢复旧比较后测试重新 RED；恢复修复后再次 GREEN
- V-4：`test_late_session_strength.py`完整模块32项通过
- V-5：其他`test_late_session_*.py`相关测试27项通过
- V-6：变更文件Ruff通过
- V-7：文档检查通过，37个Markdown文件及8个需求模块通过
- V-8：聚焦`git diff --check`通过

## Regression test

- 路径：`backend/tests/test_late_session_strength.py`
- 名称：`test_ma_increase_rejects_floating_point_noise_at_flat_boundary`
- 断言：先证明夹具产生`0 < diff <= 1e-10`，再证明该证券被严格递增筛选淘汰

## Pattern analysis

| 搜索方式 | 命中数 | 是否本次同类隐患 |
| --- | ---: | --- |
| 搜索后端`rolling_mean`、`diff()`及直接大于0比较 | 修复前1，修复后0 | 仅本次S02路径 |

## Open questions / Follow-ups

当前仅完成源码、测试及文档修复，未构建运行产物、部署本地服务或部署线上，
也未以新边界重新执行历史收益回测。
