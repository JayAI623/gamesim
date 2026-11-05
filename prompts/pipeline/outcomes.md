# 结果词典（规则 C）：所有局按同一口径汇报

> 每个结果只在这里定义一次。key 后面加 `@日期` 表示日历时间窗，例如 `hormuz_recovery@2027-04-30`；跨局比较时时间窗必须一致。
> 新局要用新结果时，先加进这里；GM 的 `p_outcomes` 和报告只能引用这里的 key 和定义。
> 下面两条对应 `examples/` 里的两个示例，复制本文件后按你的话题增删。

| key | 定义 | 时间窗 | 用过的局 |
|---|---|---|---|
| `hormuz_recovery` | 穿过海峡的原油流量 H ≥ 95（占战前 %），冲突烈度 W ≤ 30，且当轮末停火有效，三条同时满足 | 6 个月，任意一轮（ever） | examples/hormuz |
| `hormuz_partial` | H ≥ 85，且停火或暂停打击有效（`hormuz_recovery` ⊂ `hormuz_partial`） | 同上 | examples/hormuz |
| `us_collective_measure_cn` | 宽口径：S ≥ 80，按 GM 规则里的锚点表——联邦立法禁止中国籍进国家实验室或受控科研岗、暂停中国籍 H-1B/绿卡审理、对 PRC 国籍整类暂停 F/J/H 新签、列入 212(f) 旅行禁令、NSEERS 式集体登记，或州置地禁令扩到绿卡持有人/覆盖 ≥10 州；单个案件不算 | 推演窗口内任意一轮（ever） | examples/diaspora |
| `diaspora_strategy_rank` | 每类家庭的策略臂在第一层各条轨迹上的累计折现效用排名（第二层，见 examples/diaspora/strategy.json） | 推演窗口 | examples/diaspora |
