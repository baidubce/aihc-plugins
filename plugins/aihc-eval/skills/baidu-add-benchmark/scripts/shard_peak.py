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
"""冒烟期间采样每个 shard 的内存、CPU 峰值，以及 /dev/shm 与显存的增量峰值，换算建议资源。

smoke_run.py 用 start_new_session 起 shard，平台同样如此，所以一个 shard 就是一个 session：cmdline 含 ``--match``
的进程所在 session 记为一个 shard，session 内全部进程的 PSS、CPU 时间求和。/dev/shm 与显存是 N 个 shard 共用的，
只能取「峰值 − 冒烟前基线」再按 shard 数均摊。只读 /proc、cgroup 与 nvidia-smi，不依赖 psutil，兼容 Python 3.8。

内存建议是 Pod 级：平台按 Pod 的 cgroup 限内存，计入的不只是 shard 进程，还有 Ray 常驻进程、每个 shard 一个
BenchmarkActor、内存型 /dev/shm 与文件页缓存。单 shard 取「session PSS + 人均 shm」与「cgroup 工作集增量 / shard 数」
的较大者，再加 Ray 开销、乘余量（冒烟只跑少量 episode，正式评测长跑会涨）：
    内存 = (N × 单 shard + Ray 常驻 + N × Actor) × 余量
CPU = 2N，shm = N × 单 shard 峰值，每卡显存 = N / G × 单 shard 峰值。
单 shard CPU 峰值超过 2 核、shm 低于控制面显式值下限、N 不能整除卡数、读不到 cgroup 时写进 suggest_notes。

用法（先于 smoke_run.py 在后台起，shard 全部退出后自动结束）：
    python shard_peak.py --out <W>/peak.json [--match "vla-eval run"] [--interval 1]
        [--plan-shards N] [--plan-gpus G]

退出码：采到 shard 返回 0；等待 --wait 秒仍没出现 shard 返回 1。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time

_CLK_TCK = os.sysconf("SC_CLK_TCK")
_MIB = 1024 * 1024
# 平台设计值：每个 shard 2 核
_CPU_PER_SHARD = 2
# 平台拒绝显式填的 sharedMemory 低于 10 GiB（不填按缺省 10 GiB），只用于提示
_SHM_GIB_EXPLICIT_MIN = 10
# 冒烟不经 Ray，以下两项测不到，按经验值补：raylet、dashboard agent 等常驻进程；每个 shard 一个 BenchmarkActor 进程
_RAY_BASE_MIB = 2048
_ACTOR_MIB = 512
# 冒烟只跑少量 episode，正式评测每个 shard 连跑多个 episode，内存会随视频缓冲、仿真资产缓存增长
_MEM_MARGIN = 1.5
# cgroup v2 / v1 的用量与非活跃文件页；用量减非活跃文件页即工作集，与 kubelet 口径一致
_CGROUP_FILES = (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
                 ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat",
                  "total_inactive_file"))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        argparse.Namespace，含 out、match、interval、wait、shm、plan_shards、plan_gpus。

    Raises:
        SystemExit: 缺少 --out 等参数非法时 argparse 以退出码 2 结束。
    """
    parser = argparse.ArgumentParser(description="采样 shard 资源峰值")
    parser.add_argument("--out", required=True, help="结果 JSON 路径")
    parser.add_argument("--match", default="vla-eval run", help="cmdline 含此串的进程所在 session 记为一个 shard")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔，秒")
    parser.add_argument("--wait", type=float, default=600.0, help="等 shard 出现的上限，秒")
    parser.add_argument("--shm", default="/dev/shm", help="共享内存挂载点")
    parser.add_argument("--plan-shards", type=int, help="正式评测的 shard 数，缺省取采到的 shard 数")
    parser.add_argument("--plan-gpus", type=int, default=1, help="正式评测 BA 的卡数")
    return parser.parse_args(argv)


def _read(path: str) -> str | None:
    """读小文件。

    Args:
        path: 文件路径，通常是 /proc 或 cgroup 下的伪文件。

    Returns:
        文件内容（UTF-8，非法字节替换）；进程已退出或无权限时返回 None。
    """
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _stat_fields(pid: int) -> list[str] | None:
    """取 /proc/<pid>/stat 中 comm 之后的字段。

    Args:
        pid: 进程号。

    Returns:
        字段列表，下标 0 是 state，3 是 session，11/12 是 utime/stime；进程不存在或格式异常时返回 None。
    """
    raw = _read(f"/proc/{pid}/stat")
    if raw is None or ")" not in raw:
        return None
    return raw.rsplit(")", 1)[1].split()


def _mem_kib(pid: int) -> tuple[int, int]:
    """读进程内存占用。

    Args:
        pid: 进程号。

    Returns:
        (PSS, RSS)，单位 KiB；smaps_rollup 不可读时 PSS 退回 RSS。
    """
    rss = 0
    for line in (_read(f"/proc/{pid}/status") or "").splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1])
    pss = None
    for line in (_read(f"/proc/{pid}/smaps_rollup") or "").splitlines():
        if line.startswith("Pss:"):
            pss = int(line.split()[1])
    return (rss if pss is None else pss), rss


def _shard_label(pid: int) -> str:
    """从 session leader 的 environ 取 shard 标识。

    Args:
        pid: session leader 的进程号。

    Returns:
        EVAL_BA_SHARD_ID 的值；取不到时返回 "sid<pid>"。只读该键，不读其它环境变量。
    """
    for item in (_read(f"/proc/{pid}/environ") or "").split("\0"):
        if item.startswith("EVAL_BA_SHARD_ID="):
            return item.split("=", 1)[1]
    return f"sid{pid}"


def scan(match: str) -> dict[int, list[int]]:
    """扫描存活的 session，找出含匹配进程的那些。

    Args:
        match: cmdline 需包含的字符串，命中即认为该 session 是一个 shard。

    Returns:
        {sid: [该 session 内全部 pid]}，只保留含匹配进程的 session。
    """
    sessions = {}
    hits = set()
    me = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        fields = _stat_fields(pid)
        if fields is None or fields[0] == "Z":
            continue
        sid = int(fields[3])
        sessions.setdefault(sid, []).append(pid)
        cmd = (_read(f"/proc/{pid}/cmdline") or "").replace("\0", " ")
        if match in cmd:
            hits.add(sid)
    return {sid: pids for sid, pids in sessions.items() if sid in hits}


def gpu_used_mib() -> dict[str, int]:
    """每张卡的 memory.used（MiB）；没有 nvidia-smi 返回空。"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    used = {}
    for line in out.decode().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[1].isdigit():
            used[parts[0]] = int(parts[1])
    return used


def cgroup_working_set() -> int | None:
    """本容器 cgroup 的内存工作集（字节）；cgroup v2 / v1 都读不到返回 None。"""
    for usage_path, stat_path, inactive_key in _CGROUP_FILES:
        usage = (_read(usage_path) or "").strip()
        if not usage.isdigit():
            continue
        inactive = 0
        for line in (_read(stat_path) or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == inactive_key and parts[1].isdigit():
                inactive = int(parts[1])
        return max(0, int(usage) - inactive)
    return None


def sample_loop(args: argparse.Namespace) -> dict | None:
    """持续采样，直到全部 shard 退出。

    Args:
        args: parse_args 的返回值，提供 match、interval、wait、shm 与建议资源的规划参数。

    Returns:
        summarize 的结果字典；--wait 内始终没出现 shard 时返回 None。
    """
    shm_base = shutil.disk_usage(args.shm).used
    gpu_base = gpu_used_mib()
    cg_base = cgroup_working_set()
    cg_peak = None if cg_base is None else 0
    shards = {}
    prev_ticks = {}
    shm_peak = 0
    gpu_peak = {}
    start = time.time()
    last = start
    gone = 0
    samples = 0
    while True:
        time.sleep(args.interval)
        now = time.time()
        live = scan(args.match)
        if not live:
            if not shards and now - start > args.wait:
                return None
            gone = gone + 1 if shards else 0
            if gone >= 2:
                break
            continue
        gone = 0
        samples += 1
        for sid, pids in live.items():
            rec = shards.setdefault(sid, {"label": _shard_label(sid), "pss": 0, "rss": 0, "cpu": 0.0, "procs": 0})
            pss = rss = ticks = 0
            for pid in pids:
                fields = _stat_fields(pid)
                if fields is None:
                    continue
                # 只计 utime + stime：cutime 在子进程被回收的那一刻整段并入父进程，会造成假尖峰
                ticks += int(fields[11]) + int(fields[12])
                p, r = _mem_kib(pid)
                pss += p
                rss += r
            if sid in prev_ticks and now > last:
                rec["cpu"] = max(rec["cpu"], max(0, ticks - prev_ticks[sid]) / _CLK_TCK / (now - last))
            prev_ticks[sid] = ticks
            rec["pss"] = max(rec["pss"], pss)
            rec["rss"] = max(rec["rss"], rss)
            rec["procs"] = max(rec["procs"], len(pids))
        shm_peak = max(shm_peak, shutil.disk_usage(args.shm).used - shm_base)
        for idx, used in gpu_used_mib().items():
            gpu_peak[idx] = max(gpu_peak.get(idx, 0), used - gpu_base.get(idx, 0))
        cg_now = cgroup_working_set()
        if cg_peak is not None and cg_now is not None:
            cg_peak = max(cg_peak, cg_now - cg_base)
        last = now
    meta = {"samples": samples, "duration_s": round(time.time() - start)}
    return summarize(args, shards, shm_peak, gpu_peak, meta, cgroup_peak=cg_peak)


def summarize(args: argparse.Namespace, shards: dict, shm_peak: int, gpu_peak: dict, meta: dict,
              cgroup_peak: int | None = None) -> dict:
    """把实测峰值换算成 N 个 shard 的建议资源。

    Args:
        args: parse_args 的返回值，提供 plan_shards 与 plan_gpus。
        shards: {sid: 单 shard 峰值记录}。
        shm_peak: /dev/shm 用量增量峰值（字节）。
        gpu_peak: {卡序号: 显存增量峰值 MiB}。
        meta: 采样元信息（样本数、时长），原样并入结果。
        cgroup_peak: 容器 cgroup 工作集增量峰值（字节）；读不到时为 None。

    Returns:
        结果字典：per_shard、per_shard_estimate、suggest、suggest_notes 等。
    """
    n_seen = len(shards)
    per = {rec["label"]: {"peak_pss_mib": rec["pss"] // 1024, "peak_rss_mib": rec["rss"] // 1024,
                          "peak_cpu_cores": round(rec["cpu"], 2), "peak_procs": rec["procs"]}
           for rec in shards.values()}
    mem_mib = max(v["peak_pss_mib"] for v in per.values())
    cpu_peak = max(v["peak_cpu_cores"] for v in per.values())
    shm_mib = shm_peak / _MIB / n_seen
    gpu_mib = sum(max(0, v) for v in gpu_peak.values()) / n_seen
    # 内存型 /dev/shm 计入 Pod 内存；cgroup 工作集还含 shard 进程之外的页缓存与短命子进程，取两者较大者
    pod_mem_mib = mem_mib + shm_mib
    cg_mib = None if cgroup_peak is None else cgroup_peak / _MIB / n_seen
    if cg_mib is not None:
        pod_mem_mib = max(pod_mem_mib, cg_mib)
    plan_n = args.plan_shards or n_seen
    plan_g = max(1, args.plan_gpus)
    shm_gib = math.ceil(plan_n * shm_mib / 1024 * 10) / 10
    pod_total_mib = plan_n * (pod_mem_mib + _ACTOR_MIB) + _RAY_BASE_MIB
    suggest = {
        "shards": plan_n, "gpus": plan_g,
        "cpu": plan_n * _CPU_PER_SHARD,
        "memory_gib": math.ceil(pod_total_mib * _MEM_MARGIN / 1024),
        "shm_gib": shm_gib,
        "gpu_mem_per_card_mib": math.ceil(math.ceil(plan_n / plan_g) * gpu_mib),
    }
    notes = []
    if cpu_peak > _CPU_PER_SHARD:
        notes.append(f"单 shard CPU 峰值 {cpu_peak} 核高于 {_CPU_PER_SHARD} 核，按 2N 给出时 shard 间会争抢 CPU")
    if shm_gib < _SHM_GIB_EXPLICIT_MIN:
        notes.append(f"shm 换算 {shm_gib} GiB，控制面拒绝显式填低于 {_SHM_GIB_EXPLICIT_MIN} GiB 的值，"
                     f"控制台 sharedMemory 留空即按缺省 {_SHM_GIB_EXPLICIT_MIN} GiB")
    if plan_g >= 2 and plan_n % plan_g != 0:
        notes.append(f"BA 卡数 {plan_g} ≥ 2 时 shard 数须整除卡数，{plan_n} % {plan_g} != 0 会被控制面拒绝")
    if cg_mib is None:
        notes.append("读不到容器 cgroup 内存用量，内存只按 shard 进程 PSS 与 shm 估算，页缓存等未计入，可能偏小")
    return dict(meta, shards_seen=n_seen, per_shard=per, shm_peak_delta_mib=round(shm_peak / _MIB),
                gpu_peak_delta_mib=gpu_peak,
                cgroup_peak_delta_mib=None if cgroup_peak is None else round(cgroup_peak / _MIB),
                per_shard_estimate={"mem_mib": mem_mib, "cpu_cores": cpu_peak, "shm_mib": round(shm_mib),
                                    "gpu_mib": round(gpu_mib), "pod_mem_mib": round(pod_mem_mib)},
                memory_basis={"ray_base_mib": _RAY_BASE_MIB, "actor_mib": _ACTOR_MIB, "margin": _MEM_MARGIN},
                suggest=suggest, suggest_notes=notes)


def main(argv: list[str] | None = None) -> int:
    """进程入口：采样、把结果写 --out 并打印摘要。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        进程退出码：0 采到 shard，1 等待 --wait 秒仍没出现 shard。
    """
    args = parse_args(argv)
    result = sample_loop(args)
    if result is None:
        print(f"PEAK: 等了 {args.wait:.0f}s 没有 cmdline 含 {args.match!r} 的进程", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    est = result["per_shard_estimate"]
    print(f"PEAK: shards={result['shards_seen']} 单 shard 内存 {est['mem_mib']} MiB（含 shm、页缓存按 Pod 计 "
          f"{est['pod_mem_mib']} MiB）、CPU {est['cpu_cores']} 核、shm {est['shm_mib']} MiB、显存 {est['gpu_mib']} MiB")
    print(f"SUGGEST: {json.dumps(result['suggest'], ensure_ascii=False)}；详见 {args.out}")
    for note in result["suggest_notes"]:
        print(f"NOTE: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
