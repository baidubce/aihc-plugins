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

# 自定义 benchmark 表单预填与分级探测（只读）：逐行打成 `字段<TAB>候选值<TAB>出处`。
# 用法：<SSH_CMD> -o BatchMode=yes 'bash -s' < probe_form_defaults.sh
#       猜错时显式指定：<SSH_CMD> -o BatchMode=yes 'BENCH_ENV=/opt/conda/envs/x EVAL_YAML=/root/a.yaml bash -s' < …
# 只读：不安装、不建目录、不写文件；只 import vla_eval / yaml / imageio 探版本，不 import 用户 benchmark 模块
#       （分级用 importlib.util.find_spec 看模块在不在，不执行模块代码）。
# 输出是**候选值不是结论**：步 4 回显后由用户确认，任务列表、seed、max_steps、success 的出处另查源码。
# 不 set -e：任何一项探不到都只记「未探测到」，不中断其余各项。
export PYTHONPATH="${PYTHONPATH:-}"
emit() { printf '%s\t%s\t%s\n' "$1" "${2:-未探测到}" "${3:--}"; }

# ── 1. 评测 env：所有带 vla-eval 的 conda 解释器（base 是 Ray 解释器，只作对照，不当评测 env）
PROBE_PY='
import importlib.metadata as m, sys
try:
    v = m.version("vla-eval")
except Exception:
    sys.exit(1)
import vla_eval, os
from vla_eval import config as c
tok = "auth_token" in getattr(c.ServerConfig, "__dataclass_fields__", {}) or hasattr(c.ServerConfig, "auth_token")
try:
    import imageio.v3  # noqa: F401
    import imageio_ffmpeg  # noqa: F401
    iio = "ok"
except Exception as e:
    iio = "缺(%s)" % type(e).__name__
print("%s|%s|py%d.%d|auth_token=%s|imageio=%s" % (v, os.path.dirname(vla_eval.__file__), sys.version_info[0],
      sys.version_info[1], "有" if tok else "无", iio))
'
ENVS=""
for py in /opt/conda/envs/*/bin/python; do
  [ -x "$py" ] || continue
  out="$("$py" -I -c "$PROBE_PY" 2>/dev/null)" || continue
  ENVS="$ENVS ${py%/bin/python}"
  emit "bench_env[${py%/bin/python}]" "$out" "importlib.metadata + vla_eval.__file__ + ServerConfig 字段"
done
BENCH_ENV="${BENCH_ENV:-$(echo $ENVS | awk '{print $1}')}"
emit bench_env "$BENCH_ENV" "带 vla-eval 的第一个 conda env（BENCH_ENV= 可覆盖）"
base_out="$(/opt/conda/bin/python -I -c "$PROBE_PY" 2>/dev/null)"
emit base_harness "${base_out:-无}" "/opt/conda/bin/python（Ray 解释器；h090 起 base 镜像不带 harness，无 属正常）"
BPY="$BENCH_ENV/bin/python"
[ -x "$BPY" ] || BPY=""
# harness 源码树：优先评测 env 的 vla_eval（editable 安装时上溯两级即源码树），其次 base 的，再次平台镜像的固定位置
src_root() { local pkg; pkg="$(echo "$1" | cut -d'|' -f2)"
  [ -n "$pkg" ] && [ -d "$pkg/../../configs/benchmarks" ] && (cd "$pkg/../.." && pwd); }
if [ -n "${HROOT:-}" ]; then HROOT_SRC="HROOT= 指定"
elif HROOT="$(src_root "$([ -n "$BPY" ] && "$BPY" -I -c "$PROBE_PY" 2>/dev/null)")" && [ -n "$HROOT" ]; then
  HROOT_SRC="评测 env 的 vla_eval.__file__ 上溯两级"
elif HROOT="$(src_root "$base_out")" && [ -n "$HROOT" ]; then HROOT_SRC="base 的 vla_eval.__file__ 上溯两级"
elif [ -d /root/workspace/vla-evaluation-harness/configs/benchmarks ]; then
  HROOT=/root/workspace/vla-evaluation-harness; HROOT_SRC="平台 BA 镜像的 harness 固定位置"
else HROOT=""; HROOT_SRC="评测 env、base、/root/workspace/vla-evaluation-harness 都没有源码树"; fi
emit harness_root "$HROOT" "$HROOT_SRC（HROOT= 可覆盖）"

# ── 2. 用户 eval yaml：含顶层 benchmarks: 的 yaml。harness 源码树（含 src/vla_eval 的目录，镜像里常有多份拷贝）
#       与 conda 树整棵跳过，否则候选全是上游自带配置
PRUNE=(-name .git -o -name node_modules -o -path '*/conda*')
for t in $(find /root /workspace -maxdepth 6 -type f -path '*/src/vla_eval/__init__.py' 2>/dev/null); do
  PRUNE+=(-o -path "${t%/src/vla_eval/__init__.py}")
done
YAMLS="$(find /root /workspace -maxdepth 5 \( "${PRUNE[@]}" \) -prune -o \( -name '*.yaml' -o -name '*.yml' \) \
           -type f -size -256k -print 2>/dev/null | xargs -r grep -l -E '^benchmarks:' 2>/dev/null \
           | xargs -r ls -t 2>/dev/null | head -10)"
emit eval_yaml_candidates "$(printf '%s ' $YAMLS)" "含 ^benchmarks: 的 yaml（跳过 harness 树），按 mtime 新→旧"
HCONF="$(ls -d "$HROOT"/configs/benchmarks/*/ 2>/dev/null | xargs -r -n1 basename | tr '\n' ' ')"
emit harness_builtin_benchmarks "$HCONF" "\$HROOT/configs/benchmarks/ 下的目录（上游已适配的评测集）"
EVAL_YAML="${EVAL_YAML:-$(printf '%s\n' $YAMLS | head -1)}"

# ── 3. yaml 条目与分级：find_spec + ast 只看源码，不执行用户模块
YAML_PY='
import ast, importlib.util, json, sys
import yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
ents = d.get("benchmarks") or []
def emit(k, v, src):
    print("%s\t%s\t%s" % (k, v if v not in (None, "") else "未探测到", src))
emit("yaml_entries", len(ents), "benchmarks 条目数（平台要求恰好 1）")
emit("yaml_server_timeout", (d.get("server") or {}).get("timeout"),
     "server.timeout，须为 ${oc.env:EVAL_BA_SERVER_TIMEOUT}")
if not ents:
    sys.exit(0)
e = ents[0]
ref = str(e.get("benchmark", ""))
emit("yaml_benchmark", ref, "benchmarks[0].benchmark")
emit("yaml_params", json.dumps(e.get("params") or {}, ensure_ascii=False), "benchmarks[0].params（suite/seed 出处另查）")
ms = e.get("max_steps")
emit("yaml_max_steps", ms if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0 else "缺或非正整数(%r)" % (ms,),
     "benchmarks[0].max_steps，须显式写正整数")
fixed = [k for k in ("mode", "recording", "docker", "episodes_per_task") if k in e]
emit("yaml_platform_keys", ",".join(fixed) or "无", "这些键由平台 --benchmark-field 施加，yaml 里应删掉")
mod, _, cls = ref.partition(":")
spec = None
try:
    spec = importlib.util.find_spec(mod) if mod else None
except Exception:
    spec = None
src = getattr(spec, "origin", None) if spec is not None else None
emit("adapter_file", src, "importlib.util.find_spec(%s)" % mod)
bases = None
if src and src.endswith(".py"):
    for n in ast.walk(ast.parse(open(src).read())):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            bases = [getattr(b, "id", None) or getattr(b, "attr", "?") for b in n.bases]
emit("adapter_class_bases", ",".join(bases) if bases is not None else None, "ast 找 class %s 的基类" % cls)
print("GRADE\t%s" % ("A" if bases is not None else "?"))
'
GRADE="?"
if [ -n "$EVAL_YAML" ] && [ -n "$BPY" ]; then
  emit eval_yaml "$EVAL_YAML" "eval_yaml_candidates 第一个（EVAL_YAML= 可覆盖）"
  yout="$(cd / && "$BPY" -I -c "$YAML_PY" "$EVAL_YAML" 2>&1)"
  printf '%s\n' "$yout" | grep -v '^GRADE' | grep -P '\t' || emit yaml_parse "" "解析失败：$(printf '%s' "$yout" | tail -1)"
  GRADE="$(printf '%s\n' "$yout" | sed -n 's/^GRADE\t//p')"
fi

# 没有 yaml 或适配器找不到时，看用户代码里有没有 StepBenchmark 子类，再看评测 env 里装了哪些仿真器
ADAPTERS="$(find /root /workspace -maxdepth 6 \( "${PRUNE[@]}" \) -prune -o -name '*.py' -type f -size -1M \
              -print 2>/dev/null \
              | xargs -r grep -l -E 'class [A-Za-z0-9_]+\(.*StepBenchmark' 2>/dev/null | head -5)"
emit user_adapters "$(printf '%s ' $ADAPTERS)" "grep 用户目录里继承 StepBenchmark 的类（不含 harness 自带）"
SIMS=""
[ -n "$BPY" ] && SIMS="$("$BPY" -I -c '
import importlib.util as u
names = ["mujoco", "robosuite", "libero", "sapien", "mani_skill", "pybullet", "isaacsim", "omni", "omnigibson",
         "genesis", "habitat_sim", "pyrep", "metaworld"]
print(" ".join(n for n in names if u.find_spec(n) is not None))' 2>/dev/null)"
emit simulators "$SIMS" "评测 env 里 find_spec 能找到的仿真器包"
# 仿真器包名与 harness 自带配置目录同名：上游已有适配器，用户没写 yaml 也算 A 档候选
BUILTIN=""
for sim in $SIMS; do [ -d "$HROOT/configs/benchmarks/$sim" ] && BUILTIN="$BUILTIN $sim"; done
emit builtin_match "${BUILTIN# }" "simulators 与 harness_builtin_benchmarks 的交集"
if [ "$GRADE" != "A" ]; then
  if [ -n "$ADAPTERS" ]; then GRADE="A?"
  elif [ -n "$BUILTIN" ]; then GRADE="A?"
  elif printf '%s' "$SIMS" | grep -q -E 'isaacsim|omni'; then GRADE="C?"
  elif [ -n "$SIMS" ]; then GRADE="B"
  else GRADE="?"; fi
fi
case "$GRADE" in
  A)  why="eval yaml 的 benchmark 能定位到适配器类" ;;
  A?) why="有 StepBenchmark 子类或上游同名适配器，但没有用户 eval yaml；核对后按 A 档补 yaml" ;;
  B)  why="有仿真器、没有适配器；步 6 照 references/适配器指南.md 写 StepBenchmark 子类（草稿流程）" ;;
  C?) why="Isaac/Omniverse 系，多半要独立仿真进程；先讲清 references/上游缺口.md 的 C? 节，用户确认才继续" ;;
  *)  why="既没有 yaml/适配器也没认出仿真器，问用户原评测怎么跑" ;;
esac
emit grade_hint "$GRADE" "$why"

# ── 4. GPU 与图形栈（底线 2：渲染在 NVIDIA 设备）
GPU="$(nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader 2>/dev/null \
        | tr '\n' ';' | sed 's/;$//')"
emit gpus "$GPU" "nvidia-smi --query-gpu"
ICD="$(ls /usr/share/vulkan/icd.d/*.json /etc/vulkan/icd.d/*.json 2>/dev/null | tr '\n' ' ')"
emit vulkan_icds "$ICD" "ls icd.d（只应有 nvidia_icd.json；lvp/lavapipe 存在时要确认 VK_ICD_FILENAMES 钉住 NVIDIA）"
VKDEV="$(command -v vulkaninfo >/dev/null 2>&1 \
          && vulkaninfo --summary 2>/dev/null | sed -n 's/^[[:space:]]*deviceName[[:space:]]*=[[:space:]]*//p' \
          | tr '\n' ';' | sed 's/;$//')"
emit vulkan_devices "$VKDEV" "vulkaninfo --summary 的 deviceName（含 llvmpipe 即软渲染可见）"
EGL="$(ls /usr/share/glvnd/egl_vendor.d/ 2>/dev/null | tr '\n' ' ')"
emit egl_vendors "$EGL" "/usr/share/glvnd/egl_vendor.d（MuJoCo/robosuite 走 EGL 需 10_nvidia.json）"
IMG_ENV_GFX="$(tr '\0' '\n' < /proc/1/environ 2>/dev/null \
                | grep -E '^(VK_ICD_FILENAMES|MUJOCO_GL|PYOPENGL_PLATFORM|EGL_PLATFORM|__EGL_VENDOR)' \
                | tr '\n' ' ')"
emit image_env_graphics "${IMG_ENV_GFX:-无}" "/proc/1/environ 里的渲染变量（镜像 ENV 写死会覆盖平台默认值，应为 无）"

# ── 5. 交付落点与 Ray
if [ -e /opt/bench ]; then
  emit opt_bench "$(ls -A /opt/bench 2>/dev/null | tr '\n' ' ')" "/opt/bench 已存在，步 6 前先看内容，别覆盖"
else
  emit opt_bench "不存在" "步 6 新建"
fi
IMG_PATH="$(tr '\0' '\n' < /proc/1/environ 2>/dev/null | sed -n 's/^PATH=//p' | head -1)"
FIRST_RAY="$(env -i PATH="$IMG_PATH" HOME=/root bash -c 'command -v ray' 2>/dev/null)"
emit first_ray "$FIRST_RAY" "镜像 PATH 上第一个 ray（期望 /opt/conda/bin/ray，详查跑 ray_env_check.sh）"
emit workspace_free "$(df -h /root/workspace 2>/dev/null | awk 'NR==2{print $4" 可用 / "$2}')" "df -h /root/workspace"
emit shm "$(df -h /dev/shm 2>/dev/null | awk 'NR==2{print $2}')" "df -h /dev/shm"
exit 0
