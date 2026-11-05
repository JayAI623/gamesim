# gamesim

**让 Claude 分别扮演博弈中的各方，由独立的 GM 裁决、BDM 模型做数值基线，跑多局得到局势轨迹的分布。**

纯 Python 标准库；每个角色是一次 `claude -p` 调用（Claude Code CLI），不需要任何 API key。

## 它解决什么

| 问题 | gamesim 的做法 |
|---|---|
| 一个模型同时想所有方，容易「上帝视角」 | 每方一次独立调用，只看到自己的收益卡、私有信息和日记；GM 另起一次调用裁决 |
| 概率靠拍脑袋、事后改骰子 | 盲骰：GM 先承诺概率、看不到骰子，脚本按预掷的骰子判成败；基准率上限、参照类 + 90% 区间 |
| 效用是编的 | 每方一个研究员用显示偏好法从历史取舍反推权重，做样本外检验 |
| 输入写错没人发现 | 开跑前 5 视角独立审查 + 反驳式核实；`gamesim check` 机械检查常见错误 |
| 一局说明不了什么 | 多局并行、分支分层分配、压力测试分叉、k/n + Wilson 区间 |
| 关心的决策者动不了局势 | 第二层：给决策者的若干策略臂在每条轨迹上打分、做敏感性和翻转点扫描 |

## 安装

```bash
git clone <this repo> gamesim && cd gamesim
python3 -m pip install -e .          # 提供 gamesim 命令；Python ≥ 3.10，无第三方依赖
python3 -m pytest                    # 或 python3 -m unittest discover -s tests -t .
```

真跑需要已登录的 [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI（`claude` 在 PATH 上）。调用方式固定为：

```
claude -p --output-format text --no-session-persistence --setting-sources "" --strict-mcp-config \
       --disable-slash-commands --tools "" --model <model>
```

prompt 走 stdin，cwd 是空的临时目录，不加载你的设置、插件、MCP 服务器和斜杠命令。`GAMESIM_CLAUDE_BIN` 可以换成别的可执行文件（路径或一行命令）。

## 5 分钟上手

**1. 用假 claude 跑通全流程（不调用模型，约 1 秒）**

```bash
gamesim check examples/hormuz/entities.json
gamesim run runs/hormuz --entities examples/hormuz/entities.json --runs 2 --fake
gamesim status runs/hormuz
gamesim aggregate runs/hormuz
```

`--fake` 等价于 `GAMESIM_CLAUDE_BIN="python3 $PWD/gamesim/fake_claude.py"`（要用绝对路径：调用时的 cwd 是空临时目录）：它按 prompt 类型返回合法 JSON（角色、GM 两阶段、打分员），结果可复现。

**2. 用真 claude 跑示例**

```bash
gamesim run runs/hormuz-real --entities examples/hormuz/entities.json --runs 3 --model sonnet
```

6 方 × 6 轮 × 3 局 + GM 两阶段 ≈ 144 次调用；中断后重跑同一命令会从断点继续。两层的完整例子（分支、压力测试、策略臂打分、翻转点扫描）见 `examples/diaspora/README.md`。

## 流程

```
 情报 ─► 主体/共享变量 ─► 每方研究员反推效用 ─► 5 视角审查 + 核实 ─► gamesim check
                                                                         │
                                                       对齐页 ✋ 用户确认 ◄┘
                                                                         │
  gamesim bdm（基线）◄────────────────────────────────────────────────────┤
                                                                         ▼
 gamesim run：每轮  prep(预掷骰子, 写各方 prompt)
                    ─► 各方并行 claude -p ─► 校验 / 带错误重试 / raw_*.txt / 兜底「观望」
                    ─► GM 第一阶段：承诺概率（看不到骰子）─► 脚本判成败
                    ─► GM 第二阶段：描述后果、更新状态变量 ─► apply（原子提交）
               第 k 轮后：swap.json 分支（JSON-Patch 改权重 + 公开事件）
                                                                         │
        ┌──────────────────────────────┬─────────────────────────────────┤
        ▼                              ▼                                 ▼
 gamesim fork（压力测试）    gamesim aggregate / status         gamesim score / sweep（第二层）
        └──────────────────────────────┴─────────────────────────────────┘
                                         ▼
                                     report.md
```

方法论、每一步的要求和**踩过的坑**见 [`docs/METHOD.md`](docs/METHOD.md)。

## 命令

| 命令 | 作用 |
|---|---|
| `gamesim check E.json` | schema 校验 + 常见错误：第 0 轮已突破的红线、`-`+convex、按年轮次缺 `delta`、样本外命中率低 |
| `gamesim bdm E.json -o out.json [--md out.md]` | BDM 期望效用基线 |
| `gamesim run BASE --entities E.json [--runs 3] [--swap S.json] [--model sonnet] [--fake]` | 第一层多局推演 |
| `gamesim fork SRC_RUN DST_RUN --upto N [--events F.json]` | 复制前 N 轮、注入事件、继续推 |
| `gamesim status BASE` | 每局下一步 + 兜底和失败输出计数 |
| `gamesim aggregate BASE` | 目标命中 k/n、Wilson 95% 区间 |
| `gamesim score STRATEGY.json BASE [--runs …]` | 第二层策略臂打分、敏感性、分项拆解 |
| `gamesim sweep STRATEGY.json BASE --run R --from-round K --set VAR=v1,v2` | 覆盖状态变量找翻转点 |

引擎脚本也可以单独用：`python -m gamesim.turn init|prep|gm|roll|apply|rebuild`、`python -m gamesim.ingest`、`python -m gamesim.utility <run>`、`python -m gamesim.compare <run>`。

## 目录

```
gamesim/                引擎（标准库）与编排
  turn.py ingest.py     回合状态机（prep/gm/roll/apply/rebuild）、模型输出的解析与校验、兜底
  schema.py utility.py  entities.json 校验；效用（5 种形状、红线 −10）
  bdm.py aggregate.py compare.py target.py conditions.py dice.py fsio.py templates.py status.py
  runner.py             多局编排：claude -p、并行、重试、raw 输出落盘、兜底、分支 swap
  fork.py               压力测试分叉
  strategy.py           第二层：策略臂打分、敏感性、分项拆解、翻转点扫描
  cli.py                gamesim 命令
  fake_claude.py        假 claude（测试与演示）
  prompts/              回合与开局 prompt 模板（entity / game_master / gm_commit / utility_research …）
prompts/pipeline/       流程文档：intel.md、utility_research.md、review.md、alignment.md、outcomes.md
docs/METHOD.md          方法论 + 踩过的坑 + 审查案例
examples/hormuz/        6 方停火推演（entities.json）
examples/diaspora/      两层推演：entities.json（5 机构）、swap.json（大选分支）、strategy.json（家庭模板）
tests/                  引擎测试 + runner / fork / strategy / CLI 的假 claude 冒烟测试
```

## 局限与免责声明

- **推演不是预测。** 输出是在给定输入（参与方、效用、事实快照、情景前提）下，若干次 LLM 角色扮演的结果分布；3 局的频率区间很宽（0/3 的 Wilson 95% 区间是 0–56%）。
- LLM 玩家有合作偏差、会拒答（被兜底成「观望」），GM 会平滑冲击；坏情况要靠压力测试显式注入。
- 效用权重、暴露系数、分项公式都是假设；结论只在敏感性范围内成立。
- 信息隔离靠 prompt 拼装和禁用工具，不是安全边界。
- 示例里的事实截至 2026-11，不会自动更新；涉及真实国家、机构的设定是推演假设，不代表任何一方的真实意图或立场。
- **不构成投资、法律、税务或移民建议。** 涉及个人或机构决定，请咨询持牌专业人士。

## License

MIT，见 [LICENSE](LICENSE)。
