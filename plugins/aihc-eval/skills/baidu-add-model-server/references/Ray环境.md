# 补装 Ray 解释器

本文是 SKILL.md 阶段 2 的全文。适用于**重档开发机**（镜像里没有 `/opt/conda`、或 PATH 上没有符合版本的 `ray`），
典型是 NGC / 裸 PyTorch / 自建 conda 的机器。轻档（基础镜像本身已满足 Ray 约束，常见于上一轮改造过的镜像）跳过本文。

## 为什么绕不过 Ray

自定义镜像**不改变运行形态**：仍是同一个 RayJob 里的 `WorkerMSCustom` worker group 跑
`ModelServerActor`（`num_gpus=1`），控制面的自定义镜像分支只换 `Image` + `ImageConfig`。
即镜像要先能作为 Ray worker 加入集群，才轮得到你的模型服务被拉起。

KubeRay 的 init 容器与 worker 启动跑的是**裸 `ray`**（`ray start` / `ray health-check`），
取的是 PATH 上**第一个** `ray`。版本必须与平台 head 一致：

| 项 | 值 | 比较口径 |
|---|---|---|
| ray | `ray[default]==2.49.2` | 精确等值 |
| Python | `3.13.12` | patch 级一致 |

⚠️ **平台代码里没有任何校验**：声明式的 Ray/Python 版本常量没有读取方。
不满足时的失败形态是 coordinator 按 hostname 子串 `-workermscustom-worker-` 找 MS 节点，等满 600s，
最后报「MS 节点不足」——离根因极远。所以这一条必须在改造阶段就断言掉。

**模型环境不受此约束。** 推理栈该是 Python 3.10 + torch 就还是 3.10 + torch，另起一套即可。
不要试图把推理栈迁到 3.13，也不要往 Ray 那个环境里装模型依赖（依赖解析会动 `ray` / `grpcio` / `protobuf`）。

## 装哪：四个固定落点

| 路径 | 内容 | 为什么在这 |
|---|---|---|
| `/opt/uv-bin` | uv 本体 | 根盘、非 `$HOME`，能进镜像 |
| `/opt/uv-python` | uv 托管的 CPython 3.13.12 | 同上；运行时用户要能读 |
| `/opt/ray-py` | venv，**只装** `ray[default]==2.49.2` | 与模型环境物理隔离 |
| `/opt/uv-cache` | uv 缓存，由 `/etc/uv/uv.toml` 钉住 | 镜像不带 ENV，只能靠文件钉 |

全部 `chmod -R a+rX`：改造时是 root，运行时未必。

## 怎么不靠 ENV 进 PATH

将来「保存为镜像」的本质是 commit 可写层：**只带文件，不带 ENV**。你在 SSH 会话里 `export PATH=...` 不会进镜像。
所以让裸 `ray` 可见的唯一办法是——**软链进镜像 ENV `PATH` 上已有的一个根盘目录**（通常 `/usr/local/bin`）。

两个前置条件必须在脚本里先断言，不满足就停：

1. 该目录确实在镜像 ENV 的 `PATH` 上（从 `/proc/1/environ` 取 PATH，不是取当前 shell 的）；
2. 该 PATH 上**没有其它 `ray`** 排在前面——KubeRay 只认第一个。

同理 `uv` 也软链一份过去，自检与后续脚本才能在 `env -i` 下调到它。

## 先探测可复用，再决定装

**「要不要联网」是探测的结论，不是脚本的前提。** 重档机也可能已预置符合约束的 Python 3.13.12 与
`ray[default]==2.49.2`（自建 conda、别人改造过的镜像、上一轮改造的残留），这时一次下载都不需要。
所以脚本先探测、打一行提示，只有**确实要下载**的那几步才调 `need_net` 探外网、才可能硬失败。

三条复用路径（任一命中即收口于 `ray_env_check.sh`，零下载）：

| 探测结果 | 动作 | 提示前缀 |
|---|---|---|
| 镜像 PATH 上第一个 `ray` 已满足约束 | 什么都不做 | `RAY_REUSE:` |
| PATH 上没有 `ray`，但 conda / 既有 venv 里有满足约束的 | **只建软链** | `RAY_REUSE:` |
| `/opt/ray-py` 已建好且满足约束 | 跳过 `pip install` | `RAY_REUSE:` |
| 机器上已有精确 `3.13.12` 解释器 | 跳过 `uv python install` | `PY_REUSE:` |

只有一种情形必须硬失败：**PATH 上第一个 `ray` 存在但不满足约束**。KubeRay 只会用它，软链到
`$LINK_DIR` 也盖不掉（`$LINK_DIR` 多半排在后面），这时继续装只会装出一个永远用不上的 venv。

⚠️ **复用判定必须走 `env -i PATH="$IMG_PATH" HOME=/root` 口径**，不能用当前 shell 的 `command -v ray`。
SSH 会话里的 `ray` 常是 `export PATH` 或 `conda activate` 撑起来的，进不了镜像；
用 shell 口径探测会得出「已预置、无需安装」的错误结论，而镜像里根本没有。
`probe_ray` 同理按 `ray` 的 **shebang 反查解释器**再 import，不依赖 PATH 与 activate。

## 脚本（实测通过）

落在本地不入库的工作目录，上传到开发机 `/tmp/setup-<name>/` 后 heredoc 执行。**幂等**，可整份重跑。

```bash
#!/usr/bin/env bash
# 补装 Ray 解释器：ray[default]==2.49.2 + Python 3.13.12，裸 ray 进镜像 PATH。
# 用法（root）：[SETUP_PROXY=http://host:port] bash setup.sh
set -euo pipefail

RAY_VERSION=2.49.2
PY_VERSION=3.13.12
RAY_PREFIX=/opt/ray-py
UV_BIN_DIR=/opt/uv-bin
LINK_DIR=/usr/local/bin                      # 必须是镜像 PATH 上已有的根盘目录
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"     # ray_env_check.sh 与本脚本同目录上传
export UV_CACHE_DIR=/opt/uv-cache
export UV_PYTHON_INSTALL_DIR=/opt/uv-python

[ "$(id -u)" = 0 ] || { echo "需要 root" >&2; exit 1; }

# 代理：**默认不配**。只有显式传入 SETUP_PROXY 时才在本进程内生效，绝不写进任何配置文件。
# 代理地址随环境变化，默认写上会在没有该代理的机器上把每个下载挂成连接超时。
if [ -n "${SETUP_PROXY:-}" ]; then
  export http_proxy="$SETUP_PROXY" https_proxy="$SETUP_PROXY" \
         HTTP_PROXY="$SETUP_PROXY" HTTPS_PROXY="$SETUP_PROXY" \
         no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost
fi

# 外网只在**确实要下载**时才探测、才硬失败。镜像里可能已预置符合约束的 ray/python，那就一次都不用联网。
NET_TARGETS=(https://pypi.org/simple/ https://astral.sh)
need_net() {                                  # $1=用途描述
  local u
  for u in "${NET_TARGETS[@]}"; do
    curl -s -o /dev/null --max-time 10 "$u" && return 0
  done
  echo "需要外网才能$1，但 ${NET_TARGETS[*]} 均不可达。" >&2
  echo "  直连不通时显式传入本机实际可用的代理：SETUP_PROXY=http://<host>:<port> 重跑；" >&2
  echo "  或换一个已预置 ray[default]==$RAY_VERSION + Python $PY_VERSION 的基础镜像。" >&2
  exit 1
}

# 判定某个 ray 可执行是否已满足约束（按 shebang 反查解释器，不依赖 PATH 与 activate）
probe_ray() {                                 # $1=ray 可执行路径
  local r="$1" py
  [ -x "$r" ] || return 1
  py="$(sed -n '1s/^#!//p' "$r" | awk '{print $1}')"
  [ -x "$py" ] || return 1
  RAY_WANT="$RAY_VERSION" PY_WANT="$PY_VERSION" "$py" -c 'import os,sys,platform,ray
sys.exit(0 if ray.__version__==os.environ["RAY_WANT"] and platform.python_version()==os.environ["PY_WANT"] else 1)' 2>/dev/null
}

# 1. 取镜像 ENV 的 PATH（SSH 会话不继承它），断言软链目标在其上
IMG_PATH="$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^PATH=//p' | head -1)"
[ -n "$IMG_PATH" ] || { echo "取不到镜像 PATH" >&2; exit 1; }
case ":$IMG_PATH:" in
  *":$LINK_DIR:"*) ;;
  *) echo "$LINK_DIR 不在镜像 PATH 上，软链不会生效：$IMG_PATH" >&2; exit 1 ;;
esac

# 2. 复用探测：KubeRay 只认镜像 PATH 上**第一个** ray，所以先看它满不满足约束
FIRST_RAY="$(env -i PATH="$IMG_PATH" HOME=/root bash -c 'command -v ray || true')"
if [ -n "$FIRST_RAY" ] && probe_ray "$FIRST_RAY"; then
  echo "RAY_REUSE: 镜像已预置满足约束的 ray：$FIRST_RAY —— 跳过全部安装，不需要外网"
  bash "$SRC_DIR/ray_env_check.sh"            # 仍按同一口径断言一次
  exit 0                                      # 合并进 setup.sh 时改成跳过本段（见文末）
fi
if [ -n "$FIRST_RAY" ] && [ "$FIRST_RAY" != "$LINK_DIR/ray" ]; then
  echo "镜像 PATH 上第一个 ray 是 $FIRST_RAY，且不满足 ray==$RAY_VERSION + Python $PY_VERSION。" >&2
  echo "  KubeRay 只会用它，软链到 $LINK_DIR 也盖不掉。先确认它的版本或调整落点后重跑。" >&2
  exit 1
fi

# 2b. PATH 上没有 ray，但机器别处可能已有满足约束的（conda / 既有 venv）→ 只软链，同样不用外网
if [ -z "$FIRST_RAY" ]; then
  for c in "$RAY_PREFIX/bin/ray" /opt/conda/bin/ray /usr/bin/ray \
           /opt/conda/envs/*/bin/ray /root/miniconda3/envs/*/bin/ray; do
    probe_ray "$c" || continue
    echo "RAY_REUSE: 复用已有的 $c —— 只建软链，不需要外网"
    ln -sfn "$c" "$LINK_DIR/ray"
    chmod -R a+rX "$(dirname "$(dirname "$c")")" 2>/dev/null || true
    bash "$SRC_DIR/ray_env_check.sh"
    exit 0
  done
fi
echo "未探测到满足约束的 ray，进入补装路径（下面各步会在真要下载时才检查外网）"

# 3. 镜像不带 ENV：用系统级 uv 配置把缓存钉到 /opt（对 root 与运行时用户都生效）
mkdir -p /etc/uv "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"
if [ -f /etc/uv/uv.toml ] && ! grep -qx "cache-dir = \"$UV_CACHE_DIR\"" /etc/uv/uv.toml; then
  echo "/etc/uv/uv.toml 已存在且未钉 cache-dir = \"$UV_CACHE_DIR\"，人工改该行后重跑（脚本不改用户文件）" >&2; exit 1
fi
[ -f /etc/uv/uv.toml ] || printf 'cache-dir = "%s"\n' "$UV_CACHE_DIR" > /etc/uv/uv.toml   # 已钉对的不重写，保留用户其它行

# 4. uv 本体（不改任何 shell rc）。已有就复用，缺了才联网
if [ ! -x "$UV_BIN_DIR/uv" ]; then
  need_net "下载 uv 安装器"
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="$UV_BIN_DIR" UV_NO_MODIFY_PATH=1 sh
fi
ln -sfn "$UV_BIN_DIR/uv" "$LINK_DIR/uv"

# 5. Python：优先复用机器上已有的 $PY_VERSION，没有才让 uv 下载
PY_EXE=""
for p in "$UV_PYTHON_INSTALL_DIR"/cpython-"$PY_VERSION"*/bin/python3 \
         /usr/local/bin/python3.13 /usr/bin/python3.13 /opt/conda/bin/python3.13; do
  [ -x "$p" ] || continue
  [ "$("$p" -c 'import platform;print(platform.python_version())' 2>/dev/null)" = "$PY_VERSION" ] || continue
  PY_EXE="$p"; echo "PY_REUSE: 复用已有解释器 $p"; break
done
if [ -z "$PY_EXE" ]; then
  need_net "下载 CPython $PY_VERSION"
  "$UV_BIN_DIR/uv" python install "$PY_VERSION"
  PY_EXE="$PY_VERSION"                        # uv venv 也接受裸版本号
fi

# 6. 只装 Ray 的独立 venv。已满足约束就不再 pip install
[ -x "$RAY_PREFIX/bin/python" ] || "$UV_BIN_DIR/uv" venv --python "$PY_EXE" "$RAY_PREFIX"
if probe_ray "$RAY_PREFIX/bin/ray"; then
  echo "RAY_REUSE: $RAY_PREFIX 已满足约束，跳过 pip install"
else
  need_net "安装 ray[default]==$RAY_VERSION"
  "$UV_BIN_DIR/uv" pip install --python "$RAY_PREFIX/bin/python" "ray[default]==${RAY_VERSION}"
fi
ln -sfn "$RAY_PREFIX/bin/ray" "$LINK_DIR/ray"

# 7. 运行时用户未必是 root
chmod -R a+rX "$RAY_PREFIX" "$UV_PYTHON_INSTALL_DIR" "$UV_BIN_DIR" "$UV_CACHE_DIR" /etc/uv

# 8. 断言：统一走本 skill 的 scripts/ray_env_check.sh（与交付前复跑同一份口径）
#    它自己会取 /proc/1/environ 的 PATH、核「第一个 ray」、以 env -i 跑 ray --version /
#    ray health-check --help，并按 ray 的 shebang 反查解释器验 ray.__version__ 与 python 版本。
bash "$SRC_DIR/ray_env_check.sh"                    # 它自己打 RAY_ENV_OK，退出码 0 才算过
```

脚本可覆盖 `RAY_VERSION` / `PY_VERSION` / `EXPECT_RAY` / `IMG_PATH`，默认就是上表那两个版本 +
`EXPECT_RAY=/usr/local/bin/ray`。改了 `LINK_DIR` 记得同步传 `EXPECT_RAY`。

末尾打出 `RAY_ENV_OK` 且退出码 0 才算过（三条复用路径与补装路径都以它收口）。

⚠️ 上面第 2 步的两处 `exit 0` 是「本文脚本独立运行」的写法。**合并进 `setup.sh` 第 1 段时要改成跳过本段而不是退出整份脚本**——
最简单的做法是把第 0–8 步包成一个函数 `setup_ray_env()`，`exit 0` 改 `return 0`，再 `setup_ray_env` 调用一次。

## 为什么软链在 `env -i` 下也能用

uv venv 里 `bin/ray` 的 shebang 是**绝对路径**（指向 `/opt/ray-py/bin/python`），
不依赖 `VIRTUAL_ENV`、不依赖 `$HOME`、不依赖 PATH 上有没有对应 python。
所以 `env -i PATH="<镜像PATH>" HOME=/root ray --version` 能直接跑通——这正是集群里的口径。

反例：conda 环境里的 `ray` 常依赖 `conda activate` 注入的 `LD_LIBRARY_PATH` / `PYTHONHOME`，
直接软链出来在 `env -i` 下可能起不来。所以这里用 uv venv 而不是 conda prefix。

## 常见失败

| 现象 | 原因 |
|---|---|
| 「MS 节点不足」等满 600s | 本文的约束没真满足。跑 `bash /opt/ms-<name>/ray_env_check.sh` 复验 |
| Pod 卡 Init，不报版本错 | ray 差一个小版本就连不上 GCS |
| 开发机上 `ray --version` 正常、镜像里没有 | 版本是靠 shell `export PATH` 或 conda activate 撑起来的，没进镜像 |
| `/etc/uv/uv.toml` 已存在且未钉对 | 脚本刻意失败、不覆盖；人工把 `cache-dir` 行改成 `cache-dir = "/opt/uv-cache"`（其余行不动）后重跑 |
| 脚本打了 `RAY_REUSE:`、镜像里却不满足 | 复用判定没走 `env -i PATH="$IMG_PATH"` 口径，那个 `ray` 是 shell `export PATH` / `conda activate` 撑起来的 |
| 脚本报「需要外网才能…」但你以为已预置 | 预置的版本不精确等值（ray 差小版本、Python 差 patch），`probe_ray` 判不通过 |
| 软链目标目录是挂载盘 | 不进镜像。用 `findmnt -no SOURCE -T <dir>` 确认是 overlay |

## 收尾

- 装完复查一遍残留：uv 安装器可能在 `$HOME/.local/bin` 留下 `python3.13` 之类的链接，交付前清理掉。
- **不要 `uv cache prune`**：会删掉预建环境，运行时无外网再也建不回来。
- 本段改动与后续服务改动写在**同一份** `setup.sh` 里（本文脚本是它的第 1 段），
  这样产物丢了能用同一份脚本在新开发机上原样重建。
