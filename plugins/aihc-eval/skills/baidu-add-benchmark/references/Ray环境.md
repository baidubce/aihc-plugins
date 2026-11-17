# Ray 环境

步 2 探测时 `ray_env_check.sh` 不过、或 `first_ray` 不是 `/opt/conda/bin/ray` 时读本文。
改造流程照 `baidu-add-model-server` 的同名文档，这里改成 BA 的口径，单独分发本 skill 时不依赖那份。

## 1. 先判断要不要补装

| 开发机镜像 | `ray_env_check.sh` | 动作 |
|---|---|---|
| 基于 `aihc-eval_base`（公共 BA 镜像都是） | `RAY_ENV_OK`，第一个 ray 是 `/opt/conda/bin/ray` | **什么都不做**，本文到此为止 |
| 基于 base，但第一个 ray 不是 `/opt/conda/bin/ray` | FAIL | 查谁排在前面（多半是用户往 `/usr/local/bin` 装了别的 ray），删掉或挪走，不要补装 |
| 不基于 base（NGC、裸 PyTorch、自建 conda） | FAIL 或无 ray | 按第 4 节补装，之后 `EXPECT_RAY=/usr/local/bin/ray` 重跑 |

补装会写 `/opt/ray-py`、`/opt/uv-bin`、`/opt/uv-python`、`/opt/uv-cache`、`/etc/uv/uv.toml` 与 `/usr/local/bin/{ray,uv}`，
超出默认写落点（`$W`、`/opt/bench`），**必须列进 G3 计划**，用户确认后再执行。

## 2. 为什么绕不过 Ray

自定义 benchmark 不改变运行形态：BA group 仍是同一个 RayJob 里的 worker group，控制面只换 `Image` + `ImageConfig`。
镜像要先作为 Ray worker 加入集群，`BenchmarkActor` 才能被调度上去，再由它 `Popen` 你的 `start.sh`。

KubeRay 的 init 容器与 worker 启动跑的是**裸 `ray`**（`ray start` / `ray health-check`），取镜像 ENV `PATH` 上的**第一个** `ray`：

| 项 | 值 | 比较口径 |
|---|---|---|
| ray | `ray[default]==2.49.2` | 精确等值 |
| Python | `3.13.12` | patch 级一致 |

⚠️ 平台代码里没有任何校验。不满足时 coordinator 按 hostname 子串 `-<groupKey 小写>-worker-` 找 BA 节点，
等满 600s 后报 `BA 节点缺失：-<groupKey>-worker- 无节点`（`coordinator.py` 节点发现段），离根因很远。
所以这一条必须在步 2 / 步 6 就断言掉。

## 3. 两个解释器，各管各的

| 解释器 | 跑什么 | 约束 |
|---|---|---|
| Ray 解释器（base 上是 `/opt/conda/bin/python`） | `ray start`、`BenchmarkActor`（平台 `src`，经 `PYTHONPATH` 加载，只依赖标准库与 `ray`） | 上表两个版本 |
| 评测 env（如 `/opt/conda/envs/<name>`，py ≥ 3.11） | `start.sh` 里 `exec` 的 `vla-eval run`、仿真器、适配器 | 与 Ray 无版本约束；harness 0.9.0 要求 py ≥ 3.11 |

- **不要往评测 env 里装 ray**，也不要往 Ray 解释器里装评测依赖（依赖解析会动 `ray` / `grpcio` / `protobuf`）。
- 不要试图把评测栈迁到 Python 3.13。
- `start.sh` 用评测 env 的绝对路径或 `conda activate` 进入评测 env，见 `命令契约.md`；它不需要 import ray。

## 4. 补装：四个固定落点

| 路径 | 内容 | 为什么在这 |
|---|---|---|
| `/opt/uv-bin` | uv 本体 | 根盘、非 `$HOME`，能进镜像 |
| `/opt/uv-python` | uv 托管的 CPython 3.13.12 | 同上；运行时用户要能读 |
| `/opt/ray-py` | venv，**只装** `ray[default]==2.49.2` | 与评测 env 物理隔离 |
| `/opt/uv-cache` | uv 缓存，由 `/etc/uv/uv.toml` 钉住 | 镜像不带 ENV，只能靠文件钉 |

全部 `chmod -R a+rX`：改造时是 root，运行时未必。

「保存为镜像」只 commit 可写层的**文件，不带 ENV**。SSH 会话里的 `export PATH=...` 进不了镜像，
所以让裸 `ray` 可见的唯一办法是**软链进镜像 ENV `PATH` 上已有的根盘目录**（通常 `/usr/local/bin`）。脚本先断言两点，不满足就停：

1. 该目录在镜像 ENV 的 `PATH` 上（从 `/proc/1/environ` 取，不是当前 shell 的）；
2. 该 PATH 上没有别的 `ray` 排在前面。

复用判定一律走 `env -i PATH="$IMG_PATH" HOME=/root` 口径，按 `ray` 的 shebang 反查解释器再 import。
SSH 会话里的 `ray` 常是 `export PATH` 或 `conda activate` 撑起来的，用 shell 口径会误判「已预置」。

## 5. 脚本

取自 MS skill 实测通过的版本，改成函数形式（复用命中时 `return 0`，不退出整份脚本），收口改成本 skill 的
`scripts/ray_env_check.sh`。BA 侧尚未在非 base 镜像上单独实测。

在本机写成 `setup_ray.sh`，与 `scripts/ray_env_check.sh` 一起 scp 到 `$W/setup/`，root 执行 `bash $W/setup/setup_ray.sh`。幂等，可整份重跑。

```bash
#!/usr/bin/env bash
# 补装 Ray 解释器：ray[default]==2.49.2 + Python 3.13.12，裸 ray 进镜像 PATH。
# 用法（root）：[SETUP_PROXY=http://host:port] bash setup_ray.sh
set -euo pipefail

RAY_VERSION=2.49.2
PY_VERSION=3.13.12
RAY_PREFIX=/opt/ray-py
UV_BIN_DIR=/opt/uv-bin
LINK_DIR=/usr/local/bin                      # 必须是镜像 PATH 上已有的根盘目录
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"     # ray_env_check.sh 与本脚本同目录
export UV_CACHE_DIR=/opt/uv-cache
export UV_PYTHON_INSTALL_DIR=/opt/uv-python

[ "$(id -u)" = 0 ] || { echo "需要 root" >&2; exit 1; }

# 代理默认不配；只在显式传 SETUP_PROXY 时于本进程内生效，绝不写进任何配置文件
if [ -n "${SETUP_PROXY:-}" ]; then
  export http_proxy="$SETUP_PROXY" https_proxy="$SETUP_PROXY" \
         HTTP_PROXY="$SETUP_PROXY" HTTPS_PROXY="$SETUP_PROXY" \
         no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost
fi

NET_TARGETS=(https://pypi.org/simple/ https://astral.sh)
need_net() {                                  # 只在确实要下载时探外网
  local u
  for u in "${NET_TARGETS[@]}"; do
    curl -s -o /dev/null --max-time 10 "$u" && return 0
  done
  echo "需要外网才能$1，但 ${NET_TARGETS[*]} 均不可达。" >&2
  echo "  显式传入本机可用的代理 SETUP_PROXY=http://<host>:<port> 重跑，或换基于 aihc-eval_base 的镜像。" >&2
  exit 1
}

probe_ray() {                                 # $1=ray 可执行；按 shebang 反查解释器，不依赖 PATH 与 activate
  local r="$1" py
  [ -x "$r" ] || return 1
  py="$(sed -n '1s/^#!//p' "$r" | awk '{print $1}')"
  [ -x "$py" ] || return 1
  RAY_WANT="$RAY_VERSION" PY_WANT="$PY_VERSION" "$py" -c 'import os,sys,platform,ray
sys.exit(0 if ray.__version__==os.environ["RAY_WANT"] and platform.python_version()==os.environ["PY_WANT"] else 1)' 2>/dev/null
}

check() { EXPECT_RAY="$1" bash "$SRC_DIR/ray_env_check.sh"; }
setup_ray_env() {
  # 1. 镜像 ENV 的 PATH（SSH 会话不继承），断言软链目标在其上
  IMG_PATH="$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^PATH=//p' | head -1)"
  [ -n "$IMG_PATH" ] || { echo "取不到镜像 PATH" >&2; exit 1; }
  case ":$IMG_PATH:" in
    *":$LINK_DIR:"*) ;;
    *) echo "$LINK_DIR 不在镜像 PATH 上，软链不会生效：$IMG_PATH" >&2; exit 1 ;;
  esac

  # 2. KubeRay 只认镜像 PATH 上第一个 ray：满足约束就什么都不做
  FIRST_RAY="$(env -i PATH="$IMG_PATH" HOME=/root bash -c 'command -v ray || true')"
  if [ -n "$FIRST_RAY" ] && probe_ray "$FIRST_RAY"; then
    echo "RAY_REUSE: 镜像已预置满足约束的 ray：$FIRST_RAY，跳过安装"
    check "$FIRST_RAY"; return 0
  fi
  if [ -n "$FIRST_RAY" ] && [ "$FIRST_RAY" != "$LINK_DIR/ray" ]; then
    echo "镜像 PATH 上第一个 ray 是 $FIRST_RAY，不满足 ray==$RAY_VERSION + Python $PY_VERSION。" >&2
    echo "  软链到 $LINK_DIR 盖不掉它。先确认它的来历，挪走或调整落点后重跑。" >&2
    exit 1
  fi

  # 2b. PATH 上没有 ray，但别处已有满足约束的：只建软链
  if [ -z "$FIRST_RAY" ]; then
    for c in "$RAY_PREFIX/bin/ray" /opt/conda/bin/ray /usr/bin/ray \
             /opt/conda/envs/*/bin/ray /root/miniconda3/envs/*/bin/ray; do
      probe_ray "$c" || continue
      echo "RAY_REUSE: 复用已有的 $c，只建软链"
      ln -sfn "$c" "$LINK_DIR/ray"
      chmod -R a+rX "$(dirname "$(dirname "$c")")" 2>/dev/null || true
      check "$LINK_DIR/ray"; return 0
    done
  fi
  echo "未探测到满足约束的 ray，进入补装路径"

  # 3. 镜像不带 ENV：用系统级 uv 配置把缓存钉到 /opt
  mkdir -p /etc/uv "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"
  if [ -f /etc/uv/uv.toml ] && ! grep -qx "cache-dir = \"$UV_CACHE_DIR\"" /etc/uv/uv.toml; then
    echo "/etc/uv/uv.toml 已存在且未钉 cache-dir = \"$UV_CACHE_DIR\"，人工改该行后重跑" >&2; exit 1
  fi
  [ -f /etc/uv/uv.toml ] || printf 'cache-dir = "%s"\n' "$UV_CACHE_DIR" > /etc/uv/uv.toml

  # 4. uv 本体（不改 shell rc）
  if [ ! -x "$UV_BIN_DIR/uv" ]; then
    need_net "下载 uv 安装器"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_BIN_DIR" UV_NO_MODIFY_PATH=1 sh
  fi
  ln -sfn "$UV_BIN_DIR/uv" "$LINK_DIR/uv"

  # 5. Python：优先复用已有的 $PY_VERSION
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
    PY_EXE="$PY_VERSION"
  fi

  # 6. 只装 Ray 的独立 venv
  [ -x "$RAY_PREFIX/bin/python" ] || "$UV_BIN_DIR/uv" venv --python "$PY_EXE" "$RAY_PREFIX"
  if ! probe_ray "$RAY_PREFIX/bin/ray"; then
    need_net "安装 ray[default]==$RAY_VERSION"
    "$UV_BIN_DIR/uv" pip install --python "$RAY_PREFIX/bin/python" "ray[default]==${RAY_VERSION}"
  fi
  ln -sfn "$RAY_PREFIX/bin/ray" "$LINK_DIR/ray"

  # 7. 运行时用户未必是 root
  chmod -R a+rX "$RAY_PREFIX" "$UV_PYTHON_INSTALL_DIR" "$UV_BIN_DIR" "$UV_CACHE_DIR" /etc/uv

  # 8. 收口：与步 2、交付前复跑同一份口径
  check "$LINK_DIR/ray"
}

setup_ray_env
```

末尾打出 `RAY_ENV_OK` 且退出码 0 才算过。补装后步 2 与交付前都用 `EXPECT_RAY=/usr/local/bin/ray` 跑 `ray_env_check.sh`；
脚本在 `$W/setup/` 留一份，开发机丢了能在新机原样重建。

uv venv 里 `bin/ray` 的 shebang 是绝对路径（`/opt/ray-py/bin/python`），不依赖 `VIRTUAL_ENV`、`$HOME` 与 PATH，
所以 `env -i` 下也能跑。conda 的 `ray` 常依赖 `conda activate` 注入的 `LD_LIBRARY_PATH`，软链出来未必起得来，故补装用 uv venv。

## 6. 常见失败

| 现象 | 原因 |
|---|---|
| 平台任务等满 600s 报「BA 节点缺失」 | 本文约束没真满足，或 BA Pod 没起来（拉镜像失败、资源不足）。先看 BA Pod 事件，再在开发机复跑 `ray_env_check.sh` |
| BA Pod 卡 Init，不报版本错 | ray 差一个小版本就连不上 GCS |
| 开发机上 `ray --version` 正常、镜像里没有 | 靠 shell `export PATH` 或 `conda activate` 撑起来的，没进镜像 |
| 脚本打了 `RAY_REUSE:`、镜像里却不满足 | 复用判定没走 `env -i PATH="$IMG_PATH"` 口径 |
| 脚本报「需要外网才能…」但以为已预置 | 预置版本不精确等值（ray 差小版本、Python 差 patch） |
| `/etc/uv/uv.toml` 已存在且未钉对 | 脚本刻意不覆盖；人工把 `cache-dir` 行改成 `cache-dir = "/opt/uv-cache"` 后重跑 |
| 软链目标目录是挂载盘 | 不进镜像。`findmnt -no SOURCE -T <dir>` 确认是 overlay |
| 冒烟正常、平台上 shard 报 `ModuleNotFoundError: ray` | 不会出现在 `start.sh` 里：`start.sh` 不该 import ray。查是不是把 Ray 解释器当成了评测 env |

## 7. 收尾

- uv 安装器可能在 `~/.local/bin` 留下 `python3.13` 之类的链接，交付前清掉。
- **不要 `uv cache prune`**：会删掉预建环境，运行时无外网建不回来。
- 交付说明写明补装了 Ray 及落点，提醒用户保存镜像时不要清理 `/opt/ray-py` 等目录。