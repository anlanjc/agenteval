# Judge 校准报告（mock 模式，n=30）

## v1 judge（默认严格规则）

- accuracy: **0.8333**
- Cohen's kappa: **0.5098**

| golden \ predicted | pass | fail |
| --- | --- | --- |
| pass | 21 | 3 |
| fail | 2 | 4 |


阈值扫描最优（v1）：threshold=3.8, kappa=0.7619, accuracy=0.9333

## v2 judge（校准后：超长封顶 1.0→3.0，命中分档收紧）

- accuracy: **0.9667**
- Cohen's kappa: **0.8889**

| golden \ predicted | pass | fail |
| --- | --- | --- |
| pass | 24 | 0 |
| fail | 1 | 5 |


阈值扫描最优（v2）：threshold=4.4, kappa=0.8889, accuracy=0.9667

> 结论：kappa 0.5098 → 0.8889（阈值扫描只能到 0.7619，规则修订贡献了主要提升）。
> 残余误差：1 例数字归因颠倒（swap）为规则型 judge 的盲区，需真实 LLM judge 或 数值一致性校验兜底。