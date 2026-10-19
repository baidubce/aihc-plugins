---
name: baidu-add-model-server
description: "[runbook] 把一台能 SSH 的百舸开发机改造成具身评测可用的模型服务（MS），交付一台 L1 协议级冒烟通过的开发机 + 每个 benchmark 一条控制台启动命令，供用户自行保存为镜像后以自定义镜像提交评测。全程不改平台代码。触发：『把某个 VLA 模型做成 MS』『开发机改造』『自定义镜像接入评测』『继续接模型 / 恢复开发机改造』，或 MS 起不来 / `/config` 超时 / 首帧 act timeout / MS 节点不足。不适用：保存为镜像与推送仓库、做成控制台下拉里的平台预置模型、加评测集、改数据面。"
---

# 开发机改造成 Model Server

## 定位

**起点**：一台能 SSH 的百舸开发机，模型大概率已在上面手工跑通。
**终点**：一台**改造完成、L1 协议级冒烟通过**的开发机 + 控制台能填的启动命令（选了 N 个 benchmark 就是 N 条），供用户自行保存为镜像后
以**用户自定义镜像**方式提交评测。

**本 skill 只做两件事：连开发机、改开发机。** 产物只落在**开发机的可写层**（将来由用户在控制台
「保存为镜像」时带走），本地只留一份不入库的改造脚本工作副本。范围边界（做什么、不做什么）见文末「交付边界」。

**前置条件**（skill 不代办，缺了会停在步 1–2）：

- 已开通百度百舸 AIHC，并创建好一台开发机（[创建开发机](https://cloud.baidu.com/doc/AIHC/s/Tm6db1z9p)）；
- 开发机已开启 SSH：公钥已放进开发机，安全组放行 SSH 端口，本机能连通；
- 账号已开通具身评测，能在控制台以自定义镜像新建评测任务。

## 本 skill 的构成

全部内容自带，**不引用、不依赖任何其它 skill**：

| 文件 | 内容 |
|---|---|
| 本文 | **交互契约（八步 + G1–G5）** + 阶段 0–6 主干：连接契约、判档、硬约束、三条底线、观测对齐、交付边界 |
| `references/连接与授权.md` | **阶段 0 全文**：地址收集与命令规范化、TCP 探测、授权门禁、登录验证与失败对照表、跳板机、全程执行纪律、阶段摘要模板 |
| `references/Ray环境.md` | **补装 Ray 解释器**：为什么必须装、装哪、怎么不靠 ENV 进 PATH，实测通过的脚本 |
| `references/开发机改造.md` | **主流程脚本**：体检、上传、`setup.sh` 模板、运行时同口径离线自检、交付前清理 |
| `references/脚本模板.md` | 服务骨架：复用 harness `PredictModelServer` 或自己实现协议，两种形态 + 启动器 |
| `references/接入表单.md` | **阶段 0 输入 ②–⑥ 的 YAML 表单**：先选 benchmark、再按探测结果**预填**后整份回显，用户只补 `未提供` 行；含可探测字段与出处表、dm05→RoboDojo 的逐行填写示例、常见错填与判废规则 |
| `references/协议自检.md` | 逐条可验的协议与启动自检清单（policy protocol `protocol_version = 1`） |
| `references/评测集观测口径.md` | **观测速查表**：8 个常用 benchmark 的相机键名 / shape / state 字段名与维度 / 观测开关，可直接抄成 `obs.<benchmark>.json`。已按 `v0.5.0` 源码逐条复核；**仍是便利品不是权威**，落地前按阶段 3.5 的三个出处核一遍 |
| `references/恢复点.md` | 中断恢复：`form.yaml` / `PROGRESS` 格式（唯一定义处）、重新进入的读序、恢复分支表 |
| `references/改主意回退.md` | 中途改了前面的决定（G4 前 / G4 后两节）：改哪个 → 回退到哪步 → 作废哪些产物 → 重跑哪道门禁 |
| `references/冒烟测试.md` | **L1 协议级冒烟**（开发机本机，交付门禁）：前置检查表、两终端口径、就绪信号、判读，以及 Troubleshooting 表 |
| `scripts/probe_form_defaults.sh` | **可执行（只读）**：从用户启动脚本 / ckpt / 模型环境推断表单候选值，逐行 `字段<TAB>候选值<TAB>出处`；步 5 预填表单用 |
| `scripts/ray_env_check.sh` | **可执行**：Ray 解释器约束断言（`env -i` 复刻运行时口径）。改造后与交付前各跑一次 |
| `scripts/ms_selfcheck.py` | **可执行**：对已起好的 MS 跑协议级自动冒烟（`/config`、HELLO、两 episode 真观测计时、四种图像编码、畸形帧容错、session 隔离），逐条 PASS/FAIL，失败退出码 1。只需 `websockets` + `msgpack` + `numpy`（png/jpeg 另需 `pillow`），**不依赖 harness** |

## 规定的 harness 基线：tag `v0.5.0`

需要上游 harness 的服务写法（`add-model-server` 文档、`PredictModelServer` 的 chunk / batch 语义）或
benchmark 的观测口径时，**一律对齐公开仓库 `github.com/allenai/vla-evaluation-harness` 的 tag `v0.5.0`**
（commit `0fac650`）。本文与 `references/评测集观测口径.md` 的全部上游引用、行号都核对自这个 tag。

```bash
git show v0.5.0:src/vla_eval/benchmarks/<name>/benchmark.py | less   # 规定基线，可直接 checkout
```

- **`v0.5.0` 是可 checkout 的真 git tag**；镜像 tag 里的 `h050` 只是「对齐 harness `v0.5.0`」的书写约定，
  **它本身不是 ref**，要 checkout 请写 `v0.5.0`。
- **不要顺手读默认分支 `main`。** 仓库已有 `v0.6.0`–`v0.8.0`，`main` 的键名与行号都已前移，
  照它写 MS 会得到一份不报错的错分数。
- **确认开发机上装的是哪一版**：
  ```bash
  "$MODEL_ENV/bin/python" -c "import vla_eval,vla_eval._version as v; print(v.version, vla_eval.__file__)"
  ```
  不是 `v0.5.0` 时**不要默认跟随已装版本**：以 `v0.5.0` 为准核键名，并把实际版本与差异显式写进 `SETUP_INFO`
  （协议细节与观测键名在版本间会变，差一版就可能差一个键名）。


## 平台对镜像的两条硬约束

自定义镜像**不改变运行形态**：仍是同一个 RayJob 里的 `WorkerMSCustom` worker group 跑 `ModelServerActor`
（`num_gpus=1`），控制面的自定义镜像分支只换 `Image` + `ImageConfig`。所以：

1. **镜像必须能组 Ray 集群**：`ray[default]==2.49.2` + Python `3.13.12`，且**裸 `ray` 在镜像 PATH 上**
   （KubeRay 跑 `ray start` / `ray health-check`）。模型环境**不受**此约束，另起一套即可。
   见 `references/Ray环境.md`。
2. **服务必须满足协议**：同端口上 `GET /config` 返 200 + WebSocket msgpack。见 `references/协议自检.md`。

⚠️ 第 1 条**平台代码里没有任何校验**（声明式的 Ray/Python 版本常量没有读取方）。
不满足时的失败形态是：coordinator 按 hostname 子串 `-workermscustom-worker-` 找 MS 节点，等满 600s，
最后报「MS 节点不足」——离根因极远。所以这一条必须在改造阶段就断言掉。

平台固定 Ray head 口径为 `ray[default]==2.49.2` + Python `3.13.12`，必须 **patch 级一致**
（差一个小版本就连不上 GCS）。本 skill 钉死这两个值，不问用户、不靠开发机现有 ray 反推；
需要改时用 `RAY_VERSION=<x.y.z> PY_VERSION=<a.b.c>` 覆盖，并把本次版本记进 `SETUP_INFO`。

## 三条底线

改造开发机 ≠ 在 shell 里装东西。将来「保存为镜像」的本质是 commit 可写层，由此推出：

1. **镜像只带文件，不带 ENV**。你在 SSH 会话里 `export` 的 `PATH`、`UV_CACHE_DIR` 都不会进镜像。
   要钉住的东西只能靠**文件**：uv 缓存用 `/etc/uv/uv.toml` 的 `cache-dir`，可执行入口用软链进**镜像 ENV 里已有的** `bin` 目录。
2. **改造时是 root，运行时未必**。缓存、解释器、落盘资源都放 `/opt/*` 并 `chmod -R a+rX`，不放 `$HOME`、不依赖 `$HOME`。
3. **运行时无外网、无代理**。tokenizer、`trust_remote_code` 代码、JIT/编译产物、`gs://` 资源都要在改造时**真建一次并断言存在**。

推论：开发机上已有的手工调试痕迹（`~/.cache` 里的东西、`/root` 下的实验目录）**不能当作镜像内容的来源**。
脚本里用到什么，就由脚本自己产生什么——这样产物丢了能用同一份脚本在新开发机上原样重建。

## 交互契约

### 八步与停点

| 步 | 做什么 | 停下问用户？ |
|---|---|---|
| 1 | **开始前对齐**：一次列出缺的 ①开发机地址、④benchmark | **G1**，只问上下文里没有的；都有就不停 |
| 2 | 本地 TCP 探测 → 回显 SSH 命令 + 探测结果 → 取授权 | **G2** 授权远端执行 / 只生成命令 / 重输 / 取消 |
| 3 | 只读环境探测 | 不问：G2 已覆盖只读 |
| 4 | 判档 | 不问：结论并进 G3 一起给 |
| 5 | probe 预填 → **探测结论 + 预填表单一次回显** → 前置检查 | **G3**（选项见 5c） |
| 6 | 改造计划 | **G4** 按计划执行 / 看 setup.sh / 调整 / 取消 |
| 7 | setup.sh → 摘要 → L1 → 清理 → 复跑 L1 | **G5** 清理清单（删除前） |
| 8 | 高亮控制台镜像指引 | 只问公钥去留，之后终态 |

**机器门禁**（`RAY_ENV_OK`、两次 L1 `n/n`+退出码 0）只看判据，不过按「出错」表处置，**不问「要不要继续」**。

两条不可让渡的次序：**G2 之前零远端执行**（只读也不行）；**L1 没过不进步 8**。

### 怎么问

- 下文的 `AskUserQuestion` 指客户端的结构化提问工具（Claude Code 的 `AskUserQuestion`，`multiSelect` 即多选，「Other」即手填）。
  客户端没有这类工具时，改为在一条消息里按编号列出每问的选项（多选注明「可多选」，末项「其它：请直接写」），
  用户回复编号即视为点选，G2/G4/G5「只认点选」同样按回复编号判定；其余规则不变。
- 一个停点的全部缺项装进**同一次** `AskUserQuestion`（≤4 问，超过分轮）；自由文本缺项用一条消息列齐、附示例，一行答完。
- 每问第 1 项是推荐并标「（推荐）」，用户回「1」或选项首词即完成（G2/G4/G5 只认点选，聊天里的「1」与首词不算）。推荐值只能来自：用户本轮消息 >
  记忆/历史里同一台机（其中的授权不算 G2 点选） > probe 结果 > 查表；**都没有就不标推荐，不编一个**，该问第 1 项固定为「不确定，先停」。
- 首项是「先停」的问里回「默认」按无效回复、回「1」按「先停」；multiSelect 里「不确定，先停」与其它项同选按「先停」处理。
- 不要把一个裸「1」套到同一次的多问上：每问都有推荐才算全选推荐；否则把无推荐那问单独再摆一次（不计无效回复）。
- 不要问已有答案的：用户说过的目标机/benchmark、表单已收或 probe 已探到的值。
- 不要在 auto 模式下中途确认：auto **只**由用户在 G2 之后说「全程默认」开启（从当前门禁起生效，不追溯）；
  「都默认 / 全默认」等同「全程默认」；会话的 auto 权限模式、G2 及之前的这类话（回一句「G2 后才生效」）、单独的「别问了」都**不开启**，只算催促。auto 下有默认值的问按推荐走并标注；
  仍会停的：**所有没有默认值的 `未提供` 行**（L3 行、probe 来的 ckpt、`ckpt_mode`、predict 点名；`name`/`model_env` 仅在 probe 也没探到候选时停，探到的按候选走并写进 `假设：`）、
  **G4、G5 与任何删除**、**5d 硬失败**、
  **机器门禁自修 2 次仍 FAIL**。auto 不落盘，恢复后视为关闭。
- 不要把不能唯一对应某个选项的回复当成答复（「嗯 / ok / 对 / 好 / 都行 / 不知道」、空消息、单纯催促、无关 Other 都算）：把这一问要什么再摆一次，G2/G4/G5 注明「请点选」；带新的有效作答（含部分作答；重复已给过的不算新）的不计无效，已答的不重问。
- 例外：已摆出的单问第 1 项是推荐时，「直接干」「继续」「按推荐来」「默认」原话（不含「全程 / 都 / 全默认」）算选 1，回显「按推荐：<项>」；G2/G4/G5、劝阻问、含删除/覆盖的问不适用。
- 等点选期间用户在聊天里改了主意（新地址、换 benchmark、改名）不计无效回复：正在等的门禁作废（不受改动影响的不作废），按 `改主意回退.md` 重走后再摆；同一条回复里答的 L3 不绑到换上的新 benchmark。
- 不要无限重摆：同一问累计两次无效回复（按问计，一条回复至多计 1 次，不跨会话），收敛成「推荐项 / 取消」，无推荐的不删实质选项、只末项加「取消」；首项已是「先停」的不重摆，第二次无效即按「停在当前步」给终态；例外：G1 缺 ① 第 2 次无效即按「停在当前步」给终态（不写 `$W`），G5 为「按清单全删 / 不清理，停在步 7」，单项删除问两次无效即按「保留」继续，选目录用「不确定，先停」代替取消；收敛后只剩一项的不摆，按「停在当前步」给终态；
  G3 的 L3 行、probe ckpt、predict 点名与无候选文本行以 5c「L3 要有终态」为准，不用这条；其余收敛后再无效，以「停在当前步」为终态。

### 阻断 vs 标注

**错了会静默错 → 阻断；错了会大声报错、或只影响呈现 → 标注（照默认做，结果里写明）。**
拿不准算哪类时当阻断。

| 阻断 | 标注 |
|---|---|
| 目标机、SSH 授权、benchmark、ckpt 与 `ckpt_mode`、L3 模型口径、改造计划、覆盖已有目录、任何删除 | SSH 用户默认 `root`/无私钥、`LAUNCH` 启动脚本猜测、本地工作目录名、报告格式、排查顺序、缺 `sample_action` |

标注统一写在该步输出末尾，终态报告再汇总一次：

```text
假设：
- <X>（依据 <出处>；不对就说，会影响 <Y>）
```

### 过程反馈

- 每完成一个逻辑阶段只报一行结果：`✓ <完成了什么，带关键数字> → 接下来 <Y>`。
- 默认输出 ≤500 字符。例外：G3 的 YAML、各步规定的表与计划块、FAIL 时的失败用例名 +
  关键报错行、步 8 的命令块。数字不省。
- 每个终态（完成 / 取消 / `dry_run` / 停在缺项 / FAIL）最后一行固定是 `下一步建议：…`（步 8 公钥问除外）。
  G4 之后、步 8 之前的终态在它上一行加一句 `⚠️ 此可写层未完成清理与复检，勿保存为镜像`。

### 出错

| 类别 | 例子 | 处置 |
|---|---|---|
| 可自愈 | ssh 瞬断、heredoc 转义错、`LAUNCH` 猜错、偶发超时 | 自己修，**静默重试 1 次**；再失败按下一行处理 |
| G4 内可修 | L1 FAIL 且修复只动 G4 已批准的落点 | 算已批准，不另问；每道机器门禁、每个 benchmark 各计**至多 2 次**（重跑一次 `setup.sh` 算 1 次）；不许改 `form.yaml` 来源的值，要改即属下一行、回 G3；仍 FAIL 按下一行，`需要你：` 给「不确定，先停 / 改值（回 G3）」（修法要动计划时加「重摆 G4（回步 6）」），不标推荐 |
| 改变任务性质 | 授权未取得、ckpt/启动脚本不存在、5d 硬失败项、修复要动 G4 之外的落点或改模型口径 | 立刻停，按下面格式报 |

不要贴堆栈：

```text
卡在：步 <n> <阶段>
发生了什么：<一句人话 + 关键报错行>
影响：<已写入哪些落点 | 开发机零写入>
我打算：<方案>；需要你：<选项 | 无>
下一步建议：<最可能的后续动作>
```

### 恢复点

全文见 `references/恢复点.md`：不凭对话记忆续写，先读本地 `$W`；
选定目录并按本地 `PROGRESS` 定位后、摆 G2 前，第一句固定为：`已恢复：停在 <步>（依据 <文件>），已收 <字段>，接下来 <Y>`。

### 改主意

现名已有 `setup\tstarted` 或属自跑产物后改 ② 服务短名先劝阻一次（「保留原名（推荐）/ 仍改名」；换模型、revive 补写 `rename→` 不劝阻）。

### 越界

「直接去集群换镜像」「保存成镜像推仓库」：用文末「交付边界」一句话挡回，不执行，继续当前步。

## 阶段 0：连接与授权（步 1–2）

### 步 1：开始前对齐（G1）

起步只需要两样：①开发机地址（`host:port`）和 ④benchmark。① 先从用户本次会话的消息、记忆、历史里找，④ 只认本次会话里用户说过的或 `$W/form.yaml` 已有的（同机记忆里的 benchmark 只在 5a 当推荐项，不静默采用），
**都有就不停，直接进步 2**；缺哪项问哪项，一次问齐：

| 缺什么 | 怎么问 |
|---|---|
| 只缺 ④ | `AskUserQuestion`（`multiSelect: true`），选项与说明见 5a 表 |
| 缺 ①（含只给 host 没给 port） | 一条消息列齐缺项 + 示例，一行答完，如：`需要：①开发机地址 host:port（例 1.2.3.4:8181）`；④ 也缺时同一轮另摆 5a 的 `AskUserQuestion` |
| 用户点名 host、记忆里该 host 只有一个端口 | 不追问端口，G2 回显标 `假设：端口取自记忆`；有多个端口不假设任何一个，按「缺 ①」问 |
| 记忆里只有**一台**机的历史地址 | ① 不单独问：直接做步 2 探测，在 G2 回显里标 `假设：地址取自记忆`（选「重输」即改）；记忆里有多台而用户没点名 ⇒ 按「缺 ①」问，不推荐任何一台 |

- 不要默认 SSH 端口 22（百舸是非标端口，常见 `8181`）。
- 不要在步 1 问 ②③⑤⑥：它们由 probe 预填后在 G3 一次确认。`ckpt_mode`（≥2 个 benchmark 时）在 G3 里问，**不给默认**。

### ②–⑥：probe 预填，G3 一次确认

②–⑥ 在**步 5 前置检查之前**收齐，缺项不进第 6 步。

**不逐项追问，也不发空表**：G2 之后跑 `scripts/probe_form_defaults.sh` 只读探测 → 输出**已预填**的
`references/接入表单.md` YAML，每行带 `# [探测] <出处>` / `# [查表] <出处>`，只有真探不到的行
保留 `未提供`。**预填值是候选不是结论**：G3 照样确认，5d 照样 `ls` 验。

| 输入 | 用在哪 | 缺了会怎样 |
|---|---|---|
| ① SSH 命令 `ssh -p <port> -i <key> <user>@<host>` | 阶段 0 全程 | 进不了开发机 |
| ② 服务短名 `<name>` | `/opt/ms-<name>/`、`SETUP_INFO`、启动命令 | 路径无法确定，中途改名要全量返工 |
| ③ ckpt 在**开发机上的真实位置**；≥2 个 benchmark 时另定 `ckpt_mode`（复用同一份 / 分别指定） | 阶段 5 自检、交付的启动命令 | 猜路径或给默认值 ⇒ 加载到错的权重也不报错。平台挂载路径**不在此处收**：由提交评测时的 `ckptPath` 决定，服务只经 `--ckpt` 取，yaml **不写** `checkpoint` |
| ④ 目标 benchmark（**可多选**）：名称 + sub_bench + 用户侧启动参数（任务子集/seed/渲染类） | 每个 benchmark 一份 `<benchmark>.yaml` + `obs.<benchmark>.json`、阶段 3.5、阶段 5 | 观测口径与观测开关**不向用户要**——采信用户给的相机键名会让自检自洽通过、上平台全 0 分。详见 `接入表单.md` |
| ⑤ harness 实际版本（`vla_eval._version.version`）与基线 `v0.5.0` 是否一致 | 阶段 3；写进 `SETUP_INFO` | 协议细节按错版本写；观测键名在版本间会变 |
| ⑥ 模型侧推理对接：conda env、最小 predict 片段、运行时资源 + 模型侧约定（图像预处理、state 打包、action 语义、chunk、per-episode 状态、要 BA（Benchmark Agent：评测时运行 benchmark 仿真环境、作为客户端连 MS 的一侧，跑在另一个 Pod）发什么） | 阶段 3 写 `predict`、阶段 3.5、阶段 5 人工核对 | 猜错不报错、产出看起来正常的错分数；「要 BA 发什么」漏了就收不到 state / 腕部相机 |

### 步 2：TCP 探测 + 授权（G2）

本地 `nc -z` 探 TCP 端口（不登录、不碰私钥，可直接跑）→ 把**拼好的 SSH 命令（私钥只显示路径）
+ TCP 结果 + 用户名等标注**一起回显，在同一道 G2 里取授权（授权执行 / 只生成命令 / 重输 / 取消）→ 授权后才
`echo connected` 验登录。TCP 不通就不摆 G2，按「出错」格式报，让用户查开发机状态/安全组。
`dry_run` 下一条都不执行：输出步 3 体检与 5b probe 的完整命令，
请用户自己跑完把输出贴回，再据此预填 G3；明说不贴回或两次无效（按「怎么问」计）即以 `dry_run` 终态结束并写 `stop\t2\tdry_run`。G3 之后的 `dry_run` 输出改造计划与 `setup.sh` 全文即终态，不进 G4。

命令规范化、失败现象对照表、跳板机处理、全程执行纪律、阶段摘要格式——见 `references/连接与授权.md`。

## 阶段 1：环境探测与判档（步 3–4）

**步 3 不问人**：G2 授权的就是远端执行，体检全程只读（不安装、不写盘、不改任何文件），授权后直接跑，
输出一行进度即可。

脚本见 `references/开发机改造.md`「体检」。输出要回答这几个问题：

| 问题 | 不满足的后果 |
|---|---|
| 镜像 ENV 的 `PATH`（从 `/proc/1/environ` 取）里有没有 `ray`？是哪个 Python？ | 决定要不要补装、软链往哪放 |
| `PATH` 里有没有一个**在根文件系统上**的目录可供软链（如 `/usr/local/bin`）？ | 没有 → 镜像 ENV 改不了，裸 `ray` 进不了 PATH |
| 模型跑在哪个环境、ckpt 在哪、依赖哪些远端资源（tokenizer、remote code、`gs://`） | 运行时无外网，漏一个就炸 |
| 关键路径（`/opt` `/etc` `/root` 与模型目录）在根盘还是挂载盘上？ | 在挂载盘上（PFS/NFS）→ **不进镜像** |
| 运行时用户是谁、HOME 是什么 | 自检口径对不上集群 |
| 根盘可用空间、可写层已多大 | 可写层越大，用户将来「保存为镜像」越慢、越容易失败 |
| 有没有代理、token、凭据落在文件里 | 进镜像即泄露 |

判档（体检只是预判，**最终由阶段 2 的脚本自己探测并提示**，不要靠人眼下结论）：

- **轻档**（基础镜像本身已满足 Ray 约束，常见于上一轮改造过的镜像）：`/opt/conda`（或别处）里已是
  Python 3.13.12 + ray 2.49.2 且 `type -a ray` 第一个就是它 → 跳过阶段 2。
- **重档**（其他镜像，如 NGC / 裸 PyTorch）：走阶段 2。本 skill 的主场景就是这一档。
  ⚠️ 重档 **不等于一定要下载**：别人改造过的镜像、自建 conda、上一轮改造的残留都可能已满足约束，
  脚本会先探测并打 `RAY_REUSE:` / `PY_REUSE:`，命中就零下载、不需要外网。
- 判定口径只有一个：`env -i PATH="<镜像 ENV 的 PATH>" HOME=/root bash -c 'command -v ray'`（`command` 是 shell 内建，不能直接交给 `env`）。
  SSH 会话里 `ray --version` 正常不算——那常是 `export PATH` / `conda activate` 撑起来的，镜像带不走。

### 步 4：判档结论并进 G3

步 4 不单开一问：判档表直接放在 G3 回显的最上面，用户看完探测结论与预填表单后**一次**决定。
满足项合成一行带过，只逐行展开不满足项；原始输出不给，结尾写「要看完整探测原始输出说一声」。

```text
环境探测结果（只读，未做任何改动）
| 项 | 现状 | 满足 | 不满足的后果 |
| PATH 上第一个 ray | <路径|无> | ✗ | KubeRay 只认它 → 任务等满 600s 报「MS 节点不足」 |
| Python 3.13.12 | <已有于 …|无> | ✓/✗ | 版本不一致连不上 GCS，不报版本错 |
| 其余已满足 | 可软链目录 <…> / 模型环境 <…>（torch <…>）/ ckpt <…>（根盘|挂载盘）/ 根盘可用 <GiB> / 代理或凭据落盘 <无> | ✓ | — |
判档: <轻档：已满足 Ray 约束，可跳过补装|重档：需补装 Ray 解释器，需/不需外网>
```

## 步 5：预填表单 → G3 → 改造前置检查

### 5a. benchmark 选项（G1 用）

benchmark 决定 ④ 的全部默认值，所以在 G1 就定下。G1 里要问时用 `AskUserQuestion`（`multiSelect: true`）：

| 选项 | 说明（写进 description） |
|---|---|
| robodojo | 3 相机 480×640 / `state` 14D / action 14D |
| robotwin | `head_camera` 等 3 相机 / `joint_state` 14D / action 14D |
| libero | `agentview`(+wrist) / `states` 8D / action 7D |
| calvin | `rgb_static`(+gripper) / `states` 8D / action 7D |

其余（simpler / libero_pro / robocasa / behavior_1k）走「Other」手填。说明列的数字取自
`评测集观测口径.md` §2，不凭记忆写；不在该表已核实清单里的名字**没有默认值**，按 §3.9 现查。
本次所用那台机的记忆里出现过的 benchmark 排第 1 并标「（推荐）」（别的机、别的模型用过的不算）；都没有就不标推荐，第 1 项放「不确定，先停」。

### 5b. 只读探测预填（G2 已覆盖，不问）

```bash
<SSH_CMD> -o BatchMode=yes 'bash -s' < <skill目录>/scripts/probe_form_defaults.sh
# 启动脚本猜错时（可自愈，静默重试一次）：<SSH_CMD> -o BatchMode=yes 'LAUNCH=/root/run_x.sh bash -s' < …
```

按 `接入表单.md`「可探测字段与出处」把 TSV 逐行映射进 YAML，④ 的 benchmark 侧字段按 §2 查表填。

### 5c. G3：探测结论 + 预填表单，一次确认

回显顺序：判档表 → 预填 YAML（每行带出处注释）→ `假设：` 块 → 一次 `AskUserQuestion`（选项题）+ 至多一条
文本题，槽位固定如下，**不逐行单独问**：

1. **表单**：采用预填值（推荐）/ 要改某几行 / 只要探测报告，不改造（结束，零写入）。
   ⑥B 中由 predict 推出的行（`action_dim`/`chunk_len`/`camera_slot_map`/`image_*`）在 predict 点名前标「待核」，
   「采用预填值」不采信它们；点名后按该函数重推，与预填不同的行先写成 `重核 <重推值>`、单独一问「重推值（推荐）/ 改某几行」，不要静默采用；无差异不问。
2. **probe 来的 ckpt**（仅当 ③ 不是用户主动给的）：回显该目录关键文件（权重文件名/大小/mtime），
   选项为「不确定，先停」+ 探测到的候选路径（都不标推荐）+ Other。「采用预填值」**不覆盖**这一行：`ls` 在 ≠ 是对的那份权重。
3. **L3 缺项**（`state_layout` / `action_semantics` / `action_denormalize_by` / `runtime_downloads` 的显式结论，
   以及 predict 片段缺失或跑不通时的 `action_dim` / `chunk_len` / `camera_slot_map`）：有闭集的
   （`action_semantics`、`action_denormalize_by`、`runtime_downloads` 的 `none`）做成选项、**不标推荐**、第 1 项「不确定，先停」；
   开放的（`state_layout`）列在同一条消息里给格式示例，一行答完。
4. **仍 `未提供` 的 L1 行与 ≥2 个 benchmark 时的 `ckpt_mode`**：`ckpt_mode` 做成选项（不确定，先停 / reuse / per_benchmark，不标推荐）；
   `name` / `model_env` / `sub_bench` 有候选的做选项；predict 点名的候选**不标推荐**，第 1 项固定「不确定，先停」；以上无候选的并进第 3 问那条文本，一行答完。
   选项题超过 4 问时按需分轮（每轮 ≤4 问；多 benchmark 的 ⑥B 按 benchmark 分组），文本题始终只一条。

L3 行与 probe ckpt **无默认值、不许猜、不许用看起来合理的值糊过去**。
每答一行即回写 `$W/form.yaml`，确认后置 `g3_confirmed: true`，`PROGRESS` 追加一行。

**L3 要有终态**：用户累计两次答不出同一行（按行计，不跨会话，催促与「嗯 / ok / 不知道」都算）时，收敛成一问：
导出待补清单并停（推荐）/ 补填这几行 / 取消；第二次收敛不给「补填」。
「导出」输出待补清单——哪几行仍 `未提供`、为何不能默认、从哪能查到（`state_layout` 问模型作者或训练脚本里
state 的打包顺序，`action_semantics` 看训练配置的 delta/absolute 与夹爪符号，`action_denormalize_by` 看 predict 里有无反归一化与 `unnorm_key`，
`runtime_downloads` 看用户启动日志里的下载 URL 与 HF cache，predict 点名与 probe ckpt 问用户实际跑通的函数与权重，`action_dim`/`chunk_len`/`camera_slot_map` 看 predict 的返回形状与图像索引）——并写明「改造尚未开始、开发机
零写入，补齐后回 G3」，清单同步写进 `form.yaml`。收敛后再无效按「导出」处理，给 `下一步建议：`，不再摆。

`sample_action` 是**唯一**可降为标注的 L3 项（只供人工核对量级、不改分数）：缺失就进 `假设：` 块写
「未提供样例动作，人工量级核对跳过」，不要为它停。

### 5d. 改造前置检查

G3 通过后的**第二道只读检查**（不问人），与步 3 分工不同：步 3 答「这台机器是什么样」，
5d 答「按计划改造会不会失败、会不会覆盖已有东西」。结论全部带进 G4 的计划，**不为单项另开一问**；
标「硬失败」的按「出错」格式立刻停，`需要你：` 固定三项、不标推荐：不确定，先停 / 重探（已处理）/ 取消；
不给「合并进计划先跑」。重探至多 2 次仍硬失败就以「停在当前步」结束，不再给「重探」。

| 检查项 | 不通过怎么办 |
|---|---|
| ②`<name>` 已定且 `/opt/ms-<name>` 不存在或内容可覆盖 | 已存在 → 计划里标「覆盖 <目录>（现有 <n> 个文件、`setup_sha256=<…>`）」，G4 按含覆盖的形态摆（见步 6），「调整计划」可换名 |
| 四个落点 `/opt/{uv-bin,uv-python,ray-py,uv-cache}` 当前状态 | 已存在且满足约束 → 计划里标 `REUSE`，不重装 |
| `/etc/uv/uv.toml` 是否已存在且没有一行恰为 `cache-dir = "/opt/uv-cache"`（与 `setup.sh` 同一 `grep -qx` 口径；ray 全 REUSE 时不查） | 存在且未钉或钉了别的值 → **硬失败**，用户人工处理后重探，不由 skill 合并；提示里给可直接粘贴的修法（把该文件的 `cache-dir` 行改成、或补一行 `cache-dir = "/opt/uv-cache"`，其余行不动） |
| 软链目标目录在镜像 PATH 上，且 PATH 上**第一个** `ray` 满足约束或就是软链（排在后面的其它 `ray` 不算失败） | 第一个 `ray` 不满足约束 → **硬失败**（KubeRay 只用它）；路径按上面 `bash -c` 口径取（不用 SSH 会话的 PATH）；修法只给「把软链目标改到它之前的 PATH 目录」，没有这样的目录就不给重探，建议换基础镜像；不给「移出 PATH」，该 `ray` 在模型环境里时不给卸载命令 |
| 根盘可用空间 ≥ 补装所需（无复用时约 2–3 GiB） | 不足 → 停下，先让用户清理或扩容 |
| 外网可达性（仅在确实需要下载时才成为阻塞项） | 直连不通 → 提示显式传 `SETUP_PROXY=http://host:port`，不默认配代理 |
| ③ckpt 在开发机上真实存在（`per_benchmark` 时每条路径都 `ls`）；选了 ≥2 个 benchmark 时 `ckpt_mode` 已定 | 缺失 → 按「改变任务性质」停，回 G3 改该行，不用默认值糊过去 |
| ③ckpt 来源是 probe 预填时，G3 第 2 问已被用户正面认过 | 没认过 → 回 G3 第 2 问，不在这里补 |
| ④benchmark 已选定，其观测口径能落成 `obs.<benchmark>.json` | 缺失 → 回 G3；多选时每个 benchmark 各一份 |
| ⑤开发机上实际装的 harness 版本已读到（与基线 `v0.5.0` 的差异记进 `SETUP_INFO`） | 读不到 → 按 `v0.5.0` 核键名并在 `SETUP_INFO` 里注明未读到 |
| ⑥模型侧推理对接信息齐全；模型环境里 `websockets`/`msgpack`/`pillow` 现状 | 依赖已齐 → 计划里标 `REUSE`，零下载 |

这一步**仍然一个字都不写盘**。检查项与探测口径的实现见 `references/Ray环境.md` 与 `references/开发机改造.md`。

## 步 6：改造点计划（G4）

把前置检查的结论翻成一份**逐条可核对的改造计划**给用户，每条都要带「落点 / 动作 / 是否需外网 / 可否回滚」：

```text
改造点计划（共 N 条，全部落在开发机可写层）：
[1] /opt/ray-py            新建 venv + 装 ray[default]==2.49.2   需外网:<是|否(REUSE)>  回滚:删目录
[2] /opt/uv-bin,/opt/uv-python,/opt/uv-cache  uv 本体/解释器/缓存   需外网:<是|否>      回滚:删目录
[3] /etc/uv/uv.toml        新建 cache-dir=/opt/uv-cache（已钉对标 REUSE 不写）  需外网:否  回滚:仅本次新建的才删
[4] /usr/local/bin/{ray,uv} 软链（进镜像 ENV PATH）               需外网:否            回滚:删软链
[5] /opt/ms-<name>/        server.py / serve.sh / _base.yaml /
                           <benchmark>.yaml × N / obs.<benchmark>.json × N /
                           ray_env_check.sh / ms_selfcheck.py / SETUP_INFO
                                                                  需外网:否            回滚:删目录
[5b] /opt/<name>-cache/    运行时资源落盘（tokenizer / remote code / 编译产物；无远端资源则空）
                                                                  需外网:<是|否>       回滚:删目录
[6] <模型环境>              pip install websockets msgpack pillow  需外网:<是|否(REUSE)> 回滚:pip uninstall
不动：模型权重、现有 conda 环境的推理栈、/root 下既有实验目录
预计耗时：<x> 分钟；执行方式：一份幂等 setup.sh 经 heredoc 远端执行，可整份重跑
```

再用 `AskUserQuestion`：按计划执行改造（推荐）/ 先看 `setup.sh` 全文 / 调整计划（换 name / 改落点 / 跳过某条）/ 取消。
计划含覆盖已有目录或 `rm` 条目时**不标推荐**：不确定，先停 / 按计划执行（含覆盖 <目录>）/ 先看 `setup.sh` 全文 / 调整计划；
当场「调整计划」换 name 是否算改名按 `references/改主意回退.md`「改名」判。

- 前提是②–⑥ 已全部收齐（无 `未提供`/`待核`/`重核`）；缺项回 G3 补，不在这里猜。用户再急也不跳过表单直接进计划。
- **G4 之前零写操作。** 选 1 即覆盖步 7 的全部安装与写盘（含 `setup.sh` 每一段），执行中**不再逐段二次确认**；
  只有删除（G5）和计划外的写动作才另问。
- 摆出后超 1 小时才点选的（G5 同理），先只读重跑 5d / 重列清单，有差异就重摆。
- 选 1 后 `PROGRESS` 追加一行，输出 `✓ 计划已确认 → 接下来上传 setup.sh 并执行（预计 <x> 分钟）`。

## 阶段 2：补装 Ray 解释器（步 7 执行）

全文见 `references/Ray环境.md`（落点 `/opt/ray-py`、uv 本体与 `/etc/uv/uv.toml` 缓存钉定、`env -i` 复刻运行时
口径的断言，断言已固化成 `scripts/ray_env_check.sh`）。两条不能在压缩中丢的：

- **先探测可复用，再决定装**：「要不要联网」是探测的结论、不是前提。PATH 上第一个 `ray`、别处
  （conda / 既有 venv / `/opt/ray-py`）的 ray 已是 `ray[default]==2.49.2` + Python `3.13.12`，或机器上已有精确
  `3.13.12` 解释器——任一命中即零下载、不需要外网（脚本打 `RAY_REUSE:` / `PY_REUSE:`），直接收口于断言。
- **PATH 上有没有 `ray`，走的是两条相反的路**：没有 → 把 `/opt/ray-py/bin/ray` 软链进镜像 ENV `PATH` 上
  **已有**的根盘目录（通常 `/usr/local/bin`）即可；第一个 `ray` 存在但**不满足**约束 → **硬失败**，
  不许用软链去盖（KubeRay 只认第一个）。

## 阶段 3：写 model server（步 7 执行）

服务骨架、`predict`、specs、chunk、batch 的写法见 `references/脚本模板.md`，
逐条自检见 `references/协议自检.md`。最容易踩的四条：

1. **端口不能写死**，进程里读平台注入的 `MS_PORT`——规则与 envs 黑名单见「步 8」。
2. **探活是 `GET /config` 不是 `/health`**，与 WebSocket 同端口（平台 2s 探一次、900s 上限）；
   权重加载 + warmup 必须在开始监听**之前**做完（`/config` 200 后平台立刻发首帧，默认只等 30s）。
3. **ACTION 永远是 1-D 单步** `(action_dim,)`；chunk 只在服务内缓存逐帧 pop。
4. **per-episode 状态按 `session_id` 分桶**（多 shard 共享一个 MS），`EPISODE_START` 清空；
   推理异常回 `ERROR` 而**不是**零动作、不关连接。

raw 路径**不注入** `HF_HOME` / `HF_HUB_OFFLINE` / `HF_MODULES_CACHE`，要就自己在控制台 envs 里给
（`HF_MODULES_CACHE` 必须可写）。

## 阶段 3.5：观测对齐（**最容易静默错分数的一步**）

模型内部的相机命名（训练口径，常叫 `image_prompts` / `image_keys` / `cam_high`）与
**benchmark 实发的观测键名**是**两套独立命名空间**，不要假设它们相同。必须在 MS 里
显式做「观测键名 → 模型槽位」的映射。

### 真实键名只有三个可信来源

出处与版本口径见本文「规定的 harness 基线：tag `v0.5.0`」——**按 tag `v0.5.0` 核**，
开发机上装的若不是这一版，把实际版本与差异写进 `SETUP_INFO`。
按可信度排序，**不要靠开发机上手工跑通时用的那套**（那常是作者 demo 的另一套预处理）：

1. 同 benchmark 的参考 MS 实现 `src/vla_eval/model_servers/<name>.py`
   （如 robodojo 的 `robodojo_pi05.py:70` `_CAMERA_MAP`：key 是**模型侧槽位**
   `cam_high`/`cam_left_wrist`/`cam_right_wrist`，value 是可接受的**观测键名候选**，
   `cam_high` 槽位接受 `cam_head` 或 `cam_high`——这张表的形状本身就是「两套命名空间」的证据）
2. benchmark 实现本身 `src/vla_eval/benchmarks/<name>/`（看它往 `Observation` 里塞了什么键）
3. `docs/reproductions/<name>.md`

常用 benchmark 的观测口径已整理成速查表：`references/评测集观测口径.md`，可直接抄成 `obs.<benchmark>.json`。
**表是便利品不是权威**：落地前仍按上面的出处核一遍。

同时要对齐的不止键名：**图像是两层** `obs["images"][cam]`（8/8 benchmark 无例外，顶层只有
`images` / state / `task_description`）、RGB/BGR、HWC/CHW、归一化、图像翻转、state 维度与打包顺序、
`task_description` 的字段名。**线缆上的 state 字段名按 benchmark 三分**：`state`（robodojo；robocasa365
是**嵌套 dict** 不是向量）、`states`（libero / libero_pro / calvin / behavior1k / simpler；robocasa
在 v0.5.0 根本不发、v0.8.0 起才发 9D）、`joint_state`（robotwin）
——payload 就是 benchmark 的 `make_obs()` 返回的那个 dict 一字不改过线，没有统一字段名（对照表见
`references/评测集观测口径.md` §4）。`obs.<benchmark>.json` 里的 `states` 是**维度声明**，字段名由 `state_key` 给出。
MS 优先按该 benchmark yaml 里写死的 `state_key` 取；兼容写法 `obs.get("state", obs.get("states", obs.get("joint_state")))` 只作后备，**三个都缺就抛错、不要补零**
——零状态常被模型当合法输入，把维度/量纲问题盖成一份错分数（上游 `model_servers/oft.py` 的
`np.zeros(PROPRIO_DIM)` 兜底正是这种写法，别学）。
**上游没有一个 MS 兜全三种键名**：`pi0`/`oft`/`groot`/`starvla`/`lerobot` 只兜 `states`/`state`，
`dexbotic/cogact` 只读 `joint_state`，而 `xvla` 给 robotwin 配了 `send_state: True` 却不读
`joint_state`——照抄任何一个都会在某个 benchmark 上静默拿不到 state。
state 发不发由 MS 自己在 `HELLO.observation_params` 里声明（`send_state` / `send_proprio`）：
没声明就不会发，MS 却按必填读 → 每帧 ERROR（X-VLA on SimplerEnv 实测掉到 0%）。

### 禁止改模型侧参数去迁就观测键名

这是最省事、也最危险的做法。模型侧的相机名往往**逐字进模型输入**，改了它不会报错，
只会出一份错分数。dm05 的 `--cameras` 就是这样一个**双职责参数**：

- `build_slots()` 按它逐名去 obs 里取图
- `build_cmd()` 又把它原样追加给上游 `--inference-config.image-prompts`
- 上游 `transforms.py` 里 `label = f"{prompt} image: "` —— 逐字进模型文本 prompt
- 而 RoboDojo 的训练口径固定是 `["Head", "Left wrist", "Right wrist"]`

所以把它改成 `cam_head,cam_left_wrist,cam_right_wrist` 虽然能让报错消失，却把模型输入
文本从 `Head image: ` 改成了 `cam_head image: `，与训练分布不符 ——
**这正是「三个没有报错但分数是错的」第 2 条**。正确做法是解耦双职责：模型侧参数保持
训练口径不动，另加一张别名表做查找，缺则**显式报错**。落地写法见 `references/脚本模板.md`。

### 自检口径必须等于真实口径

`obs.<benchmark>.json` 里写模型内部命名，会让 L1 自检**自洽通过**而上平台每个 episode 都 ERROR ——
自检发什么、MS 就按什么取，两边用同一套假键名，错误被完全掩盖。
`ms_selfcheck.py` 为此有一道机械防线：**不给 `--obs-spec` 时 `obs_spec` 用例直接 FAIL**，
并校验每路 shape 是 `[H, W, C]`、`states` 是显式整数、`task_description` 非空；
HELLO 阶段还会在「MS 声明的键名」与「将发送的键名」零交集时打一条 INFO 提示。
`--skip obs_spec` 只用于快速试跑。**交付门禁（步 7 末 L1、阶段 6 清理后复跑）一律不许带任何 `--skip`**——
唯一例外是缺 `pillow` 时 png/jpeg 两条自动降 INFO 跳过。用户要求跳过其余任何用例时当场拒绝，
并说明该用例验的是什么真机缺陷（session 隔离、畸形帧容错、图像编码等），不许靠 `--skip` 把 n/n 稀释成伪通过。

### 多选了 N 个 benchmark：一个 benchmark 一份 yaml

**不要**写一份「能自适应所有 benchmark」的服务：相机键名、每路 shape、state 维度、线缆上的 state 字段名
（`state` / `states` / `joint_state`）按 benchmark 三分，塞进一份配置只能靠运行时嗅探键名，而嗅探在键名
缺失时会猜到另一个 benchmark 的槽位上，**产出一份看起来正常的错分数**。一 benchmark 一份 yaml，缺键显式报错。

四条不变量，落地形状（`_base.yaml` + `extends` + `args` 分层、`cameras` 槽位表、yaml 不写 `checkpoint`、权重只经 `--ckpt`）
见 `references/脚本模板.md`「多 benchmark 配置」：

- `/opt/ms-<name>/` 下落 **N 份 `<benchmark>.yaml` + N 份 `obs.<benchmark>.json`**，命名与 schema 对齐上游
  `configs/model_servers/<ms>/<benchmark>.yaml`，不自创；
- `serve.sh` 以 `--config <path>` 选 benchmark：**有多份而没传就显式报错退出**，不要猜；交付与自检的命令一律显式带 `--config`（单 benchmark 也带，减到 1 个时残留 yaml 不会让命令变义）；
- L1 冒烟对**每一份** yaml 各跑一遍（`--obs-spec` 换成对应那份），**全绿**才算过门禁；
- 步 8 给出的控制台启动命令也是 **N 条**（差 `--config`，权重分别指定时还差 `--ckpt`）。

## 阶段 4：把改动固化成一份脚本（步 7 执行）

**开发机上对镜像的一切改动只来自一份幂等脚本 `setup.sh`**，经 heredoc 远端执行；
不在交互 shell 里手敲 `pip install` / `cp`。改了就整份重跑，不在远端手工补。

脚本与服务代码放在本地一个**不入库**的工作目录（即恢复目录 `$W`，见「恢复点」），上传到开发机 `/tmp/setup-<name>/`，
**两端 sha256 比对**后执行。模板与上传、执行、轮询命令见 `references/开发机改造.md`。

脚本必须自带：镜像 ENV 回取（`/proc/1/environ`）、前置断言、**网络只探测不默认配代理**
（只在显式传入 `SETUP_PROXY` 时于本进程内生效，不写任何配置文件；理由见脚本注释）、
运行时资源真建一次 + `test -f` 断言、
`chmod -R a+rX`、结尾写一份 `/opt/ms-<name>/SETUP_INFO`（记 `setup_sha256` / 日期 /
**本次采用的 ray + python 版本及其来源（默认钉死值，或 `RAY_VERSION`/`PY_VERSION` 覆盖值）** / **harness 实际版本与规定基线 `v0.5.0` 的差异**，
复现与回溯时的唯一凭据）。

## 步 7 报告：改造摘要 + 按需详细版

`setup.sh` 打出 `SETUP_OK` 后，**先给摘要表**（默认只给这一份，不要一上来倒出所有路径）：

```text
改造完成（SETUP_OK）
| 项 | 结果 |
| Ray 环境 | <新装|REUSE> ray 2.49.2 / Python 3.13.12，裸 ray 在镜像 PATH 第一位 |
| 服务代码 | /opt/ms-<name>/ 共 <n> 个文件 |
| 模型环境依赖 | websockets/msgpack/pillow <新装|REUSE> |
| 断言 | ray_env_check.sh → RAY_ENV_OK；断网复验 server.py --help 通过 |
| 凭据 | setup_sha256=<前 12 位>（详见 SETUP_INFO） |
| 未改动 | 模型权重、现有推理栈、/root 下既有目录 |
```

**不问人，直接进阶段 5 跑 L1**；摘要末尾一句「要看软件包路径 + 代码改造说明的详细报告说一声」。
用户要了才给两张表——**路径取自 `SETUP_INFO` 与实际 `ls`，不要凭记忆写**：

**A. 软件包安装详细路径**

| 落点 | 内容 | 本次动作 |
|---|---|---|
| `/opt/ray-py` | 独立 venv，仅 `ray[default]==2.49.2` | 新建 / REUSE |
| `/opt/uv-python/cpython-3.13.12-*` | uv 托管的 CPython 3.13.12 | 新装 / REUSE |
| `/opt/uv-bin/uv` | uv 本体 | 新装 / REUSE |
| `/opt/uv-cache` + `/etc/uv/uv.toml` | uv 缓存及其 `cache-dir` 钉定（镜像不带 ENV，只能靠文件） | 新建 / 已存在 |
| `/usr/local/bin/ray`、`/usr/local/bin/uv` | 软链，使裸 `ray` 落在镜像 ENV PATH 第一位 | 新建 |
| `<模型环境>/lib/python*/site-packages` | `websockets` / `msgpack` / `pillow` | 新装 / REUSE |

**B. 代码改造说明**

| 文件 | 作用 | 关键约定 |
|---|---|---|
| `/opt/ms-<name>/server.py` | MS 本体：`GET /config` + WebSocket msgpack | 端口读 `MS_PORT`；权重与 warmup 在监听前完成；ACTION 1-D；按 `session_id` 分桶 |
| `/opt/ms-<name>/serve.sh` | 启动器，控制台命令就填它 | `MODEL_ENV` 绝对路径由 `setup.sh` 写入；端口优先 `MS_PORT`，纯数字首参可覆盖 |
| `/opt/ms-<name>/_base.yaml`、`<benchmark>.yaml` × N | 按 benchmark 选口径的服务配置，`serve.sh --config` 选一份 | `extends: _base.yaml`；写死 `state_key` / `state_dim` / `cameras` / `obs_spec`；**不写 `checkpoint`** |
| `/opt/ms-<name>/obs.<benchmark>.json` × N | 每个 benchmark 的真实观测 spec | 相机键名 + shape + state 维度，自检据此发帧 |
| `/opt/ms-<name>/ray_env_check.sh` | Ray 约束断言（`env -i` 复刻运行时口径） | 打 `RAY_ENV_OK` 才算过 |
| `/opt/ms-<name>/ms_selfcheck.py` | L1 协议级冒烟 | n/n 通过 + 退出码 0 |
| `/opt/ms-<name>/{setup.sh,SETUP_INFO}` | 自拷副本 + 出处凭据 | 同一份脚本可在新开发机原样重跑重建产物 |

两张表都只给**路径与用途**，不打印文件内容，不回显任何 token / AK/SK。

## 阶段 5：离线自检 ＝ L1 协议级冒烟（步 7 门禁）

见 `references/冒烟测试.md`「L1」（前置检查表、两终端口径、就绪信号、判读）与
`references/开发机改造.md`「自检」（起 MS 的完整命令）。两个脚本先跑，再过人工项——**全过才算交付**。

```bash
# 1. Ray 约束（无 GPU 也能跑，秒级）
bash /opt/ms-<name>/ray_env_check.sh             # 要 RAY_ENV_OK + 退出码 0

# 2. 起 MS：运行时用户 + env -i 只带镜像 ENV + 代理指向死端口（命令见 开发机改造.md）
#    每个 benchmark 各起一次：serve.sh --config /opt/ms-<name>/<benchmark>.yaml --ckpt <ckpt>
# 3. 协议级自动冒烟（另一个终端）。解释器必须用模型环境那个，不是裸 python3
"$MODEL_ENV/bin/python" /opt/ms-<name>/ms_selfcheck.py --url ws://127.0.0.1:18000 \
    --obs-spec /opt/ms-<name>/obs.<benchmark>.json   # 与 --config 同一个 benchmark；要「n/n 通过」+ 退出码 0
```

⚠️ 自检入口一律是 **`/opt/ms-<name>/`**（`setup.sh` 的落点）。`/tmp/setup-<name>/` 只是上传中转，
容器重启即消失，拿它当入口会在重启后、以及用户将来用该镜像新建开发机时直接找不到文件。

`ms_selfcheck.py` 自动覆盖：**`obs_spec` 口径门禁**（`--obs-spec` 缺省即 FAIL）、
`/config` 200 与 `?max_batch_size=1`、HELLO 的 `protocol_version`/`seq` 回显、
两个 episode × N 步真观测计时（首帧预算默认 30s）、ACTION 是 1-D 有限值且与 `action_spec` 维度一致、
四种图像编码（`ndarray`/`raw`/`png`/`jpeg`）、畸形帧回 ERROR 且不断连可恢复、两条并发连接的 session 隔离。
`--obs-spec` 给本 benchmark 的**真实**相机键名与形状
（口径与来源见「阶段 3.5：观测对齐」），`--list` 看用例名。

仍需人工确认的：

- [ ] 日志里打印的 ckpt 路径就是传入的那个（脚本看不到服务端日志）
- [ ] 观测字段与上游 reproduction 的预处理逐项对齐（见下文「三个没有报错但分数是错的」）
- [ ] `references/协议自检.md` 里标注「人工」的条目

`/config` 返 200 只是起点——它只证明端口通了，不证明能推理，必须发真观测计时。

L1 结果**按「过程反馈」给一张表**：两道门禁各一行只给判据（`RAY_ENV_OK` / `n/n`＋退出码；多 benchmark 时 `n/n` **每个 benchmark 一行**）、
人工三项各一行只给结论与那一行日志实据，逐条 `[PASS]` 与完整日志都是详情、用户问了再给。
**任一 FAIL** 必须贴失败用例名、关键报错行、以及停在哪一步、不进第 8 步。

## 阶段 6：交付前清理（步 7 收尾）

见 `references/开发机改造.md`「清理」：

- 先**只列不删**（不打印文件内容），清单走 **G5** 确认后再删：
  `AskUserQuestion` 按清单全删（推荐）/ 先看清单逐项 / 剔除某几项（私钥、凭据、代理行、history 不可剔除）/
  不清理，停在步 7（终态写明「此可写层含凭据，勿保存为镜像」）。逐项只对可剔除项摆单项问（只认点选；首项一律「保留」（即使不标推荐也不换成「不确定，先停」），不把删除放第 1 项；`/tmp` 与 `~/.local/bin` 这类安装残留的「保留」不标推荐），全部答完一次执行，中途停下即零删除。「全删」**不含**与本 MS 无关的大目录（默认保留，只在逐项的单项问里可删）与 `/root/.ssh/authorized_keys`：
  它默认保留，在**步 8 高亮块输出之后**附**单独一问**（只认点选）：
  保留（推荐）/ 删除（作为最后一条远端命令，之后本会话不再连机）。
  摆出前先记 `authorized_keys\t留`（恢复时不重问已摆出的这一问，复述时 `假设：` 写「公钥保留（未答）」）；auto 下不摆这一问，直接保留并写进 `假设：`；非 auto 下无效回复与沉默也按保留，不收敛、不重摆；`下一步建议：` 放在这一问之前，选删执行后再给一行。
  **私钥一律删，不读不打印**。**不 `uv cache prune`**（会删预建环境）。
- 不删：`/opt/ms-<name>`、`/opt/<name>-cache`、`/opt/ray-py`、`/opt/uv-*`、`/etc/uv/uv.toml`、模型环境。
  **例外**：回退产生的废产物由差量 G4 批准、G5 只复核（见 `references/改主意回退.md`）。
- **清理后按阶段 5 同一条命令复跑整套 L1**（不只是 `curl /config`）——误删只有发真观测才看得出来。
  仍要 `RAY_ENV_OK` + 「通过数 == 用例总数」+ 退出码 0（不带 `--skip`），这就是本 skill 的交付门禁。
  复跑会再生成 `/tmp/ms-selfcheck.*`，通过后再定点删一次（算 G5 已批准）。复跑 FAIL 后按「出错」表「G4 内可修」重跑了 `setup.sh` 的，
  新残留（`/tmp/setup-*`、history）重列清单再过一次 G5，再复跑 L1。
  私钥/凭据删除与这次复跑都是**硬步骤**：用户催「别删了直接交付」「清理我自己来」「L1 刚才过了不用再跑」
  都不构成跳过：回一句为什么必须做，不顺着略过。

## 步 8：提示用户去控制台制作开发机镜像

**只有阶段 6 清理（私钥/凭据已删）+ 清理后复跑 L1 全绿（`RAY_ENV_OK` + `n/n 通过` + 退出码 0）才进这一步。** 清理没做、清理后没复跑、或 L1 没过，都停在步 7 报告问题，
不要提示用户做镜像。

这段输出必须**高亮**（标题 + 加粗 + 独立代码块），因为它是用户唯一需要亲自动手的环节：

```text
⚠️ 请到百舸控制台「开发机 → 保存为镜像」制作开发机镜像

镜像 tag 必须是全新的一个，不要复用旧 tag（节点有镜像缓存，同名覆盖不生效）。

制作完成后，新建评测任务时按下面填：
  image      : <保存镜像后控制台显示的镜像地址:新 tag>
  command    : bash /opt/ms-dm05/serve.sh --config /opt/ms-dm05/robodojo.yaml \
                 --ckpt <平台挂载后的 ckpt 路径>
  envs       : HF_HOME=/opt/dm05-cache/hf  HF_HUB_OFFLINE=1  HF_MODULES_CACHE=/tmp/hf_modules

端口无需填写：平台从 8000 起全局递增分配，并以 MS_PORT 环境变量注入，serve.sh 自己读。
命令里写死端口（如 --port 8000、host:8000）会被控制面直接拒掉（400）。
envs 黑名单（填了直接 400）：EVAL_*、CUDA_VISIBLE_DEVICES、RAY_*、PYTHONPATH、MS_PORT、代理变量。
```

高亮块之后依次给：本次全程的 `假设：` 汇总（没有就不写）、`下一步建议：做完镜像后先用 1 个 benchmark、
少量 episode 提一次评测核分数量级，再放全量`（三者一并写进 `STEP8`），最后摆 `authorized_keys` 那一问。`STEP8` 与 `step8 written`/`delivered` 的时机见 `references/恢复点.md`。

上面的 `dm05` / `robodojo.yaml` / `/opt/dm05-cache/hf` 只是形状示例。**实际输出必须是可以直接粘贴的
具体命令**，不留任何占位符：`<name>` 换成②的实际值，yaml 与 envs 的路径取自 `SETUP_INFO` 与实际 `ls`。
**唯一允许留占位符的是平台挂载后的 ckpt 路径**（理由见下）。

**命令条数按 benchmark 数给，一条都不许少**：

- 单 benchmark：给 **1 条**，同样显式带 `--config`（与 L1 跑过的那条逐字一致）。
- 多 benchmark（N 个）：给 **N 条**，一条一行，每条**显式带 `--config /opt/ms-<name>/<benchmark>.yaml`**
  ——目录里有多份 yaml 而命令没传 `--config`，`serve.sh` 会显式报错退出（见「多选了 N 个 benchmark」）。
  权重分别指定时 N 条的 `--ckpt` 各不相同，逐条标明哪条对应哪个 benchmark。
- envs 若按 benchmark 不同（如各自的 `HF_HOME`），**随命令逐条给**，不要只给一份让用户自己套。

每条命令**除 `--ckpt` 的值以外，逐字等于**步 7 自检时对应那份 yaml 实际跑过的那条（自检已用
`env -i` + `MS_PORT` 复刻运行时口径），不要在交付时临时改写——多改一个字就等于交付了一条没验证过的命令。

`--ckpt` 是唯一允许不同的字段：自检用开发机上的真实路径，控制台用平台挂载后的路径，而后者由提交评测时的
`ckptPath` 决定、改造阶段还不存在（见阶段 0 输入 ③），本 skill 结构上无法预知，也不要用软链去硬凑成同一字面。
它可以不同的前提是**ckpt 参数无默认值**：填错即启动失败，不会静默跑出一份错分数。
**也因此 `--ckpt` 不能以「有默认值、不填即用 SETUP_INFO 里的路径」交付**——那个默认值指向开发机路径，
平台上根本不存在，真不填就是启动失败；而若脚本里真给了默认值，就退化成「静默加载错权重」。

## 交付之后（不在本 skill 范围，见「交付边界」）

交接时提示两条已知坑即可：

1. **同一 tag 永不复用**——节点有镜像缓存，同名覆盖不生效（换了镜像而行为没变，通常就是这个）。
2. **镜像地址要节点网络可达、凭据允许拉取**——否则以「镜像拉取失败」出现，离根因很远。

提交后的症状速查表在 `references/冒烟测试.md`「Troubleshooting」；与上游基线比对要样本量够才定级。

## 三个「没有报错但分数是错的」

1. **加载的是那个 ckpt 吗？** 日志里打印一次实际路径，E2E 时核对。ckpt 参数**不要给默认值**——
   宁可启动失败，也不要静默加载默认权重跑完全程。
2. **观测对齐了吗？** 相机键名、RGB/BGR、HWC/CHW、归一化、state 维度、图像翻转——见「阶段 3.5：观测对齐」。
   模型内部命名与 benchmark 实发键名是两套命名空间，**不要改模型侧参数去迁就观测键名**；
   开发机上「手工跑通」用的常是作者 demo 的另一套预处理，不等于和 benchmark 发来的观测对齐。
3. **动作语义对吗？** 夹爪符号、delta/absolute、`unnorm_key`、chunk 截断位置、ensemble 策略。

## 交付边界

- 交付止于「**改造完成 + L1 协议级冒烟通过**」：一台自检全绿的开发机 + 每个 benchmark 各一条能填进控制台的完整启动命令。
  **不改平台任何代码**——不写控制面 Go 文件、不动评测集/模型注册表、不动数据面。
- **不做**：保存为镜像、推送仓库、确认集群可拉取、提交评测、平台级冒烟——全部交用户走控制台；
  也**不支持**用 Dockerfile 或 docker 命令构建镜像。把模型做成**控制台下拉里的平台预置模型**
  （要改控制面注册表与数据面参数映射）同样不在范围内。
- **不要直接去集群换镜像或改部署**。
- 改造脚本与服务代码默认**不入库**（本 skill 不产生仓库改动）。用户明确要求入库时才讨论落位与 commit。
