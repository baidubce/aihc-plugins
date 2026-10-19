#!/usr/bin/env bash
# Copyright 2026 Baidu, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Ray 解释器约束断言（用户自定义镜像）。
#
# 复刻集群口径：KubeRay 的 init 容器与 worker 跑的是**裸 ray**（ray start / ray health-check），
# 取镜像 ENV PATH 上的**第一个** ray，版本必须与平台 head 精确一致。平台代码对此**没有任何校验**，
# 不满足时的失败形态是「MS 节点不足」等满 600s，离根因极远——所以必须在改造阶段断言掉。
#
# 同一份脚本两处复用：
#   1. 开发机改造后：bash ray_env_check.sh
#   2. 交付前清理后复跑：bash /opt/ms-<name>/ray_env_check.sh（误删只有复跑才暴露）
#
# 可覆盖：RAY_VERSION / PY_VERSION / EXPECT_RAY / IMG_PATH
set -uo pipefail

RAY_VERSION="${RAY_VERSION:-2.49.2}"
PY_VERSION="${PY_VERSION:-3.13.12}"
EXPECT_RAY="${EXPECT_RAY:-/usr/local/bin/ray}"

fail=0
ok()   { echo "[PASS] $*"; }
bad()  { echo "[FAIL] $*"; fail=1; }
note() { echo "[INFO] $*"; }

# 1. 镜像 ENV 的 PATH：交互式 SSH 会话不继承它，必须从 PID1 回取
IMG_PATH="${IMG_PATH:-$(tr '\0' '\n' < /proc/1/environ 2>/dev/null | sed -n 's/^PATH=//p' | head -1)}"
if [ -z "$IMG_PATH" ]; then
  bad "取不到镜像 PATH（/proc/1/environ 无 PATH）。显式传 IMG_PATH=... 重跑"
  exit 1
fi
note "镜像 PATH=$IMG_PATH"

# 2. PATH 上第一个 ray 必须是你软链的那个（KubeRay 只认第一个）
FIRST_RAY="$(env -i PATH="$IMG_PATH" HOME=/root bash -c 'command -v ray' 2>/dev/null || true)"
if [ -z "$FIRST_RAY" ]; then
  bad "镜像 PATH 上没有裸 ray。软链目标目录必须已在镜像 ENV PATH 上，且在根盘（overlay）"
else
  if [ "$FIRST_RAY" = "$EXPECT_RAY" ]; then
    ok "PATH 上第一个 ray = ${FIRST_RAY}"
  else
    bad "PATH 上第一个 ray 是 ${FIRST_RAY}，期望 ${EXPECT_RAY}（改 EXPECT_RAY 或调整落点）"
  fi
fi

# 3. 以运行时同口径跑裸 ray：env -i 只带镜像 PATH，不靠 conda activate / VIRTUAL_ENV
if [ -n "$FIRST_RAY" ]; then
  VER_OUT="$(env -i PATH="$IMG_PATH" HOME=/root ray --version 2>&1 || true)"
  note "ray --version → $VER_OUT"
  case "$VER_OUT" in
    *"$RAY_VERSION"*) ok "ray 版本 ${RAY_VERSION}（精确等值）" ;;
    *) bad "ray 版本不是 ${RAY_VERSION}：${VER_OUT}（差一个小版本就连不上 GCS）" ;;
  esac
  if env -i PATH="$IMG_PATH" HOME=/root ray health-check --help >/dev/null 2>&1; then
    ok "ray health-check --help 可执行"
  else
    bad "ray health-check --help 跑不起来（KubeRay 探活就用它）"
  fi

  # 4. 解释器版本：取 ray 的 shebang，不依赖 PATH 上有没有同名 python
  SHEBANG="$(head -1 "$FIRST_RAY" 2>/dev/null | sed -n 's/^#!//p')"
  RAY_PY="$(echo "$SHEBANG" | awk '{print $1}')"
  case "$RAY_PY" in                               # `#!/usr/bin/env python` 形式：取后一个 token 再解析
    */env)                                        # command 是 shell 内建，env 无法直接 exec，必须套 bash -c
      RAY_PY="$(env -i PATH="$IMG_PATH" HOME=/root \
                  bash -c 'command -v "$1"' _ "$(echo "$SHEBANG" | awk '{print $2}')" 2>/dev/null || true)" ;;
  esac
  note "ray shebang → ${RAY_PY:-<无>}"
  if [ -x "${RAY_PY:-}" ]; then
    if "$RAY_PY" - "$RAY_VERSION" "$PY_VERSION" <<'PY'
import platform, sys, ray
sys.exit(0 if (ray.__version__ == sys.argv[1]
               and platform.python_version() == sys.argv[2]) else 1)
PY
    then
      ok "ray.__version__ == ${RAY_VERSION} 且 python == ${PY_VERSION}"
    else
      "$RAY_PY" -c 'import platform,ray;print("  实际:",ray.__version__,platform.python_version())' 2>&1 || true
      bad "import 口径下版本不符（要求 ray ${RAY_VERSION} / python ${PY_VERSION}，patch 级一致）"
    fi
  else
    bad "ray 的 shebang 不是绝对路径可执行文件：${RAY_PY:-<空>}（conda 的 ray 在 env -i 下常起不来，改用 uv venv）"
  fi
fi

# 5. 落点可读：改造时是 root，运行时未必。
#    root 下 `[ -r ]` / `[ -x ]` 恒真，测不出任何东西——必须直接查权限位。
for d in /opt/ray-py /opt/uv-python /opt/uv-bin; do
  [ -e "$d" ] || continue
  if [ -n "$(find "$d" -maxdepth 0 ! -perm -o=rx -print -quit 2>/dev/null)" ]; then
    bad "$d 自身缺 o+rx，运行时非 root 进不去（chmod -R a+rX $d）"
    continue
  fi
  OFFEND="$(find "$d" \( \( -type d ! -perm -o=rx \) -o \( -type f ! -perm -o=r \) \) \
              -print 2>/dev/null | head -3 | tr '\n' ' ')"
  if [ -n "$OFFEND" ]; then
    bad "$d 树内有非 root 访问不到的条目（chmod -R a+rX $d）：$OFFEND"
  else
    ok "$d 权限位对非 root 可读可进"
  fi
done

[ "$fail" = 0 ] && echo "RAY_ENV_OK" || echo "RAY_ENV_FAILED"
exit "$fail"
