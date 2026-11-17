#!/usr/bin/env python
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

# -*- coding: utf-8 -*-
"""自定义 benchmark 镜像的本机冒烟：按平台口径注入 EVAL_BA_*，同机并发起 N 个 shard 执行交付的 command。

平台对每个 shard 执行同一条 command，只改 EVAL_BA_SHARD_ID 与 VIDEO_DIR 里的 {shard_id}；N 个 shard 在同一个
Pod 里并发、共用 OUTPUT_DIR 下的 recording DB。冒烟产物交给 ba_selfcheck.py 判定。

平台用 shlex.split 切分 command、不经 shell 执行，本脚本为了兼容测试命令经 shell 执行。交付的 command
只用 ``bash /opt/bench/start.sh`` 形式，变量与重定向写在 start.sh 里，两种执行方式结果相同。

用法：
    python smoke_run.py --workdir <W> --server-url ws://127.0.0.1:<port> --bench-name <name>
        --command 'bash /opt/bench/start.sh' [--set EVAL_BA_NUM_SHARDS=2 ...] [--env KEY=V ...]

产物：<W>/out/recording-<eval_id>.sqlite、<W>/videos/shard<i>/、<W>/logs/shard<i>.log、<W>/smoke_env.json、
<W>/graphics_env.json（子进程实际拿到的图形栈变量）、<W>/progress_seen.json（各 shard 运行中观察到的 .progress）、
<W>/smoke_run.json（实际执行的 command 与各 shard 退出码，ba_selfcheck 据此查 M2 / M4 与 G5 逐字一致）。

harness 在 benchmark 结束时删掉 ``.progress``，跑完再查必然找不到，所以本脚本在 shard 运行期间轮询并记录。
每次冒烟用新的 workdir：目录里已有上次的产物时直接退出，避免自检读到旧数据。

退出码：全部 shard 退出码为 0 时返回 0，否则 1；workdir 里有旧产物返回 2；Ctrl-C 中断返回 130（产物作废）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

# 平台不经 shell 执行 command，含这些字符说明它依赖 shell 展开，上平台后行为会变
SHELL_METACHARS = ("$", ">", "<", "|", "&", ";", "`")
# 平台按 key 过滤掉的代理变量（benchmark_actor._PROXY_ENV_KEYS）
_PROXY_ENV_KEYS = {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
# 平台在用户 envs 之前设的图形栈默认值（benchmark_actor._build_env）
_PLATFORM_GRAPHICS_ENV = {"MUJOCO_GL": "egl", "PYOPENGL_PLATFORM": "egl", "EGL_PLATFORM": "device"}
_NVIDIA_ICD_CANDIDATES = ("/etc/vulkan/icd.d/nvidia_icd.json", "/usr/share/vulkan/icd.d/nvidia_icd.json")
# 写进 graphics_env.json 的键：平台注入的图形栈变量，加上镜像或用户 envs 可能带进来的软渲染开关
GRAPHICS_KEYS = ("MUJOCO_GL", "PYOPENGL_PLATFORM", "EGL_PLATFORM", "MUJOCO_EGL_DEVICE_ID", "VK_ICD_FILENAMES",
                 "VK_DRIVER_FILES", "LIBGL_ALWAYS_SOFTWARE", "CUDA_VISIBLE_DEVICES")
# 与 harness orchestrator._SAFE_NAME_RE 一致：.progress 文件名用 safe_name
_SAFE_NAME_RE = re.compile(r"[^\w\-.]")
PROGRESS_POLL_SEC = 0.5
# 中断时 SIGTERM 之后等 shard 自行退出的秒数，超时再 SIGKILL
TERMINATE_GRACE_SEC = 5.0
# 平台经 EVAL_BA_* 下发、command 不得带的参数，与 ba_selfcheck.FORBIDDEN_FLAGS + COMMAND_ONLY_FLAGS 一致
FORBIDDEN_FLAGS = ("--server-url", "--shard-id", "--num-shards", "--eval-id", "--output-dir", "--render",
                   "--record-video", "--no-record-video")
# workdir 里出现即视为上次冒烟的产物（peak.json / peak.log 由 shard_peak.py 提前写，不算）
_STALE_FILES = ("smoke_env.json", "smoke_run.json", "progress_seen.json", "graphics_env.json", "selfcheck.json")
_STALE_DIRS = ("videos", "logs")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        argparse.Namespace，含 workdir、server_url、bench_name、command、set、env 六个字段。

    Raises:
        SystemExit: 缺少必填参数时 argparse 打印用法并以退出码 2 结束。
    """
    parser = argparse.ArgumentParser(description="自定义 benchmark 镜像本机冒烟")
    parser.add_argument("--workdir", required=True, help="冒烟工作目录，产物都落在这里")
    parser.add_argument("--server-url", required=True, help="模型服务地址，如 ws://127.0.0.1:8000")
    parser.add_argument("--bench-name", required=True, help="控制台条目的 name")
    parser.add_argument("--command", required=True, help="交付给控制台的 command，原样执行")
    parser.add_argument("--set", action="append", default=[], metavar="EVAL_BA_X=V",
                        help="覆盖默认的 EVAL_BA_* 值，可重复")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=V",
                        help="控制台条目里的 envs，可重复；不能以 EVAL_BA_ 开头")
    return parser.parse_args(argv)


def base_env(workdir: str, server_url: str, bench_name: str) -> dict[str, str]:
    """构造平台口径的 EVAL_BA_* 默认值。

    Args:
        workdir: 冒烟工作目录，OUTPUT_DIR 与 VIDEO_DIR 由它派生。
        server_url: 模型服务地址。
        bench_name: 控制台条目的 name。

    Returns:
        EVAL_BA_* 默认值字典；VIDEO_DIR 保留 {shard_id} 模板，由 shard_env 替换。
    """
    return {
        "EVAL_BA_SERVER_URL": server_url,
        "EVAL_BA_NUM_SHARDS": "2",
        "EVAL_BA_EVAL_ID": "em-smoke",
        "EVAL_BA_OUTPUT_DIR": os.path.join(workdir, "out"),
        "EVAL_BA_RENDER": "gpu",
        "EVAL_BA_BENCH_NAME": bench_name,
        "EVAL_BA_EPISODES_PER_TASK": "1",
        "EVAL_BA_MODE": "sync",
        "EVAL_BA_RECORD_VIDEO": "true",
        "EVAL_BA_VIDEO_DIR": os.path.join(workdir, "videos", "shard{shard_id}"),
        "EVAL_BA_SUB_BENCH": "",
        "EVAL_BA_SERVER_TIMEOUT": "30",
        "EVAL_BA_MODEL_NAME": "echo",
        "EVAL_BA_MODEL_SOURCE": "smoke",
    }


def apply_overrides(env: dict[str, str], pairs: list[str]) -> dict[str, str]:
    """把 --set KEY=V 合进 env。

    Args:
        env: base_env 构造出的默认值。
        pairs: --set 传入的 KEY=V 列表。

    Returns:
        合入覆盖值后的新 dict，不改动入参 env。

    Raises:
        ValueError: 格式不是 KEY=V，或 KEY 不以 EVAL_BA_ 开头（其余 env 由用户在控制台 envs 里给）。
    """
    out = dict(env)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.startswith("EVAL_BA_"):
            raise ValueError(f"--set 只接受 EVAL_BA_*=值，收到 {pair!r}")
        out[key] = value
    return out


def shard_env(env: dict[str, str], shard_id: int) -> dict[str, str]:
    """构造第 shard_id 个 shard 的 EVAL_BA_*。

    Args:
        env: 平台口径的 EVAL_BA_* 默认值。
        shard_id: shard 编号，从 0 开始。

    Returns:
        新 dict：SHARD_ID 已设置，VIDEO_DIR 里的 {shard_id} 已替换为编号。
    """
    out = dict(env)
    out["EVAL_BA_SHARD_ID"] = str(shard_id)
    out["EVAL_BA_VIDEO_DIR"] = out["EVAL_BA_VIDEO_DIR"].replace("{shard_id}", str(shard_id))
    return out


def _harness_version() -> str | None:
    """本机 vla-eval 版本（平台取 Head 上装的版本）；没装返回 None，此时不注入、自检跳过版本核对。"""
    try:
        import importlib.metadata as metadata
        return metadata.version("vla-eval")
    except Exception:  # noqa: BLE001  包不在或元数据坏都按没装处理
        return None


def parse_user_envs(pairs: list[str]) -> dict[str, str]:
    """解析 --env KEY=V（控制台 envs）。

    Args:
        pairs: --env 传入的 KEY=V 列表。

    Returns:
        KEY 到值的字典。

    Raises:
        ValueError: 格式不是 KEY=V，或 KEY 以 EVAL_BA_ 开头（平台会丢弃这类键）。
    """
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key or key.startswith("EVAL_BA_"):
            raise ValueError(f"--env 只接受非 EVAL_BA_ 的 KEY=值，收到 {pair!r}")
        out[key] = value
    return out


def _child_env(env: dict[str, str], user_envs: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程 env，叠加顺序与平台一致。

    Args:
        env: 本 shard 的 EVAL_BA_* 值。
        user_envs: 控制台条目里的 envs；None 表示没有。

    Returns:
        新 dict：本机环境 → 图形栈默认值 → 用户 envs → 平台 EVAL_BA_*；代理变量已去掉。
    """
    out = dict(os.environ)
    out.update(_PLATFORM_GRAPHICS_ENV)
    # 平台取 CUDA_VISIBLE_DEVICES 首项，为空补 0；自定义 benchmark 不改写 CUDA_VISIBLE_DEVICES 本身
    cuda = out.get("CUDA_VISIBLE_DEVICES", "")
    out["MUJOCO_EGL_DEVICE_ID"] = cuda.split(",")[0] if cuda else "0"
    icd = next((p for p in _NVIDIA_ICD_CANDIDATES if os.path.exists(p)), None)
    if icd is not None:
        out["VK_ICD_FILENAMES"] = icd
    out.update(user_envs or {})
    out = {k: v for k, v in out.items() if k.lower() not in _PROXY_ENV_KEYS and not k.startswith("EVAL_BA_")}
    out.update(env)
    return out


def _warn_shell(command: str) -> None:
    """command 含 shell 元字符时告警。

    Args:
        command: 交付给控制台的 command。

    Returns:
        None。命中元字符时往 stderr 打一行告警：平台不经 shell 执行，这类写法上平台后行为会变。
    """
    found = [c for c in SHELL_METACHARS if c in command]
    if found:
        print(f"WARN command 含 shell 元字符 {' '.join(found)}；平台用 shlex.split 执行、不展开变量与重定向，"
              "交付前改成 bash /opt/bench/start.sh 形式", file=sys.stderr)


def _warn_flags(command: str) -> None:
    """command 带平台下发的参数时告警：这些参数只能在 start.sh 里取 $EVAL_BA_*，自检的 command_literal 会判失败。"""
    try:
        args = shlex.split(command)
    except ValueError:
        return
    found = [a for a in args if any(a == f or a.startswith(f + "=") for f in FORBIDDEN_FLAGS)]
    if found:
        print(f"WARN command 带平台下发的参数 {' '.join(found)}；平台另行注入，交付的 command 只写 "
              "bash /opt/bench/start.sh", file=sys.stderr)


def stale_artifacts(workdir: str, env: dict[str, str]) -> list[str]:
    """检查 workdir 里上次冒烟留下的产物。

    Args:
        workdir: 本次冒烟的 --workdir。
        env: 平台口径的 EVAL_BA_* 默认值，用于取 OUTPUT_DIR。

    Returns:
        命中的产物路径列表：结果文件，以及非空的 OUTPUT_DIR、videos、logs；没有则返回空列表。
    """
    found = [name for name in _STALE_FILES if os.path.exists(os.path.join(workdir, name))]
    dirs = [env["EVAL_BA_OUTPUT_DIR"]] + [os.path.join(workdir, name) for name in _STALE_DIRS]
    found.extend(d for d in dirs if os.path.isdir(d) and os.listdir(d))
    return found


def _terminate(procs: list) -> None:
    """按进程组结束 shard。

    Args:
        procs: run_shards 起的子进程列表，每个都以 start_new_session 起、自成进程组。

    Returns:
        None。先 SIGTERM，等 TERMINATE_GRACE_SEC 后对仍在的组 SIGKILL。

    说明：
        shard 以 start_new_session 起，组号等于 shell 的 pid；shell 已退出时组里的子进程可能还在，所以不看 poll 一律发信号。
    """
    def signal_groups(sig: int) -> None:
        """把信号发给 procs 的每个进程组。

        Args:
            sig: 要发送的信号编号，如 signal.SIGTERM。

        Returns:
            None。组已不在或无权限的进程跳过。
        """
        for proc in procs:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass  # 组已不在，或 pid 已被复用到别的用户
    signal_groups(signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SEC
    while time.monotonic() < deadline and any(proc.poll() is None for proc in procs):
        time.sleep(0.1)
    signal_groups(signal.SIGKILL)
    for proc in procs:
        proc.wait()


def progress_path(env: dict[str, str], shard_id: int) -> str:
    """harness 给第 shard_id 个 shard 写的 .progress 路径。

    Args:
        env: 平台口径的 EVAL_BA_* 默认值。
        shard_id: shard 编号，从 0 开始。

    Returns:
        <OUTPUT_DIR>/<safe_name>_shard<i>of<N>.progress。
    """
    safe_name = _SAFE_NAME_RE.sub("_", env["EVAL_BA_BENCH_NAME"])
    name = f"{safe_name}_shard{shard_id}of{env['EVAL_BA_NUM_SHARDS']}.progress"
    return os.path.join(env["EVAL_BA_OUTPUT_DIR"], name)


def _read_progress(path: str) -> dict | None:
    """读一次 .progress。

    Args:
        path: progress_path 算出的路径。

    Returns:
        {"completed": int, "total": int, "errors": int}；文件不在或内容不是合法 JSON 对象时返回 None。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return {key: int(data.get(key, 0)) for key in ("completed", "total", "errors")}
    except (TypeError, ValueError):
        return None


def _poll_progress(seen: dict, env: dict[str, str], num_shards: int) -> None:
    """读一轮各 shard 的 .progress 并原地更新 seen。

    Args:
        seen: run_shards 维护的 {shard: {"path", "seen", "last"}}，函数直接改写它。
        env: 平台口径的 EVAL_BA_* 默认值。
        num_shards: shard 总数。

    Returns:
        None。读到的计数保留最后一次快照，读不到的不动。
    """
    for shard_id in range(num_shards):
        snap = _read_progress(progress_path(env, shard_id))
        if snap is not None:
            entry = seen[str(shard_id)]
            entry["seen"] += 1
            entry["last"] = snap


def run_shards(command: str, env: dict[str, str], workdir: str,
               user_envs: dict[str, str] | None = None) -> tuple[list[int], dict]:
    """并发起 NUM_SHARDS 个 shard 执行 command，运行期间轮询 .progress，等全部结束。

    Args:
        command: 交付给控制台的 command，原样执行。
        env: 平台口径的 EVAL_BA_* 默认值。
        workdir: 冒烟工作目录，日志写在 workdir/logs。
        user_envs: 控制台条目里的 envs；None 表示没有。

    Returns:
        (按 shard 顺序的退出码, {shard: {"path", "seen": 读到次数, "last": 最后一次快照或 None}})。

    Raises:
        ValueError: EVAL_BA_NUM_SHARDS 不是正整数。
    """
    num_shards = int(env["EVAL_BA_NUM_SHARDS"])
    if num_shards <= 0:
        raise ValueError(f"EVAL_BA_NUM_SHARDS 必须是正整数，收到 {num_shards}")
    log_dir = os.path.join(workdir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    seen = {str(i): {"path": progress_path(env, i), "seen": 0, "last": None} for i in range(num_shards)}
    procs = []
    logs = []
    try:
        for shard_id in range(num_shards):
            senv = shard_env(env, shard_id)
            os.makedirs(senv["EVAL_BA_VIDEO_DIR"], exist_ok=True)
            log = open(os.path.join(log_dir, f"shard{shard_id}.log"), "wb")
            logs.append(log)
            procs.append(subprocess.Popen(command, shell=True, env=_child_env(senv, user_envs), stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True))
        while any(proc.poll() is None for proc in procs):
            _poll_progress(seen, env, num_shards)
            time.sleep(PROGRESS_POLL_SEC)
        _poll_progress(seen, env, num_shards)
        return [proc.wait() for proc in procs], seen
    except BaseException:
        # Ctrl-C 等中断：shard 在独立 session 里收不到终端信号，不收拾会留下仿真器进程
        _terminate(procs)
        raise
    finally:
        for log in logs:
            log.close()


def main(argv: list[str] | None = None) -> int:
    """进程入口：写 smoke_env.json，起 N 个 shard 并记录 .progress。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        0 全部 shard 退出码为 0；1 有 shard 非 0；2 workdir 里有旧产物；130 被中断。

    Raises:
        ValueError: --set 格式非法或 NUM_SHARDS 非法。
    """
    args = parse_args(argv)
    workdir = os.path.abspath(args.workdir)
    env = base_env(workdir, args.server_url, args.bench_name)
    version = _harness_version()
    if version is not None:
        env["EVAL_BA_HARNESS_VERSION"] = version
    env = apply_overrides(env, args.set)
    user_envs = parse_user_envs(args.env)
    stale = stale_artifacts(workdir, env)
    if stale:
        print(f"ERROR {workdir} 里有上次冒烟的产物（{', '.join(stale)}），自检会读到旧数据；"
              "旧产物不删，换新目录重跑：--workdir $W/smoke-<序号>", file=sys.stderr)
        return 2
    os.makedirs(env["EVAL_BA_OUTPUT_DIR"], exist_ok=True)
    with open(os.path.join(workdir, "smoke_env.json"), "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=2)
    # 图形栈变量单独落盘供排查渲染设备；不进 smoke_env.json，那份只给 ba_selfcheck 读 EVAL_BA_*
    child = _child_env(shard_env(env, 0), user_envs)
    with open(os.path.join(workdir, "graphics_env.json"), "w", encoding="utf-8") as f:
        json.dump({k: child.get(k) for k in GRAPHICS_KEYS}, f, ensure_ascii=False, indent=2)
    _warn_shell(args.command)
    _warn_flags(args.command)
    try:
        codes, seen = run_shards(args.command, env, workdir, user_envs)
    except KeyboardInterrupt:
        print(f"SMOKE: INTERRUPTED；shard 已结束，{workdir} 的产物作废，换新目录重跑", file=sys.stderr)
        return 130
    with open(os.path.join(workdir, "progress_seen.json"), "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    with open(os.path.join(workdir, "smoke_run.json"), "w", encoding="utf-8") as f:
        json.dump({"command": args.command, "exit_codes": codes}, f, ensure_ascii=False, indent=2)
    missing = sorted(shard for shard, entry in seen.items() if entry["seen"] == 0)
    if missing:
        print(f"WARN shard {','.join(missing)} 运行期间没读到 .progress，自检的 progress 项会失败", file=sys.stderr)
    for shard_id, code in enumerate(codes):
        print(f"shard{shard_id} exit={code} log={os.path.join(workdir, 'logs', f'shard{shard_id}.log')}")
    ok = all(code == 0 for code in codes)
    print(f"SMOKE: {'PASS' if ok else 'FAIL'}；下一步：python ba_selfcheck.py "
          f"--db {env['EVAL_BA_OUTPUT_DIR']}/recording-{env['EVAL_BA_EVAL_ID']}.sqlite "
          f"--env-file {os.path.join(workdir, 'smoke_env.json')} "
          f"--control --out {os.path.join(workdir, 'selfcheck.json')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
