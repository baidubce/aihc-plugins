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
| `references/评测集观测口径.md` | **观测速查表**：8 个常用 benchmark 的相机键名 / shape / state 字段名与维度 / 观测开关，可直接抄成 `obs.<benchmark>.json`。已按 `v0.9.0` 源码逐条复核；**仍是便利品不是权威**，落地前按阶段 3.5 的三个出处核一遍 |
| `references/恢复点.md` | 中断恢复：`form.yaml` / `PROGRESS` 格式（唯一定义处）、重新进入的读序、恢复分支表 |
| `references/改主意回退.md` | 中途改了前面的决定（G4 前 / G4 后两节）：改哪个 → 回退到哪步 → 作废哪些产物 → 重跑哪道门禁 |
| `references/交互守则.md` | **怎么问、怎么报、怎么收口**：一次问齐、推荐项规则、无效回复计数与收敛、auto 边界、阻断/标注、过程反馈、出错三分类、恢复点与改主意入口、越界 |
| `references/观测对齐.md` | **阶段 3.5 全文**：三处可信出处、state 字段名三分、禁止改模型侧参数、自检口径＝真实口径、多 benchmark 一份 yaml |
| `references/交付控制台.md` | **步 8 细则**：高亮块之后给什么、命令条数与逐字一致、`--ckpt` 为何可不同、交付后两条坑 |
| `references/走查样例.md` | **一键走查**：一台假想机从 G1 到步 8 的每停点输出骨架，给首次执行者对顺序与形状的直觉 |
| `references/冒烟测试.md` | **L1 协议级冒烟**（开发机本机，交付门禁）：前置检查表、两终端口径、就绪信号、判读，以及 Troubleshooting 表 |
| `scripts/probe_form_defaults.sh` | **可执行（只读）**：从用户启动脚本 / ckpt / 模型环境推断表单候选值，逐行 `字段<TAB>候选值<TAB>出处`；步 5 预填表单用 |
| `scripts/ray_env_check.sh` | **可执行**：Ray 解释器约束断言（`env -i` 复刻运行时口径）。改造后与交付前各跑一次 |
| `scripts/ms_selfcheck.py` | **可执行**：对已起好的 MS 跑协议级自动冒烟（`/config`、HELLO、`action_spec` 与 benchmark 口径比对、两 episode 真观测计时、四种图像编码、畸形帧容错、session 隔离、`VECTOR_STEP`/`VECTOR_RESULT`；给了 Token（`VLA_EVAL_SERVER_TOKEN`）时另验 Token 格式与 Bearer 鉴权正反用例），逐条 PASS/FAIL，失败退出码 1。只需 `websockets` + `msgpack` + `numpy`（png/jpeg 另需 `pillow`），**不依赖 harness** |

## 规定的 harness 基线：tag `v0.9.0`

需要上游 harness 的服务写法（`add-model-server` 文档、`PredictModelServer` 的 chunk / batch 语义）或
benchmark 的观测口径时，**一律对齐公开仓库 `github.com/allenai/vla-evaluation-harness` 的 tag `v0.9.0`**
（commit `8798464`，2026-11-03）。本文与 `references/评测集观测口径.md` 的全部上游引用、行号都核对自这个 tag。

```bash
git show v0.9.0:src/vla_eval/benchmarks/<name>/benchmark.py | less   # 规定基线，可直接 checkout
```

- **`v0.9.0` 是可 checkout 的真 git tag**；镜像 tag 里的 `h090`（如 `h090-261106`）只是「对齐 harness
  `v0.9.0`」的书写约定，**它本身不是 ref**，要 checkout 请写 `v0.9.0`。
- **不要顺手读 `main` 或其他分支。** 它们都已在 `v0.9.0` 之后前移，
  键名与行号会再变，照它写 MS 会得到一份不报错的错分数。
- **相对 `v0.5.0` 的破坏性变化**（旧镜像 `h050-*` 的 MS 不能照搬）：`PredictModelServer` 改成
  `predict(obs, row: Row)` + `reset_episodes` / `finish_episodes`，`SessionContext`、`action_ensemble`、
  `ema_alpha` 已删除；协议新增 `EPISODE_READY` / `VECTOR_STEP` / `PUBLISH` / `EPISODE_ERROR` 等由 HELLO
  `capabilities` 协商的消息（`protocol_version` 仍是 `1`）；robotwin 新增 `send_state` → `states` 16D、
  `max_steps` 400→1700；benchmark 收不下的 `observation_params` 从静默丢弃改为 warning；`rlbench` 删除、新增 `ocbench`。
- **`capabilities` 不是纯可选项**：`robodojo` 与 `ocbench` 是 `VectorStepBenchmark`，MS 必须声明
  `capabilities: {"vector_step": true}` 并实现 `VECTOR_STEP`/`VECTOR_RESULT`，否则 `_run_vector` 直接
  `RuntimeError`，而 benchmark 的 `hard_exit` 会把它吞成 `exit code 1` + 0 episode、**没有任何 traceback**。
  报文与语义见 `references/脚本模板.md`「形态 B 必做：VECTOR_STEP」，自检见
  `scripts/ms_selfcheck.py` 的 `vector_step` 用例（未声明即 FAIL）。
- **确认开发机上装的是哪一版**：
  ```bash
  "$MODEL_ENV/bin/python" -c "import vla_eval,vla_eval._version as v; print(v.version, vla_eval.__file__)"
  ```
  不是 `v0.9.0` 时**不要默认跟随已装版本**：以 `v0.9.0` 为准核键名，并把实际版本与差异显式写进 `SETUP_INFO`
  （协议细节与观测键名在版本间会变，差一版就可能差一个键名）。


## 平台对镜像的两条硬约束

自定义镜像**不改变运行形态**：仍是同一个 RayJob 里的 `WorkerMSCustom` worker group 跑 `ModelServerActor`
（`num_gpus=1`），控制面的自定义镜像分支只换 `Image` + `ImageConfig`。所以：

1. **镜像必须能组 Ray 集群**：`ray[default]==2.49.2` + Python `3.13.12`，且**裸 `ray` 在镜像 PATH 上**
   （KubeRay 跑 `ray start` / `ray health-check`）。模型环境**不受**此约束，另起一套即可。
   见 `references/Ray环境.md`。
2. **服务必须满足协议**：同端口上 `GET /config` 返 200 + WebSocket msgpack。见 `references/协议自检.md`。

⚠️ 第 1 条**平台代码里没有任何校验**（声明式的 Ray/Python 版本常量没有读取方）：不满足时 coordinator 按 hostname
子串 `-workermscustom-worker-` 找 MS 节点，等满 600s 报「MS 节点不足」——离根因极远，所以必须在改造阶段断言掉。
平台固定 Ray head 口径为 `ray[default]==2.49.2` + Python `3.13.12`，必须 **patch 级一致**（差一个小版本就连不上 GCS）；
本 skill 钉死这两个值，不问用户、不靠开发机现有 ray 反推；要改时用 `RAY_VERSION=<x.y.z> PY_VERSION=<a.b.c>`
覆盖，并把本次版本记进 `SETUP_INFO`。

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

**机器门禁**（`RAY_ENV_OK`、两次 L1 `n/n`+退出码 0）只看判据，不过按 `references/交互守则.md`「出错」处置，**不问「要不要继续」**。

两条不可让渡的次序：**G2 之前零远端执行**（只读也不行）；**L1 没过不进步 8**。

### 怎么问（细则见 `references/交互守则.md`）

三条不可让渡的机制，其余（推荐项规则、无效回复计数与收敛、auto 边界、恢复、改主意、越界）全在
`references/交互守则.md`：

1. **一次问齐**：一个停点的全部缺项装进同一次 `AskUserQuestion`（≤4 问，超过分轮）；自由文本缺项用一条消息列齐、附示例，一行答完。
2. **首项推荐、不编推荐**：每问第 1 项是推荐并标「（推荐）」，推荐值只能来自本次会话用户消息 > 同机记忆 > probe 结果 > 查表；
   **都没有就不标推荐**，该问第 1 项固定「不确定，先停」。
3. **终态必收口**：每个终态最后一行是 `下一步建议：…`，G4 之后、步 8 之前的终态再加一句
   `⚠️ 此可写层未完成清理与复检，勿保存为镜像`。

门禁只在这几处停：G1 缺项、G2 授权、G3 无默认值行、G4 计划、G5 删除（含 `authorized_keys`）、5d 硬失败。

阻断 vs 标注：**错了会静默错 → 阻断；错了会大声报错、或只影响呈现 → 标注（照默认做，结果里写明）**，拿不准当阻断。
用户**显式要求偏离训练口径**时按用户要求做，但把「偏离项 + 用户原话依据 + 可能后果」写进 `假设：` 与 `SETUP_INFO`。

过程反馈（一个逻辑阶段一行 `✓ … → 接下来 …`、默认 ≤500 字符、数字不省）、出错三分类与标准报错格式、
恢复点、改主意、越界：见 `references/交互守则.md`。

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

**每一项都在后面被当成唯一真相源用**（①–⑥ 逐项「用在哪 / 缺了会怎样」见 `references/接入表单.md`「为什么这六项必须收齐」）。
到阶段 3/5 才发现缺会返工，最典型的是④ 的观测口径与⑥C 的训练口径：前者采信用户给的相机键名会让自检自洽通过、
上平台全 0 分，后者缺了就跑满 max_steps、0 分。

### 步 2：TCP 探测 + 授权（G2）

本地 `nc -z` 探 TCP 端口（不登录、不碰私钥，可直接跑）→ 把**拼好的 SSH 命令（私钥只显示路径）
+ TCP 结果 + 用户名等标注**一起回显，在同一道 G2 里取授权（授权执行 / 只生成命令 / 重输 / 取消）→ 授权后才
`echo connected` 验登录。TCP 不通就不摆 G2，按 `references/交互守则.md`「出错」格式报，让用户查开发机状态/安全组。
`dry_run` 下一条都不执行：输出步 3 体检与 5b probe 的完整命令，
请用户自己跑完把输出贴回，再据此预填 G3；明说不贴回或两次无效（按 `references/交互守则.md`「怎么问」计）即以 `dry_run` 终态结束并写 `stop\t2\tdry_run`。G3 之后的 `dry_run` 输出改造计划与 `setup.sh` 全文即终态，不进 G4。

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
3. **L3 缺项**（`state_layout` / `action_layout` / `action_denormalize_by` / `runtime_downloads` 的显式结论，
   以及 predict 片段缺失或跑不通时的 `action_dim` / `chunk_len` / `camera_slot_map`）：有闭集的
   （`action_denormalize_by`、`runtime_downloads` 的 `none`）做成选项、**不标推荐**、第 1 项「不确定，先停」；
   开放的（`state_layout` / `action_layout`）列在同一条消息里给格式示例，一行答完。
   `action_format` 不进 L3：它逐字取 benchmark `get_action_spec()` 的 `format` 串，只有 benchmark 查不出时才向用户要语义串；
   模型语义与 benchmark 期望冲突（delta vs absolute、夹爪正负）时**不换串**，按「口径冲突」停下问用户。
   有构造参数的 benchmark（robotwin `action_type` / calvin `absolute_action`）另收一行 `action_params`：
   这是**提交评测时实际用的值**，不与 MS 声明成对就会静默错（16D 截断成 14）。
4. **仍 `未提供` 的 L1 行与 ≥2 个 benchmark 时的 `ckpt_mode`**：`ckpt_mode` 做成选项（不确定，先停 / reuse / per_benchmark，不标推荐）；
   `name` / `model_env` / `sub_bench` 有候选的做选项；predict 点名的候选**不标推荐**，第 1 项固定「不确定，先停」；以上无候选的并进第 3 问那条文本，一行答完。
   选项题超过 4 问时按需分轮（每轮 ≤4 问；多 benchmark 的 ⑥B 按 benchmark 分组），文本题始终只一条。

L3 行与 probe ckpt **无默认值、不许猜、不许用看起来合理的值糊过去**。
每答一行即回写 `$W/form.yaml`，确认后置 `g3_confirmed: true`，`PROGRESS` 追加一行。

**L3 要有终态**：用户累计两次答不出同一行（按行计，不跨会话，催促与「嗯 / ok / 不知道」都算）时，收敛成一问：
导出待补清单并停（推荐）/ 补填这几行 / 取消；第二次收敛不给「补填」。
「导出」输出待补清单——哪几行仍 `未提供`、为何不能默认、从哪能查到（`state_layout` 问模型作者或训练脚本里
state 的打包顺序，`action_layout` 看训练配置里动作向量的段序（段名可含 joint / ee / base_vel / torso 等，不限 joint|ee 二选一），`action_denormalize_by` 看 predict 里有无反归一化与 `unnorm_key`，
`runtime_downloads` 看用户启动日志里的下载 URL 与 HF cache，predict 点名与 probe ckpt 问用户实际跑通的函数与权重，`action_dim`/`chunk_len`/`camera_slot_map` 看 predict 的返回形状与图像索引）——并写明「改造尚未开始、开发机
零写入，补齐后回 G3」，清单同步写进 `form.yaml`。收敛后再无效按「导出」处理，给 `下一步建议：`，不再摆。

`sample_action` 是**唯一**可降为标注的 L3 项（只供人工核对量级、不改分数）：缺失就进 `假设：` 块写
「未提供样例动作，人工量级核对跳过」，不要为它停。

### 5d. 改造前置检查

G3 通过后的**第二道只读检查**（不问人），与步 3 分工不同：步 3 答「这台机器是什么样」，
5d 答「按计划改造会不会失败、会不会覆盖已有东西」。结论全部带进 G4 的计划，**不为单项另开一问**；
标「硬失败」的按 `references/交互守则.md`「出错」格式立刻停，`需要你：` 固定三项、不标推荐：不确定，先停 / 重探（已处理）/ 取消；
不给「合并进计划先跑」。重探至多 2 次仍硬失败就以「停在当前步」结束，不再给「重探」。

检查项与「不通过怎么办」的完整表见 `references/开发机改造.md`「改造前置检查（步 5d）」。两条**硬失败**只有：
`/etc/uv/uv.toml` 已存在但没把 `cache-dir` 钉成 `/opt/uv-cache`；PATH 上第一个 `ray` 不满足约束（软链盖不掉）。

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

- **先探测可复用，再决定装**：「要不要联网」是探测的结论、不是前提。PATH 上第一个 `ray`、conda / 既有 venv /
  `/opt/ray-py` 里的 ray 已是 `ray[default]==2.49.2` + Python `3.13.12`，或机器上已有精确 `3.13.12`——任一命中即
  零下载、不需要外网（脚本打 `RAY_REUSE:` / `PY_REUSE:`），直接收口于断言。
- **PATH 上有没有 `ray`，走的是两条相反的路**：没有 → 软链进镜像 ENV `PATH` 上**已有**的根盘目录（通常
  `/usr/local/bin`）；第一个 `ray` 存在但**不满足**约束 → **硬失败**（KubeRay 只认第一个，软链盖不掉）。

## 阶段 3：写 model server（步 7 执行）

服务骨架、`predict`、specs、chunk、batch 的写法见 `references/脚本模板.md`，
逐条自检见 `references/协议自检.md`。最容易踩的七条：

1. **端口不能写死**，进程里读平台注入的 `MS_PORT`——规则与 envs 黑名单见「步 8」。
2. **探活是 `GET /config` 不是 `/health`**，与 WebSocket 同端口（平台 2s 探一次、900s 上限）；
   权重加载 + warmup 必须在开始监听**之前**做完（`/config` 200 后平台立刻发首帧，默认只等 30s）。
3. **ACTION 永远是 1-D 单步** `(action_dim,)`；chunk 只在服务内缓存逐帧 pop。
4. **per-episode 状态不能用进程级单变量**（多 shard 共享一个 MS）：形态 A 按 `row.id` 分桶、在
   `reset_episodes` 里清空；形态 B 按连接（`session_id`）分桶、`EPISODE_START` 清空；
   推理异常回 `ERROR` 而**不是**零动作、不关连接。
5. **`get_action_spec()` 的 `format` 逐字等于 benchmark 的 `get_action_spec()`**：`DimSpec.format`
   是自由字符串（不是 ee/joint 二选一，段名与段数都由 benchmark 定），harness 的 `check_specs()`
   对不上**只打 warning、评测照跑** ⇒ 分数静默错。`ms_selfcheck.py` 的 `action_spec` 用例把同一份
   比对做成硬门禁，交付前必须过；写法与候选表见 `脚本模板.md`「action / observation spec 怎么写」。
   **维度也不由 ee/joint 决定**：robotwin 是 joint 14D / ee 16D，但 libero/simpler/calvin 末端 7D、
   robocasa365 13D 五段、behavior_1k 关节 23D——一律抄 `get_action_spec()`，不套记忆里的数。
6. **MS 声明的口径要与 BA 侧构造参数成对**：robotwin 声明 `ee_pose` 16D 时，提交评测的 sub_bench
   必须传 `action_type=ee`，否则 benchmark 走默认 `qpos`、把 16D **静默截断成 14** 跑完全程
   （`robotwin/benchmark.py:521-524`）。自检加 `--bench-param action_type=ee`
   （calvin 是 `absolute_action=true|false`）才把这条钉住，不传只打提示。
7. **⑥C 的训练口径要真的接线，并在启动日志里回显**：`action_mode`（及其在推理侧的消费方向）、
   `is_history` + 历史槽位数 / 采样率 / 取自哪路相机，逐项打进日志一行（`use_absolute_action=...`、
   `is_history=...`、`max_history_images=...`）。口径写在文档里、没落到 argv 和日志里，等于没做——
   出分时唯一能回溯的就是这一行。历史帧采样的具体实现见 `references/脚本模板.md`「训练口径接线（⑥C）与历史帧」。

raw 路径**不注入** `HF_HOME` / `HF_HUB_OFFLINE` / `HF_MODULES_CACHE`，要就自己在控制台 envs 里给
（`HF_MODULES_CACHE` 必须可写）。

## 阶段 3.5：观测对齐（**最容易静默错分数的一步**）

全文见 `references/观测对齐.md`（三处可信出处、state 字段名三分、禁止改模型侧参数、自检口径＝真实口径、
多 benchmark 一份 yaml）。不能压缩的四条：

- **两套命名空间**：模型侧相机名（常逐字进模型文本 prompt）与 benchmark 实发观测键名互相独立。真实键名只认
  `v0.9.0` 的参考 MS `_CAMERA_MAP` > benchmark 实现 > `docs/reproductions/`，**不认开发机上手工跑通那套**
  （那常是作者 demo 的另一套预处理）。
- **禁止改模型侧参数去迁就键名**：改了不报错、只出一份错分数（prompt 从 `Head image: ` 变成 `cam_head image: `）。
  正确做法是解耦双职责 + 别名表，缺则显式报错。
- **不补零、不嗅探**：`state`/`states`/`joint_state` 三个都缺就抛错；一 benchmark 一份 yaml，缺键显式报错。
- **自检口径必须等于真实口径**：`obs.<benchmark>.json` 写模型内部命名会让 L1 自洽通过而上平台每 episode ERROR；
  `ms_selfcheck.py` 的 `--obs-spec` 缺省即 FAIL，**交付门禁不许带任何 `--skip`**（缺 `pillow` 时 png/jpeg 自动降 INFO 除外）。
## 阶段 4：把改动固化成一份脚本（步 7 执行）

**开发机上对镜像的一切改动只来自一份幂等脚本 `setup.sh`**，经 heredoc 远端执行；
不在交互 shell 里手敲 `pip install` / `cp`。改了就整份重跑，不在远端手工补。

脚本与服务代码放在本地一个**不入库**的工作目录（即恢复目录 `$W`，见 `references/恢复点.md`），上传到开发机 `/tmp/setup-<name>/`，
**两端 sha256 比对**后执行。模板与上传、执行、轮询命令见 `references/开发机改造.md`。

脚本必须自带：镜像 ENV 回取（`/proc/1/environ`）、前置断言、**网络只探测不默认配代理**（只在显式传
`SETUP_PROXY` 时于本进程内生效，不写任何配置文件）、运行时资源真建一次 + `test -f` 断言、`chmod -R a+rX`、
结尾写 `/opt/ms-<name>/SETUP_INFO`（`setup_sha256` / 日期 / 本次 ray+python 版本及其来源 / harness 实际版本与
`v0.9.0` 的差异）——复现与回溯时的唯一凭据。

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

详细版两张表（软件包安装路径 / 代码改造说明）见 `references/开发机改造.md`「步 7 详细报告」——
**路径取自 `SETUP_INFO` 与实际 `ls`，不凭记忆写；只给路径与用途，不打印文件内容，不回显任何 token / AK/SK。**

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
    --obs-spec /opt/ms-<name>/obs.<benchmark>.json --benchmark <benchmark>   # 与 --config 同一个 benchmark；要「n/n 通过」+ 退出码 0
# 4. 仅当 MS 还要以「自建推理服务」接入、开了 Bearer 鉴权时：MS 与自检读同一个环境变量再跑一遍
#    export VLA_EVAL_SERVER_TOKEN=<token>，重起 MS 后在第 3 步命令末尾加 --require-auth
```

⚠️ 自检入口一律是 **`/opt/ms-<name>/`**（`setup.sh` 的落点）。`/tmp/setup-<name>/` 只是上传中转，
容器重启即消失，拿它当入口会在重启后、以及用户将来用该镜像新建开发机时直接找不到文件。

`ms_selfcheck.py` 自动覆盖（完整清单见 `references/协议自检.md`「哪些条目已被脚本自动判定」，`--list` 看用例名）：
**`obs_spec` 口径门禁**（`--obs-spec` 缺省即 FAIL）、`/config` 200、HELLO 回显、**`action_spec` 动作口径门禁**
（与 benchmark `get_action_spec()` 的 `format`/`dims` 逐项比对，`--bench-param action_type=ee` / `absolute_action=true`
收窄候选）、两 episode × N 步真观测计时、ACTION 1-D 且维度一致、四种图像编码、畸形帧容错、session 隔离、
`VECTOR_STEP`/`VECTOR_RESULT`；给了 Token 另验格式与鉴权正反用例（输出一律打 `Bearer ***`）。
`--obs-spec` 必须给本 benchmark 的**真实**相机键名与形状（见「阶段 3.5：观测对齐」）。

仍需人工确认的：

- [ ] 日志里打印的 ckpt 路径就是传入的那个（脚本看不到服务端日志）
- [ ] 观测字段与上游 reproduction 的预处理逐项对齐（见下文「四个没有报错但分数是错的」）
- [ ] `references/协议自检.md` 里标注「人工」的条目

`/config` 返 200 只是起点——它只证明端口通了，不证明能推理，必须发真观测计时。

L1 结果**按 `references/交互守则.md`「过程反馈」给一张表**：两道门禁各一行只给判据（`RAY_ENV_OK` / `n/n`＋退出码；多 benchmark 时 `n/n` **每个 benchmark 一行**）、
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
  复跑会再生成 `/tmp/ms-selfcheck.*`，通过后再定点删一次（算 G5 已批准）。复跑 FAIL 后按 `references/交互守则.md`「出错」表「G4 内可修」重跑了 `setup.sh` 的，
  新残留（`/tmp/setup-*`、history）重列清单再过一次 G5，再复跑 L1。
  私钥/凭据删除与这次复跑都是**硬步骤**：用户催「别删了直接交付」「清理我自己来」「L1 刚才过了不用再跑」
  都不构成跳过：回一句为什么必须做，不顺着略过。

## 步 8：提示用户去控制台制作开发机镜像

**只有阶段 6 清理（私钥/凭据已删）+ 清理后复跑 L1 全绿（`RAY_ENV_OK` + `n/n 通过` + 退出码 0）才进这一步。**
清理没做、清理后没复跑、或 L1 没过，都停在步 7 报告问题，不要提示用户做镜像。

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

三条硬要求：**除平台挂载后的 `--ckpt` 路径外不许留占位符**、**命令条数按 benchmark 数给**（单 1 条；N 条各带
`--config /opt/ms-<name>/<benchmark>.yaml`）、**每条除 `--ckpt` 外逐字等于步 7 自检跑过的那条**（多改一个字
就等于交付了一条没验证过的命令）。高亮块之后依次给：`假设：` 汇总 → `下一步建议：…` → `authorized_keys` 那一问
（保留（推荐）/ 删除）。写法细节、`--ckpt` 为何可不同、交付后要提示的两条坑（tag 不复用、镜像需节点可达）见
`references/交付控制台.md`。
## 四个「没有报错但分数是错的」

1. **加载的是那个 ckpt 吗？** 日志里打印一次实际路径，E2E 时核对。ckpt 参数**不要给默认值**——宁可启动失败。
2. **观测对齐了吗？** 相机键名、RGB/BGR、HWC/CHW、归一化、state 维度、图像翻转；模型内部命名与 benchmark 实发键名
   是两套命名空间，**不要改模型侧参数去迁就键名**（见「阶段 3.5」与 `观测对齐.md`）。
3. **动作语义对吗？** 夹爪符号、delta/absolute、`unnorm_key`、chunk 截断位置、ensemble 策略。
4. **推理框架的默认值 = 训练时的值吗？**（前三条的「上游版」）推理入口的 dataclass / argparse 默认值常是作者调试值，
   与产出该 ckpt 的训练脚本不一致、**语义还可能取反**；必须回训练入口取证（⑥C），不许照推理 serve 脚本抄。
   实测两例（dm05 / RoboDojo，2026-11-17）：`action_mode` 训练 `ABSOLUTE` 而推理默认 `RELATIVE`，框架里
   `use_absolute_action = (action_mode == RELATIVE)` ⇒ 管线再叠一次 `abs = state + action`，形状对、无报错、
   `mean_success=0`；`is_history` 训练 `True` + 20 个 1 FPS 历史槽位（取自 Head），推理默认不传 ⇒ prompt 少 20 路图。

## 交付边界

- 交付止于「**改造完成 + L1 协议级冒烟通过**」：一台自检全绿的开发机 + 每个 benchmark 各一条能填进控制台的完整启动命令。
  **不改平台任何代码**——不写控制面 Go 文件、不动评测集/模型注册表、不动数据面。
- **不做**：保存为镜像、推送仓库、确认集群可拉取、提交评测、平台级冒烟——全部交用户走控制台；
  也**不支持**用 Dockerfile 或 docker 命令构建镜像。把模型做成**控制台下拉里的平台预置模型**
  （要改控制面注册表与数据面参数映射）同样不在范围内。
- **不要直接去集群换镜像或改部署**。
- 改造脚本与服务代码默认**不入库**（本 skill 不产生仓库改动）。用户明确要求入库时才讨论落位与 commit。
