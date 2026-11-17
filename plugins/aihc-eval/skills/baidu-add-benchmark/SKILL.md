---
name: baidu-add-benchmark
description: "[runbook] 把一台能 SSH 的百舸开发机上手工能跑的评测代码，改造成具身评测可用的自定义 benchmark（BA）镜像内容：装 vla-eval v0.9.0、写 StepBenchmark 适配器、单条目 eval.yaml、单 shard 的 start.sh，经本机冒烟与 ba_selfcheck 自检全过后，交付控制台表单（name、command、envs、建议资源）。全程不改平台代码。触发：『把某个评测集做成自定义 benchmark』『自定义 BA 镜像接入』『benchmark 镜像冒烟 / 自检不过』『继续接 benchmark』。不适用：保存镜像与推送仓库、提交评测任务、做成平台预置评测集、改控制面或数据面、改造模型服务（用 baidu-add-model-server）。"
---

# 开发机改造成自定义 Benchmark

## 定位

**起点**：一台能 SSH 的百舸开发机，原评测代码能手工跑。
**终点**：开发机通过本机冒烟（`scripts/smoke_run.py`）与自检（`scripts/ba_selfcheck.py`），交付镜像建议名、
command、envs、建议资源，供用户在控制台「保存为镜像」后以自定义 benchmark 提交评测。

**前置条件**（skill 不代办）：已有百舸开发机且本机能 SSH；账号已开通具身评测；开发机有 NVIDIA 卡（0 卡评测另走 cpu 渲染路径）。

## 本 skill 的构成

| 文件 | 内容 |
|---|---|
| 本文 | 交互契约（八步 + G1–G5）、三条底线、单 shard 规则、交付边界 |
| `references/接入表单.md` | `form.yaml` 字段：name、command、envs、镜像内 config 路径、资产方式，及各字段出处 |
| `references/命令契约.md` | `EVAL_BA_*` 全表、`start.sh` / `eval.yaml` 模板、CLI 覆盖行为、单 shard 隔离要求 |
| `references/冒烟测试.md` | echo MS + `smoke_run.py` + `ba_selfcheck.py` 的跑法、检查项与 M 编号、`--control` 对照、失败排查表 |
| `scripts/smoke_run.py` | 按平台口径注入 `EVAL_BA_*`，同机并发起 N 个 shard 原样执行交付 command |
| `scripts/ba_selfcheck.py` | 在冒烟产物上跑 M2、M4～M12（M3 只查版本），`--control` 跑零动作 / oracle 对照（M12），输出与平台 `validation` 同构的 `checks[]`，附 `harness_config` |
| `scripts/shard_peak.py` | 冒烟期间采单 shard 内存、CPU、shm、显存峰值，换算 N 个 shard 的建议资源（内存按 Pod 计），会被控制面拒绝的取值出 `NOTE:` |
| `scripts/ba_checks.py` | 平台运行后核对模块的官方副本，**禁止手改**，以本 skill 附带的这份为准 |
| `references/连接与授权.md` | 步 1：探端口 → G1 → 验登录，G1 授权的范围与写落点，远端执行纪律 |
| `references/恢复点.md` | `PROGRESS` 行格式、`form.yaml` 出处标签、中断后怎么续 |
| `references/改主意回退.md` | 改了哪个决定回到哪一步、作废什么、重过哪道门禁 |
| `scripts/probe_form_defaults.sh` | 步 2～4 的只读探测：评测 env、eval yaml、适配器、仿真器、分级提示、图形栈、Ray、磁盘 |
| `scripts/ray_env_check.sh` | 断言镜像 PATH 上第一个 ray 是 base 的 2.49.2 / Python 3.13.12（只读） |
| `scripts/echo_ms.py` | 冒烟用的零动作模型服务，复用上游 `vla-eval test` 的 echo 实现（`smoke._make_echo_server` + `serve_async`），不依赖 docker |
| `references/Ray环境.md` | 镜像不基于 base 时补装 Ray 解释器（`/opt/ray-py` + 软链），失败形态与收尾 |
| `references/适配器指南.md` | B 档：照原评测代码写 `StepBenchmark` 适配器，六项对齐与 success 出处、观测图像方向 |
| `references/渲染与资产.md` | `render_backends` / `configure_render`、各仿真器 cpu 路径、ICD 钉法、资产落点（MuJoCo EGL 已验证，其余未验证） |
| `references/上游缺口.md` | v0.9.0 已知缺口、平台过渡做法、C? 档注意事项、集群单 Pod 复测（起法、过的标准、卡住时怎么取证）、issue / PR 草稿纪律（C? 节与单 Pod 复测已由 Isaac Sim 样例验证） |
| `references/交付说明模板.md` | 步 8：交付说明的固定章节、写法与待填项 |

已验证的有三档：**A 档**（评测集已是 harness benchmark，只缺平台口径的 `start.sh` / `eval.yaml`）；B 档的 MetaWorld MT1 一例
（MuJoCo EGL，开发机冒烟与自检 PASS，平台 oracle 全部成功）；C? 档的 Isaac Sim 5.1 一例（平台普通创建接口正式评测通过）。
`渲染与资产.md` 除 MuJoCo EGL 外、`上游缺口.md` 除 C? 节与单 Pod 复测外仍是**未验证**。B 档按 `适配器指南.md` 写适配器；C? 档步 3 先停下，
把 `上游缺口.md` 的 C? 节讲给用户，用户确认继续才往下走。

## harness 基线：vla-eval v0.9.0

引用上游代码与行号一律对齐 tag `v0.9.0`，不读 `main`。harness 0.9.0 要求 Python ≥ 3.11，评测 env 低于 3.11 时
要另建 env。h090 起 base 镜像（`aihc-eval_base:0.4.0`）不再带 harness，平台 BA 镜像自带该版本 harness，放在
`/root/workspace/vla-evaluation-harness`，评测 env 以 `pip install --no-deps -e` 装它（带 `auth_token`）。开发机上先确认版本：

```bash
"$BENCH_ENV/bin/python" -c "import vla_eval, importlib.metadata as m; print(m.version('vla-eval'), vla_eval.__file__)"
```

镜像不基于 `aihc-eval_base`（PATH 上第一个 ray 不是 `/opt/conda/bin/ray`）时，按 `references/Ray环境.md`
补装 Ray 解释器，再以 `EXPECT_RAY=/usr/local/bin/ray` 重跑 `ray_env_check.sh`。

## 三条底线（任何一条不满足都不交付）

1. **success 有出处**：success 判定追溯到原评测集代码或论文，交付说明写明文件与行号。
2. **渲染在 NVIDIA 设备**：画面非全黑，Vulkan / EGL 不落到 lavapipe、llvmpipe。自检 M10 先查，结论由用户确认后写进交付说明。
3. **动作语义一致**：动作维度、坐标系、增量 / 绝对、gripper 取值与目标模型一致，写进交付说明。

## 单 shard 规则

交付的 command 是**单个 shard** 的启动命令，平台在同一 Pod 内并发执行 N 次，N 个进程共用 `EVAL_BA_OUTPUT_DIR` 下的
recording DB 与 `work_queue`。所以：

- `start.sh` 只按 `EVAL_BA_*` 跑本 shard，**不循环、不并发起多个 `vla-eval run`**，最后一行用 `exec`；
- 不监听固定端口（绑 0）；自有临时文件、缓存、锁按 `EVAL_BA_SHARD_ID` 分目录或 `mktemp`；
- 不改写 `EVAL_BA_OUTPUT_DIR`、`EVAL_BA_EVAL_ID`；不在 `start.sh` 里 `setsid`；
- 平台用 `shlex.split` 执行 command、不经 shell：command 只写 `bash /opt/bench/start.sh`，变量与重定向全写进 `start.sh`。

## 交互契约：八步与门禁

工作目录记作 `$W`（开发机上，如 `/root/workspace/ba-<name>`）。每步结束往开发机 `$W/PROGRESS` 追加一行，
表单存同目录的 `form.yaml`，格式与中断后怎么续见 `references/恢复点.md`；改了已确认的决定按 `references/改主意回退.md` 回退。

| 步 | 做什么 | 停点 / 门禁 |
|---|---|---|
| 1 连接与授权 | 拿到 SSH 命令，验证能登录，见 `references/连接与授权.md` | **G1**：未授权不执行写命令；授权范围见 `references/连接与授权.md` §3 |
| 2 只读探测 | `<SSH_CMD> -o BatchMode=yes 'bash -s' < scripts/probe_form_defaults.sh` 与 `scripts/ray_env_check.sh`（输出 `tee` 到 `$W/ray_env_check.log`，供自检 M3）；用户已指定评测 env 时显式传 `BENCH_ENV=`，猜错 yaml 时用 `EVAL_YAML=` 重探；另问用户三件事：原评测入口、资产路径、要接哪个 suite（name 在步 4 定） | `ray_env_check.sh` 须 `RAY_ENV_OK`；评测 env 须 `auth_token=有` |
| 3 分级 | 读探测的 `grade_hint`：A 有 eval yaml 且能定位到适配器类；A? 有适配器或上游同名适配器但缺 yaml，核对后按 A 补 yaml；B 有仿真无适配器；C? Isaac/Omniverse 系 | 非 A / A?：B 档告诉用户要照原评测写适配器（`references/适配器指南.md`）并征得同意；C? 档讲清 `上游缺口.md` C? 节的风险，用户确认才继续 |
| 4 预填表单 | 用探测的 `yaml_*` 行作候选，填 name、`eval.yaml` 的 `benchmark` / `params` / `max_steps`、envs、任务列表，写进 `form.yaml`；`yaml_platform_keys` 列出的键在 `eval.yaml` 里删掉 | **G2**：任务列表、seed、horizon（`max_steps`）、success 定义四项各附出处（文件:行 或论文节） |
| 5 计划 | 列出要新增 / 修改的文件与命令，预估冒烟时长 | **G3**：用户确认计划后才进入步 6 |
| 6 改造 | 装 vla-eval、（B 档）写适配器、写 `/opt/bench/eval.yaml` 与 `/opt/bench/start.sh`（照 `references/命令契约.md` 模板） | 只写 `$W`、`/opt/bench` 与计划里列出的 conda env；不动 base 的 Ray 解释器，非 base 镜像补装 Ray 的落点也须在 G3 计划里 |
| 7 冒烟与自检 | 起 echo MS → `smoke_run.py`（运行期间记录各 shard 的 `.progress` 到 `progress_seen.json`）→ `ba_selfcheck.py --control`，再用 `--set EVAL_BA_MODE=live` 换新目录跑一次实时模式冒烟与自检，见 `references/冒烟测试.md`；每次冒烟用新的 `--workdir`；C? 档或按核数起线程的仿真器，开发机过了再在集群上起单 Pod 按 BA 配额复测（`references/上游缺口.md` 第 5 节，起 Pod 前征得用户同意） | **G4**：任一 shard 非 0 退出或任一自检项 FAIL 都回到步 6，**没有 `--skip`**；正式评测失败而开发机不复现时，先做单 Pod 复测，不要改一版就让用户重存镜像 |
| 8 交付 | 输出交付说明（见下），再按「收尾清理」删掉开发机上的残留 | **G5**：交付的 command 与 `smoke_run.py --command` 实际执行的逐字一致；清理后评测 env 仍能 import `vla_eval` |

改了前面已确认的决定（换 suite、换 `max_steps`、换 conda env），回到对应步重新过该步门禁，之后的冒烟结果作废。

## 交付说明

照 `references/交付说明模板.md` 写，存到 `$W/交付说明.md` 并在对话里给用户，收尾时随 `$W` 归档到本地，不进代码仓库。至少包含：

- 镜像建议名：`aihc-eval_ba_custom_<name>:h090-<北京日期 YYMMDD>`（由用户在控制台保存镜像时填）；
- 控制台条目：`name`、`command`（`bash /opt/bench/start.sh`）、`envs`（不含 `EVAL_BA_*`，平台下发的同名键会覆盖用户值）；
- 建议资源：取 `shard_peak.py` 的 `suggest`：CPU ≥ 2×N，shm ≥ N × 单 shard 峰值，每卡显存 ≥ N / G × 单 shard 峰值；
  内存按 Pod 计，含 Ray 常驻、每 shard 一个 Actor、shm 与页缓存，并留 1.5 倍余量；`peak.log` 的 `NOTE:` 行原样写进交付说明
  （单 shard CPU 峰值超 2 核、shm 低于 10 GiB 时控制台留空、G ≥ 2 时 N 须整除 G、读不到 cgroup 时内存可能偏小）；
- `ba_selfcheck.py --out` 的 JSON 里的 `harness_config`（已脱敏），供用户与正式评测的 `harness_config` 对照；
- 三条底线的结论与出处；`--control` 对照结论（没有 oracle 写「未验证」）；
- 对环境的改动：保留了哪些路径（`/opt/bench` 与评测 env），收尾清理删了哪些，没有就写「无」。

## 收尾清理

用户保存的是整个开发机，留下的东西都会进镜像。交付说明写完后由本 skill 自己清理，只留 `/opt/bench` 与评测 env，
不把「保存前须删除」留给用户。

1. **归档**：`$W`（`PROGRESS`、`form.yaml`、`selfcheck.json`、冒烟日志）与安装日志 rsync 回本地，并在本地副本的
   `PROGRESS` 末尾追加 `cleanup	done	<删除清单>`。
2. **列清单**：G3 计划里批准过的写落点，除 `/opt/bench` 与评测 env 外全部删除，例如 `$W`、探测目录、pip / conda 缓存、
   冒烟用的 HOME、评测代码的临时拷贝。另外删掉安装与运行的副产物：`conda create` 写进 `<base>/pkgs` 的新包、
   `~/.conda`、`~/.nv`、`~/.cache/conda`；Kit 系还有 `/tmp/isaac-kit-s*`、`/tmp/carb.*`、`/tmp/hub-root.lock`，
   以及 env 内 `site-packages/isaacsim/kit/{cache,data,logs}/`（`--portable` 模式把 Kit 缓存与日志写在包目录里，
   着色器缓存与集群驱动不匹配，留着只增大镜像）、`~/.nvidia-omniverse`。每次冒烟都会重新生成，最后一次冒烟后再删一遍。
   副产物只删 mtime 晚于开发机启动时间、且确认是本次写入的。更稳的做法是在步 6 第一次写之前，用 `ls -A` 记下
   `/tmp`、`/root`、`/root/.cache`、`<base>/pkgs`、`/root/workspace` 的基线，收尾时与基线比对后再列清单。
3. **不动**：基础镜像自带的路径（如 `/root/workspace/vla-evaluation-harness`），以及平台生成的文件（code-server、jupyter、
   ssh、supervisor 日志）。来源拿不准的列给用户，由用户决定删不删。
4. **验证**：确认没有评测进程在跑后再删。删完激活评测 env，import `vla_eval`（Kit 系加 `isaacsim`），`du -sh` 保留路径，
   把结果写进交付说明。验证时 HOME 和缓存指向临时目录，用完删掉，不在验证过程中再留下残留。Kit 系 import `isaacsim`
   要带 `OMNI_KIT_ACCEPT_EULA=YES` 并 `</dev/null`，否则会停在 EULA 提示，显示 `The EULA was not accepted`。

## 交付边界

**做**：改开发机上的评测环境与 `/opt/bench`，本机冒烟与自检，出交付说明，收尾清理。

**不做**：保存镜像、推送仓库、提交评测、改平台代码（控制面、数据面、base 镜像）。发现 harness 能力不足只写
issue / PR 草稿给用户，用户确认后才对外提交。脚本与交付说明里不得出现凭据、开发机地址与端口。
