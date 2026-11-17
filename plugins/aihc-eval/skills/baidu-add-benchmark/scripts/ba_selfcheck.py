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
"""自定义 benchmark 镜像的本机自检：在 smoke_run.py 的冒烟产物上判断镜像是否达到最低运行标准。

覆盖 M2、M4～M12，M3 只查版本（读 ray_env_check.sh 的输出）。M5 与平台运行后核对
（ba_checks.check_recording）同一口径；另查 command 字面量与平台参数（--benchmark-field、server.timeout）、退出码、
实时模式（shard 日志里的 get_hold_action 报错）、.progress、结果格式、共享队列、视频、渲染、max_steps 与对照实验。
任一项不过不交付（G4），没有跳过开关。

用法：
    python ba_selfcheck.py --db <W>/out/recording-<eval_id>.sqlite --env-file <W>/smoke_env.json
        [--smoke-file <W>/smoke_run.json] [--ray-env-log <W>/ray_env_check.log]
        [--progress-file <W>/progress_seen.json] [--shards 0,1]
        [--control | --control-db <零动作 DB>] [--oracle-db <oracle DB>] [--out report.json]
    python ba_selfcheck.py --list

--smoke-file / --ray-env-log / --progress-file 缺省取 --env-file 同目录。--control 把 --db（echo MS 的零动作冒烟）
当零动作对照；对照只能证伪、不能证明。

退出码：0 全部通过；1 有检查不过；2 DB 读不了或 env 缺键（自检本身没跑起来）。
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ba_checks  # noqa: E402  与本 skill 附带的 scripts/ba_checks.py 逐字节一致

# 一项检查：m 为检查项编号，hint 为不过时的排查方法，actual 为可 JSON 序列化的实测值
Check = collections.namedtuple("Check", ["name", "passed", "m", "hint", "actual"])

# 检查名 → (编号, 排查方法)。param_match.<子项> 优先用 PARAM_HINTS，没有再退回 param_match
HINTS = {
    "command_literal": ("M2", "command 或其调用的脚本把平台下发的参数写成了字面量：--server-url / --shard-id / "
                              "--num-shards / --eval-id / --output-dir / --render 及 name、episodes_per_task、mode、"
                              "recording.output_dir、suite 只能取 $EVAL_BA_*；command 本身只写 bash /opt/bench/start.sh"),
    "ray_version": ("M3", "镜像 PATH 上第一个 ray 不是 2.49.2 / Python 3.13.12：先跑 ray_env_check.sh 并把输出存成 "
                          "ray_env_check.log；非 base 镜像按 references/Ray环境.md 补装"),
    "exit_code": ("M4", "有 shard 非 0 退出：看 smoke_run.py 打出的 shard<i>.log，常见是 config 类 import 失败、"
                        "未知 task / suite、端口冲突"),
    "db": ("M4", "没找到或读不了 recording DB：确认 start.sh 没改写 --output-dir、--eval-id 用的是 "
                 "$EVAL_BA_EVAL_ID，DB 应在 $EVAL_BA_OUTPUT_DIR/recording-<eval_id>.sqlite"),
    "progress": ("M5", "shard 运行期间没读到 .progress（平台靠它报进度）：确认 start.sh 没改写 --output-dir、"
                       "name 用的是 $EVAL_BA_BENCH_NAME、--shard-id / --num-shards 已传；"
                       "progress_seen.json 须是 smoke_run.py 本次冒烟写出的"),
    "param_match": ("M5", "harness 实际生效的参数与平台下发的 EVAL_BA_* 不一致：对照 references/命令契约.md "
                          "的 start.sh 模板逐个核对 --benchmark-field"),
    "result_has_success": ("M7", "每个 episode 的结果必须带 bool 型 success（int 1 不算），且不得以异常结束："
                                 "改 get_step_result / get_result，episode 报错看 failure_detail"),
    "metric_keys": ("M7", "get_metric_keys() 须保留 {'success': 'mean'}，且所有 episode 有 bool success，"
                          "否则平台成功率聚合静默算错"),
    "shared_queue": ("M8", "2 个 shard 须共用 work_queue、episode 不重不漏：确认 start.sh 不自己循环 shard、"
                           "不改写 OUTPUT_DIR / EVAL_ID，端口与临时文件按 shard 隔离"),
    "video_count": ("M9", "视频数少于 episode 数：确认每步调用了 self._recorder.record_video(frame)，"
                          "--record-video 随 EVAL_BA_RECORD_VIDEO 下发"),
    "video_decodable": ("M9", "视频解码失败、0 帧或帧数与 steps 差太多：确认每步录一帧、进程正常退出让 "
                              "recorder 收尾（被 kill 会留下坏 mp4）"),
    "render_device": ("M10", "GPU 渲染时不得落到 lavapipe / llvmpipe 软渲染，画面不得全黑：见 "
                             "references/渲染与资产.md，钉 NVIDIA Vulkan ICD、检查相机与光照"),
    "live_hold_action": ("M5", "shard 日志里有 harness 拒跑实时模式的报错：没实现 get_hold_action 的，按 "
                               "references/适配器指南.md「实时模式」补（绝对控制用 base.repeat_last_hold，增量控制返回"
                               "固定零动作）；VectorStepBenchmark 只支持 sync，交付说明里写明「不支持实时模式」"),
    "max_steps": ("M11", "eval.yaml 须显式写正整数 max_steps，episode 步数不得超过它（平台据此算超时数）"),
    "control_zero_action": ("M12", "零动作也能成功：success 判定可能恒真或读错状态，回到原始代码 / 论文核对"),
    "control_oracle": ("M12", "oracle 回放全部失败：success 判定可能恒假，或 reset / 动作语义与原实现不一致"),
}

PARAM_HINTS = {
    "episodes_per_task": ("M5", "episodes_per_task 不符：start.sh 须传 --benchmark-field "
                                "\"episodes_per_task=$EVAL_BA_EPISODES_PER_TASK\"，eval.yaml 里别再写死"),
    "harness_version": ("M6", "镜像内 vla-eval 版本与平台不同 major.minor：pip install vla-eval==<平台版本>"),
    "single_entry": ("M5", "eval_metadata 须恰好一行且 eval_id 为 <EVAL_ID>-<BENCH_NAME>：eval.yaml 只留一个"
                           " benchmark 条目，并传 name=$EVAL_BA_BENCH_NAME、subname="),
    "shard": ("M5", "eval_shards 缺本 shard 或未 complete：确认传了 --shard-id / --num-shards，进程跑完再退出"),
    "record_step": ("M5", "step_rows 非空：start.sh 须传 recording.record_step=false"),
    "render": ("M5", "render.requested 与 EVAL_BA_RENDER 不符：start.sh 须传 --render \"$EVAL_BA_RENDER\""),
    "recording": ("M5", "录屏块与平台口径不符：output_dir / filename_stem / video_fps 照 start.sh 模板传"),
    "mode": ("M5", "评测模式不符：start.sh 须传 mode=$EVAL_BA_MODE"),
    "server_timeout": ("M5", "超时秒数不符：eval.yaml 的 server.timeout 须写 ${oc.env:EVAL_BA_SERVER_TIMEOUT}"),
    "env": ("M5", "EVAL_BA_SHARD_ID / EVAL_BA_NUM_SHARDS 须是整数"),
    "db": ("M5", "recording DB 结构或元数据不符合平台口径：确认 vla-eval 版本与平台一致"),
}

# 平台经 EVAL_BA_* 下发、command 不得写死的参数（与平台校验规则一致）
FORBIDDEN_FLAGS = ("--server-url", "--shard-id", "--num-shards", "--eval-id", "--output-dir", "--render")
# command 本身也不得带：录屏开关随 EVAL_BA_RECORD_VIDEO 在 start.sh 里切换
COMMAND_ONLY_FLAGS = ("--record-video", "--no-record-video")
# --benchmark-field 里必须取 EVAL_BA_* 的键
PLATFORM_FIELDS = ("name", "episodes_per_task", "mode", "recording.output_dir")
RAY_ENV_OK = "RAY_ENV_OK"
# eval.yaml 须这样引用超时（不带默认值），否则平台下发的 serverTimeout 不生效
_TIMEOUT_REF_RE = re.compile(r"\$\{oc\.env:EVAL_BA_SERVER_TIMEOUT\}")
# harness v0.9.0 拒跑实时模式时的报错片段（orchestrator / runner），冒烟默认 sync 不会触发，需另跑一次 live
LIVE_MODE_ERRORS = ("does not implement get_hold_action", "runs in sync mode only")

REQUIRED_ENV = ("EVAL_BA_EVAL_ID", "EVAL_BA_BENCH_NAME", "EVAL_BA_NUM_SHARDS", "EVAL_BA_EPISODES_PER_TASK",
                "EVAL_BA_MODE", "EVAL_BA_RENDER", "EVAL_BA_RECORD_VIDEO", "EVAL_BA_VIDEO_DIR",
                "EVAL_BA_SERVER_TIMEOUT")

# applied_env 的值含这些片段即视为软渲染
_SOFT_RENDER_PARTS = ("lvp", "lavapipe", "llvmpipe", "swrast", "softpipe")
_VIDEO_NAME_RE = re.compile(r"^task(\d+)_ep(\d+)\.mp4$")
CONTROL_ZERO_MAX_RATE = 0.1


class SelfcheckError(Exception):
    """自检本身跑不起来（DB 读不了、env 缺键），对应退出码 2。"""


def _check(name: str, passed: bool, actual: object) -> Check:
    """按检查名填编号与排查方法。

    Args:
        name: 检查名，前缀相同的项共用排查方法。
        passed: 本项是否通过。
        actual: 实测值，原样写进报告。

    Returns:
        Check 具名元组。
    """
    head, _, sub = name.partition(".")
    m, hint = PARAM_HINTS.get(sub) or HINTS[head]
    return Check(name, bool(passed), m, hint, actual)


def shard_env(env: dict[str, str], shard_id: int) -> dict[str, str]:
    """构造平台给第 shard_id 个 shard 的 env。

    Args:
        env: smoke_env.json 的 EVAL_BA_* 内容。
        shard_id: shard 编号，从 0 开始。

    Returns:
        新 dict：EVAL_BA_SHARD_ID 已设置，VIDEO_DIR 里的 {shard_id} 已替换。
    """
    out = dict(env)
    out["EVAL_BA_SHARD_ID"] = str(shard_id)
    if out.get("EVAL_BA_VIDEO_DIR"):
        out["EVAL_BA_VIDEO_DIR"] = out["EVAL_BA_VIDEO_DIR"].replace("{shard_id}", str(shard_id))
    return out


@contextlib.contextmanager
def _snapshot(db_path: str):
    """用 backup API 取 recording DB 的一致快照（DB 开了 WAL）。

    Args:
        db_path: recording DB 路径。

    Yields:
        (快照连接, 快照字节)，退出 with 后连接关闭。

    Raises:
        SelfcheckError: DB 不存在或不是可读的 SQLite 库。
    """
    if not os.path.isfile(db_path):
        raise SelfcheckError(f"recording DB 不存在：{db_path}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "snapshot.sqlite")
        try:
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            with open(copy, "rb") as f:
                data = f.read()
            conn = sqlite3.connect(copy)
        except sqlite3.Error as exc:
            raise SelfcheckError(f"读不了 recording DB {db_path}：{exc}") from exc
        try:
            yield conn, data
        finally:
            conn.close()


def _load_metrics(raw: object) -> dict:
    """把 metrics 列解析成 dict。

    Args:
        raw: episode_results.metrics 列的原始值。

    Returns:
        解析出的 dict；值不是 JSON 字符串或解析失败时返回空 dict。
    """
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {}


# 一个 episode 结果行（同一 (task_idx, episode_id) 有多行时取 rowid 最大的）
Episode = collections.namedtuple("Episode", ["task_idx", "episode_id", "steps", "elapsed_sec", "metrics",
                                             "failure_reason", "failure_detail"])


def _read_episodes(conn: sqlite3.Connection, bench_id: str) -> tuple[dict, list]:
    """读本 bench 的 episode 行。

    Args:
        conn: 快照连接。
        bench_id: 本 benchmark 的 eval_id。

    Returns:
        ({(task_idx, episode_id): Episode}, 出现多行的 key 列表)；同一 key 有多行时取 rowid 最大的。
    """
    rows = conn.execute(
        "SELECT rowid, json_extract(context, '$.task_idx'), episode_id, steps, elapsed_sec, metrics,"
        " failure_reason, failure_detail FROM episode_results WHERE eval_id = ? ORDER BY rowid",
        (bench_id,)).fetchall()
    episodes: dict = {}
    duplicated = []
    for _, task_idx, episode_id, steps, elapsed, metrics, reason, detail in rows:
        key = (task_idx, episode_id)
        if key in episodes:
            duplicated.append(key)
        episodes[key] = Episode(task_idx, episode_id, steps, elapsed, _load_metrics(metrics), reason, detail)
    return episodes, duplicated


def _param_checks(db_bytes: bytes, env: dict[str, str], shards: list[int]) -> tuple[list, dict]:
    """跑 M5、M6：逐 shard 做平台同款核对。

    Args:
        db_bytes: 快照字节，转交给 ba_checks.check_recording。
        env: smoke_env.json 的 EVAL_BA_* 内容。
        shards: 参与冒烟的 shard 编号。

    Returns:
        (检查列表, {shard: CheckReport})。
    """
    checks = []
    reports = {}
    for shard in shards:
        report = ba_checks.check_recording(db_bytes, shard_env(env, shard), require_complete=True)
        reports[shard] = report
        for m in report.mismatches:
            checks.append(_check(f"param_match.{m.check}", False,
                                 {"shard": shard, "expected": m.expected, "actual": m.actual}))
    if not checks:
        checks.append(_check("param_match", True, {"shards": shards}))
    return checks, reports


def _result_checks(episodes: dict, meta: dict) -> list:
    """跑 M7：每个 episode 都有 bool 型 success 且没报错，metric_keys 声明 success 按 mean 聚合。

    Args:
        episodes: _read_episodes 返回的 episode 字典。
        meta: harness_config（含 metric_keys）。

    Returns:
        检查列表。
    """
    invalid = [list(key) for key, ep in episodes.items()
               if not ep.failure_reason and not isinstance(ep.metrics.get("success"), bool)]
    errored = [{"episode": list(key), "reason": ep.failure_reason, "detail": (ep.failure_detail or "")[:200]}
               for key, ep in episodes.items() if ep.failure_reason]
    has_success = bool(episodes) and not invalid and not errored
    actual = {"episodes": len(episodes), "missing_bool_success": invalid[:10], "errored": errored[:5]}
    metric_keys = meta.get("metric_keys")
    declared = isinstance(metric_keys, dict) and metric_keys.get("success") == "mean"
    return [_check("result_has_success", has_success, actual),
            _check("metric_keys", declared and bool(episodes) and not invalid,
                   {"metric_keys": metric_keys, "missing_bool_success": len(invalid)})]


def _queue_check(conn: sqlite3.Connection, bench_id: str, episodes: dict, duplicated: list,
                 shards: list[int]) -> Check:
    """跑 M8：≥2 个 shard 共用 work_queue，每个 item 都 done 且恰有一条 episode 行。

    Args:
        conn: 快照连接。
        bench_id: 本 benchmark 的 eval_id。
        episodes: _read_episodes 返回的 episode 字典。
        duplicated: episode 行重复的 key 列表。
        shards: 参与冒烟的 shard 编号。

    Returns:
        shared_queue 这一项检查结果。
    """
    if len(shards) < 2:
        return _check("shared_queue", False, {"shards": shards, "reason": "冒烟须至少 2 个 shard 并发"})
    if not ba_checks.has_work_queue(conn, bench_id):
        return _check("shared_queue", False, {"work_queue": None, "reason": "没有走 harness 动态队列"})
    rows = conn.execute("SELECT item, task_idx, shard_id, done FROM work_queue WHERE eval_id = ? ORDER BY item",
                        (bench_id,)).fetchall()
    first_item: dict = {}
    for item, task_idx, _, _ in rows:
        first_item.setdefault(task_idx, item)
    covered = {first_item[t] + e for t, e in episodes if t in first_item and isinstance(e, int)}
    missing = [item for item, _, _, _ in rows if item not in covered]
    not_done = [item for item, _, _, done in rows if done != 1]
    by_shard: dict = {}
    for _, _, shard_id, done in rows:
        if done == 1:
            by_shard[str(shard_id)] = by_shard.get(str(shard_id), 0) + 1
    actual = {"items": len(rows), "missing": missing, "not_done": not_done,
              "duplicated": [list(key) for key in duplicated], "done_by_shard": by_shard}
    return _check("shared_queue", not missing and not not_done and not duplicated, actual)


def _list_videos(directory: str) -> list[str]:
    """列出目录下平台命名的 mp4。

    Args:
        directory: 某个 shard 的视频目录。

    Returns:
        按路径排序的 mp4 列表，不含 recorder 写到一半的临时文件；目录不存在时为空列表。
    """
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, name) for name in os.listdir(directory) if _VIDEO_NAME_RE.match(name))


def _default_reader(path: str) -> tuple[int, int]:
    """逐帧解码 mp4。

    Args:
        path: 视频文件路径。

    Returns:
        (帧数, 最大像素值)。

    Raises:
        SelfcheckError: 本机没装 imageio。
    """
    try:
        import imageio.v3 as iio
    except ImportError as exc:
        raise SelfcheckError("解码视频需要 imageio：pip install 'imageio[ffmpeg]'") from exc
    frames = 0
    max_pixel = 0
    for frame in iio.imiter(path):
        frames += 1
        max_pixel = max(max_pixel, int(frame.max()))
    return frames, max_pixel


def _frame_tolerance(steps: int) -> int:
    """算帧数与 steps 允许的差。

    Args:
        steps: 该 episode 的步数。

    Returns:
        允许的帧数偏差，取 max(2, steps 的 5%)；recorder 可能多录 reset 后的首帧。
    """
    return max(2, int(steps * 0.05))


def _video_checks(env: dict[str, str], shards: list[int], reports: dict, episodes: dict,
                  reader) -> tuple[list, list]:
    """跑 M9：视频数不少于 episode 数、可解码、帧数 ≈ steps。

    Args:
        env: smoke_env.json 的 EVAL_BA_* 内容。
        shards: 参与冒烟的 shard 编号。
        reports: {shard: CheckReport}。
        episodes: _read_episodes 返回的 episode 字典。
        reader: 视频解码函数 path -> (帧数, 最大像素)。

    Returns:
        (检查列表, 各视频的最大像素列表)。

    Raises:
        SelfcheckError: reader 是默认实现且本机没装 imageio。
    """
    missing_by_shard = {}
    broken = []
    max_pixels = []
    for shard in shards:
        senv = shard_env(env, shard)
        videos = _list_videos(senv["EVAL_BA_VIDEO_DIR"])
        missing = ba_checks.missing_videos(senv, reports[shard].episodes, len(videos))
        if missing > 0:
            missing_by_shard[str(shard)] = missing
        for path in videos:
            name = os.path.basename(path)
            match = _VIDEO_NAME_RE.match(name)
            episode = episodes.get((int(match.group(1)), int(match.group(2))))
            try:
                frames, max_pixel = reader(path)
            except SelfcheckError:
                raise
            except Exception as exc:  # noqa: BLE001  解码器抛什么都算坏视频
                broken.append({"file": name, "reason": f"解码失败：{exc}"})
                continue
            max_pixels.append(max_pixel)
            steps = episode.steps if episode is not None else None
            if frames == 0:
                broken.append({"file": name, "reason": "0 帧"})
            elif not isinstance(steps, int):
                broken.append({"file": name, "reason": "找不到对应 episode 的 steps"})
            elif abs(frames - steps) > _frame_tolerance(steps):
                broken.append({"file": name, "reason": f"帧数 {frames} 与 steps {steps} 差太多"})
    # 0 个 episode 时视频数「相等」没有意义，按不过处理（result_has_success 同样不过）
    checks = [_check("video_count", bool(episodes) and not missing_by_shard,
                     {"episodes": len(episodes), "missing_by_shard": missing_by_shard}),
              _check("video_decodable", not broken, broken)]
    return checks, max_pixels


def _render_check(env: dict[str, str], meta: dict, max_pixels: list) -> Check:
    """跑 M10：GPU 渲染时 applied_env 不得指向软渲染、画面不得全黑。

    Args:
        env: smoke_env.json 的 EVAL_BA_* 内容。
        meta: harness_config（含 render.applied_env）。
        max_pixels: 各视频的最大像素值；空列表表示没有可解码视频。

    Returns:
        render_device 这一项检查结果；CPU 渲染不查软渲染与全黑。
    """
    requested = (env.get("EVAL_BA_RENDER") or "gpu").strip().lower()
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    applied = render.get("applied_env") if isinstance(render.get("applied_env"), dict) else {}
    if requested != "gpu":
        return _check("render_device", True, {"render": requested, "applied_env": applied})
    soft = sorted(key for key, value in applied.items()
                  if any(part in str(value).lower() for part in _SOFT_RENDER_PARTS))
    if env.get("EVAL_BA_RECORD_VIDEO") != "true":
        all_black = "未录屏，未检查画面"
    elif not max_pixels:
        # 录了屏却没有一个可解码视频，画面无从核对，不能当成通过
        all_black = "无可解码视频，未检查画面"
    else:
        all_black = all(p == 0 for p in max_pixels)
    unchecked = env.get("EVAL_BA_RECORD_VIDEO") == "true" and not max_pixels
    actual = {"render": requested, "soft_render_env": soft, "all_black": all_black, "applied_env": applied}
    return _check("render_device", not soft and all_black is not True and not unchecked, actual)


def _progress_check(progress: dict | None, shards: list[int]) -> Check:
    """跑 M5：每个 shard 运行期间都读到过 .progress，且最后一次快照 total > 0。

    Args:
        progress: smoke_run.py 写的 progress_seen.json 内容；None 表示没有这份记录。
        shards: 参与冒烟的 shard 编号。

    Returns:
        progress 这一项检查结果。
    """
    if not isinstance(progress, dict):
        return _check("progress", False, {"reason": "没有 progress_seen.json，用 smoke_run.py 重跑冒烟"})
    bad = {}
    for shard in shards:
        entry = progress.get(str(shard))
        last = entry.get("last") if isinstance(entry, dict) else None
        if not isinstance(last, dict) or not isinstance(last.get("total"), int) or last["total"] <= 0:
            bad[str(shard)] = entry
    return _check("progress", not bad, {"bad": bad} if bad else {str(s): progress[str(s)]["last"] for s in shards})


def _max_steps_check(meta: dict, episodes: dict) -> Check:
    """跑 M11：config.max_steps 为正整数，所有 episode 的 steps 不超过它。

    Args:
        meta: harness_config（含 config.max_steps）。
        episodes: _read_episodes 返回的 episode 字典。

    Returns:
        max_steps 这一项检查结果。
    """
    config = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    max_steps = config.get("max_steps")
    valid = isinstance(max_steps, int) and not isinstance(max_steps, bool) and max_steps > 0
    steps = [ep.steps for ep in episodes.values() if isinstance(ep.steps, int)]
    elapsed = [float(ep.elapsed_sec) for ep in episodes.values() if isinstance(ep.elapsed_sec, (int, float))]
    over = [list(key) for key, ep in episodes.items()
            if valid and isinstance(ep.steps, int) and ep.steps > max_steps]
    actual = {"max_steps": max_steps, "max_episode_steps": max(steps) if steps else None, "over": over[:10],
              "elapsed_sec": {"mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
                              "max": max(elapsed) if elapsed else None}}
    return _check("max_steps", valid and not over, actual)


def _script_lines(path: str) -> list[list[str]]:
    """把脚本按行切成 token。

    Args:
        path: 脚本路径。

    Returns:
        每行的 token 列表：合并反斜杠续行、去 # 注释，`sb=(--param` 这类数组写法剥掉 `x=(` 与 `)`。

    Raises:
        OSError: 文件读不了。
        UnicodeDecodeError: 文件不是 UTF-8。
    """
    with open(path, encoding="utf-8") as f:
        text = f.read().replace("\\\n", " ")
    lines = []
    for line in text.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            tokens = line.split()
        tokens = [re.sub(r"^\w+=\(", "", t).rstrip(");") for t in tokens]
        if any(tokens):
            lines.append([t for t in tokens if t])
    return lines


def _flag_value(tokens: list[str], i: int, flag: str) -> str | None:
    """取 flag 的值。

    Args:
        tokens: 一行的 token 列表。
        i: 当前 token 下标。
        flag: 参数名，如 --config。

    Returns:
        tokens[i] 是 flag 或 flag=VALUE 时返回其值（flag 在行尾时为空串），否则 None。
    """
    token = tokens[i]
    if token == flag:
        return tokens[i + 1] if i + 1 < len(tokens) else ""
    if token.startswith(flag + "="):
        return token[len(flag) + 1:]
    return None


def _literal_problems(lines: list[list[str]]) -> list[str]:
    """找脚本里平台下发的参数没取 $EVAL_BA_* 的地方。

    Args:
        lines: _script_lines 切出的 token 行。

    Returns:
        问题描述列表；没有问题时为空列表。
    """
    problems = []
    for tokens in lines:
        for i in range(len(tokens)):
            for flag in FORBIDDEN_FLAGS:
                value = _flag_value(tokens, i, flag)
                if value is not None and "EVAL_BA_" not in value:
                    problems.append(f"{flag} {value!r} 不是 $EVAL_BA_*")
            field = _flag_value(tokens, i, "--benchmark-field")
            if field:
                key, _, value = field.partition("=")
                if key in PLATFORM_FIELDS and "EVAL_BA_" not in value:
                    problems.append(f"--benchmark-field {field!r} 不是 $EVAL_BA_*")
            param = _flag_value(tokens, i, "--param")
            if param and param.startswith("suite=") and "EVAL_BA_" not in param:
                problems.append(f"--param {param!r} 不是 $EVAL_BA_SUB_BENCH")
    return problems


def _platform_problems(lines: list[list[str]]) -> list[str]:
    """找 command 与脚本合起来缺的平台参数。

    Args:
        lines: command 与它调用的脚本切出的 token 行。

    Returns:
        问题描述列表：PLATFORM_FIELDS 每个键都要有 --benchmark-field，
        --config 指的 eval.yaml 要引用 ${oc.env:EVAL_BA_SERVER_TIMEOUT}。

    冒烟默认值与 harness 默认值相同（mode=sync），漏传或写死时运行后核对查不出来，只能看脚本。
    """
    fields = set()
    configs = []
    for tokens in lines:
        for i in range(len(tokens)):
            field = _flag_value(tokens, i, "--benchmark-field")
            if field:
                fields.add(field.partition("=")[0])
            for flag in ("--config", "-c"):
                value = _flag_value(tokens, i, flag)
                if value:
                    configs.append(value)
    problems = [f"缺 --benchmark-field {key}=$EVAL_BA_*" for key in PLATFORM_FIELDS if key not in fields]
    if not configs:
        return problems + ["vla-eval run 没传 --config"]
    for raw in configs:
        path = os.path.expandvars(raw)
        try:
            with open(path, encoding="utf-8") as f:
                text = "\n".join(line.split("#", 1)[0] for line in f.read().splitlines())
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"读不了 --config {raw}：{exc}")
            continue
        if not _TIMEOUT_REF_RE.search(text):
            problems.append(f"{os.path.basename(path)} 的 server.timeout 没写 ${{oc.env:EVAL_BA_SERVER_TIMEOUT}}"
                            "（不带默认值）")
    return problems


def _script_paths(args: list[str]) -> list[str]:
    """找出 command 调用的脚本。

    Args:
        args: command 经 shlex.split 切出的 token。

    Returns:
        脚本路径列表：以 .sh 结尾的 token，或 bash / sh 后的第一个非选项 token；已去重。
    """
    paths = []
    for i, arg in enumerate(args):
        after_shell = i > 0 and os.path.basename(args[i - 1]) in ("bash", "sh") and not arg.startswith("-")
        if (arg.endswith(".sh") or after_shell) and arg not in paths:
            paths.append(arg)
    return paths


def _command_check(smoke: dict | None) -> Check:
    """跑 M2：command 不带平台下发的参数，它调用的脚本里这些参数只能取 $EVAL_BA_*。

    Args:
        smoke: smoke_run.json 内容；None 表示没有这份记录。

    Returns:
        command_literal 这一项检查结果。
    """
    command = smoke.get("command") if isinstance(smoke, dict) else None
    if not isinstance(command, str) or not command.strip():
        return _check("command_literal", False, {"reason": "没有 smoke_run.json，用 smoke_run.py 重跑冒烟"})
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return _check("command_literal", False, {"command": command, "reason": f"切分失败：{exc}"})
    problems = [f"command 带 {a}" for a in args
                if any(a == f or a.startswith(f + "=") for f in FORBIDDEN_FLAGS + COMMAND_ONLY_FLAGS)]
    scripts = _script_paths(args)
    lines = [args]
    unreadable = False
    for path in scripts:
        try:
            script = _script_lines(path)
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"读不了 {path}：{exc}")
            unreadable = True
            continue
        lines.extend(script)
        problems.extend(f"{os.path.basename(path)}: {p}" for p in _literal_problems(script))
    if not unreadable:
        problems.extend(_platform_problems(lines))
    return _check("command_literal", not problems, {"command": command, "scripts": scripts, "problems": problems})


def _ray_check(log_text: str | None) -> Check:
    """跑 M3（只查版本）：ray_env_check.sh 的输出没有 [FAIL] 行，且最后一行是 RAY_ENV_OK。

    Args:
        log_text: ray_env_check.log 内容；None 表示没有这份记录。

    Returns:
        ray_version 这一项检查结果。
    """
    if log_text is None:
        return _check("ray_version", False, {"reason": "没有 ray_env_check.log"})
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    fails = [line for line in lines if line.startswith("[FAIL]")]
    return _check("ray_version", last == RAY_ENV_OK and not fails, {"last": last, "fail": fails[:5]})


def _live_check(log_dir: str) -> Check:
    """实时模式检查：shard 日志里不得有 harness 拒跑 live 的报错（LIVE_MODE_ERRORS）。

    Args:
        log_dir: 冒烟日志目录。

    Returns:
        live_hold_action 这一项检查结果。

    这类报错只在 mode=live 时出现，所以冒烟要另跑一次 --set EVAL_BA_MODE=live；sync 冒烟下本项恒过。
    """
    hits = []
    names = sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []
    for name in names:
        if not (name.startswith("shard") and name.endswith(".log")):
            continue
        try:
            with open(os.path.join(log_dir, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if any(part in line for part in LIVE_MODE_ERRORS):
                        hits.append({"log": name, "line": line.strip()[:300]})
                        break
        except OSError as exc:
            hits.append({"log": name, "line": f"读不了：{exc}"})
    return _check("live_hold_action", not hits, {"hits": hits})


def _exit_check(smoke: dict | None) -> Check:
    """跑 M4：冒烟里每个 shard 的 command 都以 0 退出。

    Args:
        smoke: smoke_run.json 内容；None 表示没有这份记录。

    Returns:
        exit_code 这一项检查结果。
    """
    codes = smoke.get("exit_codes") if isinstance(smoke, dict) else None
    if not isinstance(codes, list) or not codes:
        return _check("exit_code", False, {"reason": "没有 smoke_run.json，用 smoke_run.py 重跑冒烟"})
    return _check("exit_code", all(c == 0 for c in codes), {"exit_codes": codes})


def run_checks(db_path: str, env: dict[str, str], shards: list[int], *, reader=None,
               progress: dict | None = None) -> dict:
    """在冒烟产物上跑全部检查。

    Args:
        db_path: 冒烟产出的 recording DB。
        env: smoke_env.json 的内容（VIDEO_DIR 为含 {shard_id} 的模板，不含 EVAL_BA_SHARD_ID）。
        shards: 参与冒烟的 shard 编号。
        reader: 视频解码函数 path -> (帧数, 最大像素)；默认用 imageio。
        progress: smoke_run.py 写的 progress_seen.json 内容；None 时 progress 项不过。

    Returns:
        {"passed": bool, "checks": [{name, passed, m, hint, actual}], "harness_config": dict | None}。

    Raises:
        SelfcheckError: env 缺键、DB 不存在或读不了。
    """
    missing = [key for key in REQUIRED_ENV if not env.get(key)]
    if missing:
        raise SelfcheckError(f"env 缺键：{missing}")
    bench_id = ba_checks.bench_eval_id(env["EVAL_BA_EVAL_ID"], env["EVAL_BA_BENCH_NAME"])
    with _snapshot(db_path) as (conn, data):
        try:
            episodes, duplicated = _read_episodes(conn, bench_id)
            checks, reports = _param_checks(data, env, shards)
            queue = _queue_check(conn, bench_id, episodes, duplicated, shards)
        except sqlite3.Error as exc:
            raise SelfcheckError(f"recording DB 结构不对：{exc}") from exc
    harness_config = ba_checks.extract_harness_config(db_path, bench_id)
    meta = harness_config or {}
    checks.append(_progress_check(progress, shards))
    checks.extend(_result_checks(episodes, meta))
    checks.append(queue)
    max_pixels = []
    if env.get("EVAL_BA_RECORD_VIDEO") == "true":
        video_checks, max_pixels = _video_checks(env, shards, reports, episodes, reader or _default_reader)
        checks.extend(video_checks)
    checks.append(_render_check(env, meta, max_pixels))
    checks.append(_max_steps_check(meta, episodes))
    return {"passed": all(c.passed for c in checks), "checks": [c._asdict() for c in checks],
            "harness_config": harness_config}


def _success_rate(db_path: str) -> tuple[float | None, int]:
    """算对照 DB 全部 episode 的成功率。

    Args:
        db_path: 对照实验的 recording DB 路径。

    Returns:
        (成功率, episode 数)；success 为 True 记 1，没有 episode 时成功率为 None。

    Raises:
        SelfcheckError: 对照 DB 不存在或表结构不对。
    """
    with _snapshot(db_path) as (conn, _):
        try:
            rows = conn.execute("SELECT metrics FROM episode_results").fetchall()
        except sqlite3.Error as exc:
            raise SelfcheckError(f"对照 DB 结构不对：{exc}") from exc
    if not rows:
        return None, 0
    wins = sum(1 for (raw,) in rows if _load_metrics(raw).get("success") is True)
    return wins / len(rows), len(rows)


def check_control(control_db: str | None = None, oracle_db: str | None = None) -> list:
    """跑 M12 对照实验：零动作成功率须 <= CONTROL_ZERO_MAX_RATE，oracle 回放成功率须 > 0。

    Args:
        control_db: 零动作对照 DB；None 表示不做这项。
        oracle_db: oracle 回放对照 DB；None 表示不做这项。

    Returns:
        检查列表，没给 DB 的项不出现。

    Raises:
        SelfcheckError: 对照 DB 不存在或表结构不对。
    """
    checks = []
    if control_db is not None:
        rate, n = _success_rate(control_db)
        checks.append(_check("control_zero_action", rate is not None and rate <= CONTROL_ZERO_MAX_RATE,
                             {"rate": rate, "episodes": n, "max_rate": CONTROL_ZERO_MAX_RATE}))
    if oracle_db is not None:
        rate, n = _success_rate(oracle_db)
        checks.append(_check("control_oracle", rate is not None and rate > 0, {"rate": rate, "episodes": n}))
    return checks


def _print_report(checks: list[dict], passed: bool) -> None:
    """逐项打印检查结果。

    Args:
        checks: 检查结果列表，每项含 name、passed、m、hint、actual。
        passed: 整体结论，写在最后一行 RESULT。

    Returns:
        None。失败项附实测值与排查方法。
    """
    for c in checks:
        print(f"{'PASS' if c['passed'] else 'FAIL'} {c['m']}  {c['name']}")
        if not c["passed"]:
            print(f"      actual: {json.dumps(c['actual'], ensure_ascii=False, default=str)[:600]}")
            print(f"      hint:   {c['hint']}")
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        argparse.Namespace，含 db、env_file、progress_file、smoke_file、ray_env_log、shards、
        control、control_db、oracle_db、out、list。

    Raises:
        SystemExit: 参数非法时 argparse 打印用法并以退出码 2 结束。
    """
    parser = argparse.ArgumentParser(description="自定义 benchmark 镜像本机自检")
    parser.add_argument("--db", help="冒烟产出的 recording DB")
    parser.add_argument("--env-file", help="smoke_run.py 写出的 smoke_env.json")
    parser.add_argument("--progress-file", help="smoke_run.py 写出的 progress_seen.json；默认取 --env-file 同目录")
    parser.add_argument("--smoke-file", help="smoke_run.py 写出的 smoke_run.json（command 与退出码）；默认取 --env-file 同目录")
    parser.add_argument("--ray-env-log", help="ray_env_check.sh 的输出；默认取 --env-file 同目录的 ray_env_check.log")
    parser.add_argument("--shards", help="参与冒烟的 shard，逗号分隔；默认 0..EVAL_BA_NUM_SHARDS-1")
    parser.add_argument("--control", action="store_true", help="把 --db（echo MS 零动作冒烟）当零动作对照（M12）")
    parser.add_argument("--control-db", help="另跑的零动作对照 recording DB；给了就不用 --control")
    parser.add_argument("--oracle-db", help="oracle 回放对照实验的 recording DB")
    parser.add_argument("--out", help="完整报告写到这个 JSON 文件")
    parser.add_argument("--list", action="store_true", help="列出全部检查项后退出")
    return parser.parse_args(argv)


def _read_optional(path: str, as_json: bool):
    """按需读取可选文件。

    Args:
        path: 文件路径。
        as_json: True 按 JSON 解析，False 按文本读。

    Returns:
        文件内容；文件不存在时为 None。

    Raises:
        OSError: 文件存在但读不了。
        ValueError: as_json 为 True 且内容不是合法 JSON。
    """
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f) if as_json else f.read()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：读冒烟产物、跑全部检查并打印报告。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        退出码：0 全过，1 有检查不过，2 自检没跑起来。

    Raises:
        无。读文件与自检本身的异常都收敛成退出码 2。
    """
    args = parse_args(argv)
    if args.list:
        for name, (m, hint) in sorted(HINTS.items(), key=lambda kv: int(kv[1][0][1:])):
            print(f"{m:4} {name:20} {hint}")
        return 0
    if not args.db or not args.env_file:
        print("ERROR: 需要 --db 与 --env-file", file=sys.stderr)
        return 2
    control_db = args.control_db or (args.db if args.control else None)
    try:
        with open(args.env_file, encoding="utf-8") as f:
            env = json.load(f)
        if not isinstance(env, dict):
            raise SelfcheckError("env 文件须是 JSON 对象")
        env = {str(k): str(v) for k, v in env.items()}
        if args.shards:
            shards = [int(s) for s in args.shards.split(",") if s.strip()]
        else:
            shards = list(range(int(env.get("EVAL_BA_NUM_SHARDS") or 0)))
        workdir = os.path.dirname(os.path.abspath(args.env_file))
        progress = _read_optional(args.progress_file or os.path.join(workdir, "progress_seen.json"), True)
        smoke = _read_optional(args.smoke_file or os.path.join(workdir, "smoke_run.json"), True)
        ray_log = _read_optional(args.ray_env_log or os.path.join(workdir, "ray_env_check.log"), False)
        result = run_checks(args.db, env, shards, progress=progress)
        control = check_control(control_db, args.oracle_db)
    except (OSError, ValueError, SelfcheckError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    head = [_command_check(smoke), _ray_check(ray_log), _exit_check(smoke),
            _live_check(os.path.join(workdir, "logs"))]
    result["checks"] = [c._asdict() for c in head] + result["checks"] + [c._asdict() for c in control]
    result["passed"] = all(c["passed"] for c in result["checks"])
    result["control"] = {"zero_action": control_db is not None, "oracle": args.oracle_db is not None}
    _print_report(result["checks"], result["passed"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
