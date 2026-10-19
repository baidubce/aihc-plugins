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

# 接入表单预填探测（只读）：把开发机上能推断出的表单字段逐行打成 `字段<TAB>候选值<TAB>出处`。
# 用法：<SSH_CMD> -o BatchMode=yes 'bash -s' < probe_form_defaults.sh
#       启动脚本猜错时显式指定：<SSH_CMD> -o BatchMode=yes 'LAUNCH=/root/run_x.sh bash -s' < probe_form_defaults.sh
# 只读：不安装、不建目录、不 import 模型代码（只 import vla_eval/websockets/msgpack/PIL 探版本）。
# 输出是**候选值不是结论**：表单回显后由用户确认；不打印任何文件全文，命令行里凭据类参数一律打码。
# 不 set -e：任何一项探不到都只记「未探测到」，不中断其余各项。
export PYTHONPATH="${PYTHONPATH:-}"
emit() { printf '%s\t%s\t%s\n' "$1" "${2:-未探测到}" "${3:--}"; }
mask() { sed -E 's/((token|secret|passw(or)?d|[-_](ak|sk))[A-Za-z0-9_-]*[ =]+)[^ ]+/\1<masked>/Ig'; }

# ── 1. 启动脚本：用户手工跑通时用的那条（只作线索，最终产物不执行它）
#    python 调用常跨续行（`python x.py \` 换行再给参数），必须先合并续行再匹配，否则按行 grep 一个都命中不了
CANDS="$(for f in $(ls -t /root/*.sh /root/*/*.sh /workspace/*.sh /workspace/*/*.sh 2>/dev/null); do
  sed -e ':a' -e '/\\$/N; s/\\\n//; ta' "$f" 2>/dev/null \
    | grep -q -E 'python[0-9.]* .*(infer|serve|server|policy|deploy|--port)' && echo "$f"
done)"
LAUNCH="${LAUNCH:-$(printf '%s\n' "$CANDS" | head -1)}"
emit launch_candidates "$(printf '%s ' $CANDS)" "ls -t 脚本 + grep python…infer/serve/port，按 mtime 新→旧"
[ -f "$LAUNCH" ] || { emit launch_command "" "无候选脚本：其余字段全部回退为 未提供"; exit 0; }
# 合并续行，取最后一条非注释的 python 调用
CMD="$(sed -e ':a' -e '/\\$/N; s/\\\n//; ta' "$LAUNCH" | grep -v -E '^[[:space:]]*#' \
  | grep -E 'python[0-9.]* ' | tail -1 | sed -E 's/^[[:space:]]+//' | mask)"
emit launch_command "$LAUNCH" "最新候选脚本（LAUNCH= 可覆盖）"
emit launch_cmdline "$CMD" "$LAUNCH 里最后一条 python 调用"

# ── 2. 模型环境：脚本里的 envs/<x> 绝对路径优先，其次 conda activate <x>
ENV_PATH="$(grep -oE '/[^ "'\'']+/envs/[^/ "'\'']+' "$LAUNCH" | head -1)"
if [ -z "$ENV_PATH" ]; then
  n="$(grep -oE 'conda activate +[^ ;&"]+' "$LAUNCH" | awk '{print $3}' | tail -1)"
  for b in /root/miniconda3 /opt/conda /root/anaconda3 /opt/miniconda3; do
    [ -n "$n" ] && [ -x "$b/envs/$n/bin/python" ] && { ENV_PATH="$b/envs/$n"; break; }
  done
fi
[ -n "$ENV_PATH" ] && [ ! -x "$ENV_PATH/bin/python" ] && ENV_PATH=""
emit model_env "$ENV_PATH" "$LAUNCH 的 envs 路径 / conda activate"
# name 只是建议：取 env 名，规范成小写下划线；用户确认后不再改
NAME="$(basename "${ENV_PATH:-}" | tr 'A-Z' 'a-z' | sed -E 's/[^a-z0-9_]+/_/g; s/^_+|_+$//g')"
emit name "$NAME" "model_env 的目录名（建议值）"

PY="${ENV_PATH:+$ENV_PATH/bin/python}"
[ -x "${PY:-}" ] || PY="$(command -v python3 || echo /usr/bin/python3)"
WORKDIR="$(grep -oE '^[[:space:]]*cd +[^ ;&]+' "$LAUNCH" | awk '{print $2}' | tail -1)"
WORKDIR="${WORKDIR:-$(dirname "$LAUNCH")}"
emit workdir "$WORKDIR" "$LAUNCH 里最后一个 cd"

# ── 3. 解析启动命令行（shlex，不 import 任何模型代码）：入口、ckpt、chunk、action_dim、相机 prompt
CMD="$CMD" WORKDIR="$WORKDIR" "$PY" - <<'PY' 2>/dev/null || emit launch_parse "" "解析失败：$PY 不可用或命令行非法"
import os, re, shlex
cmd, wd = os.environ["CMD"], os.environ["WORKDIR"]
def emit(k, v, src): print(f"{k}\t{v or '未探测到'}\t{src}")
try:
    toks = shlex.split(cmd)
except ValueError:
    toks = cmd.split()
entry = ""
for i, t in enumerate(toks):
    if re.search(r"python[0-9.]*$", t):
        rest = toks[i + 1:]
        if rest[:1] == ["-m"] and len(rest) > 1:
            entry = "-m " + rest[1]
        else:
            entry = next((r for r in rest if r.endswith(".py")), "")
        break
if entry and not entry.startswith(("-m ", "/")):
    entry = os.path.normpath(os.path.join(wd, entry))
emit("entry_file", entry, "启动命令里 python 后的 .py（相对 workdir 归一化）")
# --opt v1 v2 …：多值参数（如 image-prompts）收齐到下一个 -- 为止
opts, cur = {}, None
for t in toks:
    if t.startswith("--"):
        k, _, v = t[2:].partition("=")
        cur = k.split(".")[-1].lower().replace("_", "-")
        opts.setdefault(cur, [])
        if v:
            opts[cur].append(v)
    elif cur is not None:
        opts[cur].append(t)
def pick(pat):
    for k, v in opts.items():
        if re.fullmatch(pat, k):
            return k, v
    return None, []
for field, pat in [
    ("ckpt", r"model-name-or-path|ckpt(-path)?|checkpoint(-path)?|model-path|pretrained[a-z-]*|policy-path"),
    ("chunk_len", r"chunk-size|chunk-len|action-horizon|n-action-steps|action-chunk[a-z-]*"),
    ("action_dim", r"(output-)?action-dim"),
    ("camera_slot_map", r"image-prompts?|image-keys|cameras?|camera-names"),
    ("model_port", r"port"),
]:
    k, v = pick(pat)
    emit(field, " | ".join(v) if field == "camera_slot_map" else (v[0] if v else ""),
         f"启动命令 --{k}" if k else "启动命令里无对应参数")
PY

# ── 4. ckpt 与入口文件的事实（只列名、不读内容）
CKPT="$(CMD="$CMD" "$PY" -c 'import os,re,shlex
t=shlex.split(os.environ["CMD"])
for i,x in enumerate(t[:-1]):
    if re.search(r"(model[-_]name[-_]or[-_]path|ckpt|checkpoint|model[-_]path|pretrained)[^=]*$",x) and x.startswith("--"): print(t[i+1]); break
    if x.startswith("--") and "=" in x and re.search(r"(name[-_]or[-_]path|ckpt|checkpoint|model[-_]path)=",x): print(x.split("=",1)[1]); break' 2>/dev/null)"
if [ -n "$CKPT" ] && [ -e "$CKPT" ]; then
  emit ckpt_exists "yes" "ls $CKPT"
  emit ckpt_mount "$(findmnt -no SOURCE,FSTYPE -T "$CKPT" 2>/dev/null | tr -s ' ')" "findmnt（非 overlay 即不进镜像，运行时由平台挂载）"
  emit ckpt_files "$(ls "$CKPT" 2>/dev/null | head -20 | tr '\n' ' ')" "ls（只列名）"
  # config.json 里与 chunk / action_dim 相关的键：启动命令没给时的后备出处
  CFG="$CKPT/config.json" "$PY" - <<'PY' 2>/dev/null
import json, os, re
p = os.environ["CFG"]
if os.path.isfile(p):
    d = json.load(open(p))
    hits = []
    def walk(o, pre=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if re.search(r"chunk|action_dim|horizon|auto_map", k, re.I):
                    hits.append(f"{pre}{k}={v if not isinstance(v, (dict, list)) else type(v).__name__}")
                if isinstance(v, dict) and not pre:
                    walk(v, k + ".")
    walk(d)
    print("ckpt_config_hints\t" + (" ".join(hits[:8]) or "未探测到") + "\tckpt/config.json 的 chunk/action_dim/auto_map 键")
PY
else
  emit ckpt_exists "${CKPT:+no: $CKPT}" "启动命令里的 ckpt 路径 ls 不在 ⇒ 回问用户"
fi
ENTRY="$(CMD="$CMD" WORKDIR="$WORKDIR" "$PY" -c 'import os,re,shlex
t=shlex.split(os.environ["CMD"]); e=next((x for x in t if x.endswith(".py")),"")
print(e if e.startswith("/") else os.path.normpath(os.path.join(os.environ["WORKDIR"],e)) if e else "")' 2>/dev/null)"
if [ -f "$ENTRY" ]; then
  emit predict_snippet "$(grep -nE '^[[:space:]]*def +(_?predict|_?infer(ence)?|select_action|get_action|_?initialize|load_model|_prepare_model_input|_parse_state)\b' "$ENTRY" \
    | sed -E 's/^([0-9]+):[[:space:]]*def +([A-Za-z_0-9]+).*/\2@L\1/' | tr '\n' ' ' | sed "s#^#$ENTRY:: #")" "入口文件里的 predict/初始化类函数（候选，待用户点名）"
  emit http_framework "$(grep -oE '^(from|import) +(flask|fastapi|gradio|websockets|uvicorn|tornado)' "$ENTRY" | awk '{print $2}' | sort -u | tr '\n' ' ')" "入口文件 import（gradio/demo 不可当 predict 片段）"
fi

# ── 5. 运行时下载线索（只 grep -l / 只列名，不读内容）
HITS="$(grep -rlE 'trust_remote_code *= *True|hf_hub_download|snapshot_download|torch\.hub\.load|gs://|from_pretrained\( *["'\''][A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+["'\'']' \
  --include='*.py' "$WORKDIR" 2>/dev/null | head -8 | tr '\n' ' ')"
emit runtime_download_hits "$HITS" "grep -l workdir/*.py：remote code / hub 下载 / gs://（命中≠运行时必走，需逐个核）"
emit hf_cache_repos "$(ls -d /root/.cache/huggingface/hub/models--* 2>/dev/null | sed 's#.*/models--##; s#--#/#' | head -8 | tr '\n' ' ')" \
  "HOME 下 HF 缓存的仓库名（手工跑通时下过 ⇒ 运行时也可能要；不能当镜像内容来源）"
emit runtime_downloads "" "**不自动下结论**：据上两行 + ckpt_files 里有无 tokenizer* 由用户显式确认（none 也要显式）"

# ── 6. 版本与依赖（importlib.metadata，不 import 包本体；vla_eval 除外，按 skill 口径读 _version）
"$PY" - <<'PY' 2>/dev/null || emit installed_version "" "$PY 不可执行"
import importlib.metadata as m
def ver(p):
    try: return m.version(p)
    except Exception: return "未装"
try:
    import vla_eval._version as v; hv = v.version
except Exception:
    hv = "未装"
print(f"installed_version\t{hv}\tvla_eval._version（基线 v0.5.0；未装 ⇒ SETUP_INFO 注明按 v0.5.0 核）")
print("proto_deps\t" + " ".join(f"{p}={ver(p)}" for p in ("websockets", "msgpack", "pillow")) + "\t模型环境；全有 ⇒ setup.sh DEPS_REUSE")
print("stack_versions\t" + " ".join(f"{p}={ver(p)}" for p in ("torch", "numpy", "transformers")) + "\tdeps_conflict 判定用（装 vla_eval 是否动大版本）")
PY
emit sample_action "" "无法探测：用户认可的一帧 action，只能用户给"
