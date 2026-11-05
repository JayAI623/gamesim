# 示例：东升西降情景下的在美华人（两层推演）

**问题**：假如中国综合实力约在 2031 年超过美国（用户给定的条件情景；主流预测并不支持名义 GDP 意义上的超越），2027–2032 年美国是否会对华裔/中国籍人群出台集体性限制（宽口径 S ≥ 80）？三类在美华人家庭各自的哪种布局在这些轨迹上更稳？

| 文件 | 内容 |
|---|---|
| `entities.json` | 第一层：5 个机构（美国联邦政府、中国政府、美国科技雇主与高校、中立枢纽、美国盟友），10 个共享变量，6 轮 × 1 年，盲骰 / 基准率上限 / 参照类 / 折现全开，各实体已按年写好 `delta` |
| `swap.json` | 2028 大选分支：第 2 轮后按分层分配（run_1、run_2 民主党，run_3 共和党）给美国联邦政府换效用权重、在第 3 轮开头公布结果；汇总按 p = 0.6 / 0.4 加权为 0.3 / 0.3 / 0.4 |
| `strategy.json` | 第二层**模板**：家庭甲（公民）/ 乙（绿卡）/ 丙（H-1B）× 5 个布局臂（锚美、美国为主 + 第三地期权、一人先回流、举家回流、举家迁第三地），F 分项、暴露系数、红线、折现、按臂的变量修正、打分员规则 |

## 先填模板

`strategy.json` 里所有 `<填写…>` 都是要按你评估的家庭改的字段：身份年限、居住州类型（无外国人置地限制的州 / 有置地限制的州，如得州、佛州）、收入区间、在原籍国的牵连（亲属、房产、账户）、子女情况、回流收入比、关键时间点。没填的字段，打分员按同类人群的中位情况估计并在依据里注明。丙的 `var_mods`（在有置地限制的州，美国侧臂的在华资产变量 A 再 −10）只在适用时保留。

## 跑

```bash
gamesim check examples/diaspora/entities.json     # 1 条提醒：美国联邦政府样本外命中 1.5/4（对齐页要标为「拿不准」）

# 第一层：3 局 + 大选分支
gamesim run runs/diaspora --entities examples/diaspora/entities.json --swap examples/diaspora/swap.json --runs 3 --fake

# 压力测试：复制 run_3 的前 2 轮，在第 3 轮注入一次危机（文件自己写，格式见下）
gamesim fork runs/diaspora/run_3 runs/diaspora/run_4 --upto 2 --events stress.json --fake

# 第二层：平稳局（默认不含 fork 出来的局）和压力局分开打分
gamesim score examples/diaspora/strategy.json runs/diaspora --fake
gamesim score examples/diaspora/strategy.json runs/diaspora --runs run_4 --out runs/diaspora/layer2_stress --fake

# 翻转点：压力局从第 4 轮起把 S 扫到拘押级，看哪类家庭的第一名会变
gamesim sweep examples/diaspora/strategy.json runs/diaspora --run run_4 --out runs/diaspora/layer2_stress \
  --from-round 4 --set S=56,70,80,90,96
```

去掉 `--fake` 就用真 claude（第一层约 5×6×3 + 2×6×3 = 126 次，压力局约 40 次，第二层每条轨迹 × 每类家庭 1 次）。

`stress.json` 例子：

```json
[{"id": "stress_strait", "seq": 1, "text": "【压力测试，用户注入】第 3 轮开头发生海上检疫危机，持续约 3 周；本轮 W 至少升至 75，各方必须对此作出反应。"}]
```

## 读结果时注意

- 第二层的暴露系数、收入公式和 F 分项是人定的假设；看 `summary.md` 里的敏感性和分项拆解，前两名差距在假设误差内时当作持平（`docs/METHOD.md` 坑⑦）。
- LLM 玩家偏温和，平稳局很少出现坏情况，坏情况靠 `fork` 注入（坑④）。

> 这是条件情景推演，不是预测；不构成移民、法律、税务或投资建议。涉及个人决定请咨询持牌专业人士。
