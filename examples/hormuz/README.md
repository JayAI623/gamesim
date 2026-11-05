# 示例：霍尔木兹海峡停火（6 方 × 6 轮，每轮 1 个月）

**问题**：从停火条件出发，未来 6 个月海峡恢复（H ≥ 95 且 W ≤ 30 且停火有效）的概率是多少？谁、何时、以什么条件破局？

| 项 | 内容 |
|---|---|
| 参与方 | 美国、IRGC 核心、伊朗外交线、以色列、中国、海湾–调解集团（codename Alpha–Foxtrot） |
| 共享变量 | H 原油流量、P 油价、N 核门槛、B 封锁强度、W 冲突烈度、L 黎巴嫩战线、R 政权稳定、D 美国国内压力、G 全球下行压力、Y 绕行管道 |
| 目标 | `hormuz_recovery`：任意一轮 H ≥ 95、W ≤ 30 且 `agreement.in_effect`（ever） |
| 时间 | 2026-11-03 至 2027-04-30，6 轮 |
| 开关 | 盲骰、基准率上限、参照类、折现全开；协议框架「停火」（美国与伊朗） |

`entities.json` 由测试夹具 `tests/fixtures/hormuz_v3_run1/` 导出（`tests.helpers.fixture_entities(v08=True)`），事实截至 2026-11，是一份**历史快照**，不会自动更新。

## 跑

```bash
gamesim check examples/hormuz/entities.json            # 会给出 7 条提醒，见下
gamesim bdm examples/hormuz/entities.json -o examples/hormuz/bdm.json --markdown
gamesim run runs/hormuz --entities examples/hormuz/entities.json --runs 3 --fake   # 假 claude，约 1 秒
gamesim run runs/hormuz-real --entities examples/hormuz/entities.json --runs 3     # 真 claude，约 6×(6+2)×3 = 144 次调用
gamesim aggregate runs/hormuz-real
```

## `check` 的提醒为什么保留

这份 entities 原样保留了当时的写法：有 5 个「越高越糟」的项用了 `-` + `convex`，它表示越接近上限边际损失越小（见 `docs/METHOD.md` 坑②）。是不是要改成 `concave`，要看每一项的本意，所以示例里不替你改，留给 `check` 提醒。

> 推演是条件情景，不是预测，也不构成任何投资建议。
