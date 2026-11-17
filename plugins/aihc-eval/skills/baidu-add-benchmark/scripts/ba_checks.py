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

"""自定义 benchmark 的运行后核对。

shard 退出后只读 harness v0.8.0 已有的 recording DB 表（``eval_metadata`` / ``eval_shards`` /
``episode_results`` / ``step_rows``），核对平台经 ``EVAL_BA_*`` 下发的运行期值是否真的生效。
任一不符说明启动脚本没按平台口径映射参数，结果口径不明，调用方判 shard 失败、不回传 DB。

纯标准库实现，不 import 本仓库其他模块：skill 自检脚本直接 vendor 本文件复用同一套口径。
"""

from __future__ import annotations

import collections
import contextlib
import json
import re
import sqlite3
import tempfile

# 一条不符项：check 为核对项名，expected / actual 为期望值与实际值（均可 JSON 序列化）
Mismatch = collections.namedtuple("Mismatch", ["check", "expected", "actual"])

# 平台口径的录屏块，与 start.sh 模板一致，改一边须同步另一边
PLATFORM_FILENAME_STEM = "task{task_idx:04d}_ep{episode_id:04d}"
PLATFORM_VIDEO_FPS = 20

# 与 harness orchestrator._SAFE_NAME_RE 一致：bench 级 eval_id = f"{eval_id}-{safe_name}"
_SAFE_NAME_RE = re.compile(r"[^\w\-.]")

# harness 超时 episode 的 failure_detail 形如 ``timeout=45s: <exc>``（orchestrator.py:502）
_TIMEOUT_DETAIL_RE = re.compile(r"^timeout=([0-9]+(?:\.[0-9]+)?)s:")


@contextlib.contextmanager
def _open_db(db_bytes: bytes):
    """把 DB 字节落临时文件后只读打开。

    Args:
        db_bytes: recording DB 的完整字节。

    Yields:
        sqlite3.Connection；离开上下文即关闭并删除临时文件。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        tmp.write(db_bytes)
        tmp.flush()
        conn = sqlite3.connect(tmp.name)
        try:
            yield conn
        finally:
            conn.close()


def bench_eval_id(eval_id: str, bench_name: str) -> str:
    """按 harness 规则算 bench 级 eval_id（``subname`` 由平台置空，resolved_name 即 name）。

    Args:
        eval_id: 平台下发的 ``EVAL_BA_EVAL_ID``。
        bench_name: 控制台条目的 name。

    Returns:
        ``f"{eval_id}-{safe_name}"``，safe_name 与 harness 的 ``_SAFE_NAME_RE`` 同口径。
    """
    return f"{eval_id}-{_SAFE_NAME_RE.sub('_', bench_name)}"


def _major_minor(version: str | None) -> str | None:
    """取版本号的 major.minor。

    Args:
        version: harness 版本号，如 "0.9.0"。

    Returns:
        "0.9" 这样的前两段；非字符串或不足两段时返回 None。
    """
    if not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    if len(parts) < 2:
        return None
    return ".".join(parts[:2])


def _to_float(value: object) -> float | None:
    """宽松转 float。

    Args:
        value: 待转的值，常见来源是 env 里的字符串或 JSON 里的数字。

    Returns:
        转换结果；转不了（None、非数字字符串等）时返回 None。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def expected_recording(env: dict[str, str]) -> dict[str, object]:
    """平台口径的 ``config.recording``：录屏开关随 ``EVAL_BA_RECORD_VIDEO``，其余固定。

    Args:
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。

    Returns:
        ``config.recording`` 的期望 dict：record_step、record_video、output_dir、filename_stem、video_fps。
    """
    return {
        "record_step": False,
        "record_video": env.get("EVAL_BA_RECORD_VIDEO") == "true",
        "output_dir": env.get("EVAL_BA_VIDEO_DIR"),
        "filename_stem": PLATFORM_FILENAME_STEM,
        "video_fps": PLATFORM_VIDEO_FPS,
    }


def sibling_video_dirs(env: dict[str, str]) -> set[str]:
    """同 bench 各 shard 的录屏目录：``EVAL_BA_VIDEO_DIR`` 末段 ``shard<本 shard>`` 换成 0..N-1。

    Args:
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。

    Returns:
        可接受的 output_dir 集合。同 Pod 的 shard 共用一个 DB，harness 只保留首个写入 shard 的
        eval_metadata，其中 ``recording.output_dir`` 是那个 shard 的目录；末段不是 ``shard<id>`` 时
        只认原值。``EVAL_BA_VIDEO_DIR`` 缺失或为空时只认同一个空值（与 ``expected_recording`` 的
        output_dir 同口径）。
    """
    video_dir = env.get("EVAL_BA_VIDEO_DIR")
    if not video_dir:
        return {video_dir}
    shard_id = env.get("EVAL_BA_SHARD_ID", "0")
    head, sep, tail = video_dir.rstrip("/").rpartition("/")
    if tail != f"shard{shard_id}":
        return {video_dir}
    try:
        num_shards = int(env.get("EVAL_BA_NUM_SHARDS", "1"))
    except ValueError:
        return {video_dir}
    return {video_dir} | {f"{head}{sep}shard{k}" for k in range(num_shards)}


def _recording_matches(actual: object, expected: dict[str, object], output_dirs: set | None = None) -> bool:
    """逐键比对录屏块。

    Args:
        actual: eval_metadata 里的 ``config.recording``，可能不是 dict。
        expected: ``expected_recording`` 算出的期望块。
        output_dirs: 可接受的 output_dir 集合（见 ``sibling_video_dirs``）；None 表示只认 expected 里的值。

    Returns:
        True 表示全部键相符。``video_fps`` 按数值比，布尔项须是真 bool（字符串 "false" 在 harness 里为真）。
    """
    if not isinstance(actual, dict):
        return False
    for key, want in expected.items():
        got = actual.get(key)
        if key == "output_dir" and output_dirs is not None:
            if got not in output_dirs:
                return False
        elif key == "video_fps":
            if _to_float(got) != float(want):
                return False
        elif isinstance(want, bool):
            if got is not want:
                return False
        elif got != want:
            return False
    return True


# 本 shard 名下 episode 行的 rowid，参数依次为 (bench_id, bench_id, bench_id, shard_id)。
# 同 Pod 各 shard 共用一个 DB，只能按 work_queue 认领方归属：item 是 harness work_items 的下标
# （按 task 展开、每 task 连续 episodes_per_task 个），故 item = 该 task 的最小 item + episode_id。
# 某项被 release 后由别的 shard 重跑会留下多行，与 export 去重口径一致取 rowid 最大的那行；
# 只认 done=1，认领了没跑完的不算。
_OWNED_ROWIDS_SQL = """
SELECT latest.rid FROM (
    SELECT MAX(rowid) AS rid, json_extract(context, '$.task_idx') AS task_idx, episode_id
    FROM episode_results
    WHERE eval_id = ? AND json_extract(context, '$.task_idx') IS NOT NULL
    GROUP BY task_name, episode_id, json_extract(context, '$.task_idx')
) AS latest
JOIN (SELECT task_idx, MIN(item) AS first_item FROM work_queue WHERE eval_id = ? GROUP BY task_idx) AS base
    ON base.task_idx = latest.task_idx
JOIN work_queue AS q ON q.eval_id = ? AND q.item = base.first_item + latest.episode_id
WHERE q.shard_id = ? AND q.done = 1
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """库里是否有这张表。

    Args:
        conn: 已打开的 recording DB 连接。
        name: 表名。

    Returns:
        表存在为 True。
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def has_work_queue(conn: sqlite3.Connection, bench_id: str | None) -> bool:
    """该 bench 是否走了 harness 动态队列：``work_queue`` 表存在且有它的行。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id；None 直接判否。

    Returns:
        True 表示走了动态队列。
    """
    if not bench_id or not _table_exists(conn, "work_queue"):
        return False
    return conn.execute(
        "SELECT 1 FROM work_queue WHERE eval_id = ? LIMIT 1", (bench_id,)).fetchone() is not None


def shard_rows_filter(conn: sqlite3.Connection, bench_id: str | None,
                      shard_id: int | None) -> tuple[str, tuple]:
    """``episode_results`` 上「本 shard 名下的行」的 WHERE 子句与参数。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id；None 表示旧调用方口径、查全库。
        shard_id: 本 shard 编号；None 表示不按 shard 归属过滤。

    Returns:
        (WHERE 子句, 参数元组)。走动态队列时按认领方归属；没有队列（表缺失或无本 bench 的行）时
        退回本 bench 全部行；bench_id 也没给时退回全库。
    """
    if bench_id and shard_id is not None and has_work_queue(conn, bench_id):
        return f"rowid IN ({_OWNED_ROWIDS_SQL})", (bench_id, bench_id, bench_id, shard_id)
    if bench_id:
        return "eval_id = ?", (bench_id,)
    return "1", ()


def shard_episode_stats(conn: sqlite3.Connection, bench_id: str | None = None,
                        shard_id: int | None = None) -> tuple[int, int]:
    """本 shard 名下的 episode 数与出错数。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id；None 表示旧调用方口径、查全库。
        shard_id: 本 shard 编号；None 表示不按 shard 归属过滤。

    Returns:
        (episode 数, 出错数)；出错口径与 harness 一致：``failure_reason`` 非空。
    """
    where, params = shard_rows_filter(conn, bench_id, shard_id)
    total, errors = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN failure_reason IS NOT NULL AND failure_reason != ''"
        f" THEN 1 ELSE 0 END) FROM episode_results WHERE {where}", params).fetchone()
    return int(total or 0), int(errors or 0)


def owned_episode_keys(conn: sqlite3.Connection, bench_id: str, shard_id: int) -> set[tuple[str, str]] | None:
    """本 shard 名下 episode 行的 ``(sid, eid)``。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id。
        shard_id: 本 shard 编号。

    Returns:
        ``(sid, eid)`` 集合；bench 没走动态队列、无法归属时返回 None。
    """
    if not has_work_queue(conn, bench_id):
        return None
    rows = conn.execute(f"SELECT sid, eid FROM episode_results WHERE rowid IN ({_OWNED_ROWIDS_SQL})",
                        (bench_id, bench_id, bench_id, shard_id))
    return {(sid, eid) for sid, eid in rows}


def _read_tables(conn: sqlite3.Connection, bench_id: str, shard_id: int,
                 rows_filter: tuple[str, tuple]) -> dict[str, object]:
    """一次读出核对需要的全部表内容。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id，用于取本 shard 的 eval_shards 行。
        shard_id: 本 shard 编号。
        rows_filter: ``shard_rows_filter`` 算出的 (WHERE 子句, 参数)。

    Returns:
        ``{meta_rows, shard_row, steps, timeouts}``；超时 episode 只看本 shard 名下的行。
    """
    meta_rows = conn.execute("SELECT eval_id, metadata FROM eval_metadata").fetchall()
    shard_row = conn.execute(
        "SELECT num_shards, complete FROM eval_shards WHERE eval_id = ? AND shard_id = ?",
        (bench_id, shard_id)).fetchone()
    (steps,) = conn.execute("SELECT COUNT(*) FROM step_rows").fetchone()
    where, params = rows_filter
    timeouts = [row[0] for row in conn.execute(
        f"SELECT failure_detail FROM episode_results WHERE failure_reason = 'timeout' AND {where}", params)]
    return {"meta_rows": meta_rows, "shard_row": shard_row, "steps": int(steps or 0), "timeouts": timeouts}


def _check_shard(shard_row: tuple | None, shard_id: int, num_shards: int,
                 require_complete: bool) -> Mismatch | None:
    """``eval_shards`` 须有本 shard 且 num_shards 一致；正常退出时还须 complete=1。

    Args:
        shard_row: 查到的 (num_shards, complete) 行；None 表示没有该 shard。
        shard_id: 本 shard 编号。
        num_shards: 平台下发的 shard 总数。
        require_complete: 子进程正常退出时为 True，此时要求 complete=1。

    Returns:
        Mismatch；全部相符时为 None。
    """
    expected = {"shard_id": shard_id, "num_shards": num_shards, "complete": 1 if require_complete else None}
    if shard_row is None:
        return Mismatch("shard", expected, None)
    actual = {"shard_id": shard_id, "num_shards": shard_row[0], "complete": shard_row[1]}
    if shard_row[0] != num_shards or (require_complete and shard_row[1] != 1):
        return Mismatch("shard", expected, actual)
    return None


def _check_metadata(meta: dict, env: dict[str, str]) -> list[Mismatch]:
    """核对 eval_metadata 里的 harness 版本、渲染、录屏、轮次与评测模式。

    Args:
        meta: eval_metadata 里解析出的 metadata dict。
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。

    Returns:
        不符项列表，空列表即全部相符。
    """
    found: list[Mismatch] = []
    config = meta.get("config") if isinstance(meta.get("config"), dict) else {}
    want_version = _major_minor(env.get("EVAL_BA_HARNESS_VERSION"))
    if want_version is not None and _major_minor(meta.get("harness_version")) != want_version:
        found.append(Mismatch("harness_version", env.get("EVAL_BA_HARNESS_VERSION"), meta.get("harness_version")))
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    want_render = (env.get("EVAL_BA_RENDER") or "gpu").strip().lower()
    if render.get("requested") != want_render:
        found.append(Mismatch("render", want_render, render.get("requested")))
    want_recording = expected_recording(env)
    if not _recording_matches(config.get("recording"), want_recording, sibling_video_dirs(env)):
        found.append(Mismatch("recording", want_recording, config.get("recording")))
    want_episodes = _to_float(env.get("EVAL_BA_EPISODES_PER_TASK"))
    if want_episodes is None or _to_float(config.get("episodes_per_task")) != want_episodes:
        found.append(Mismatch("episodes_per_task", env.get("EVAL_BA_EPISODES_PER_TASK"),
                              config.get("episodes_per_task")))
    mode = config.get("mode")
    want_live = env.get("EVAL_BA_MODE") == "live"
    if (isinstance(mode, str) and mode.startswith("live")) is not want_live:
        found.append(Mismatch("mode", env.get("EVAL_BA_MODE"), mode))
    return found


def _check_timeouts(details: list, env: dict[str, str]) -> Mismatch | None:
    """超时 episode 的 ``failure_detail`` 解析出的秒数须等于 ``EVAL_BA_SERVER_TIMEOUT``。

    Args:
        details: 本 shard 名下超时 episode 的 failure_detail 列表。
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。

    Returns:
        首个不符项；无超时 episode 或全部相符时返回 None。
    """
    want = _to_float(env.get("EVAL_BA_SERVER_TIMEOUT"))
    for detail in details:
        match = _TIMEOUT_DETAIL_RE.match(detail) if isinstance(detail, str) else None
        if match is None or want is None or float(match.group(1)) != want:
            return Mismatch("server_timeout", env.get("EVAL_BA_SERVER_TIMEOUT"),
                            detail[:80] if isinstance(detail, str) else detail)
    return None


def _count_invalid_results(conn: sqlite3.Connection, rows_filter: tuple[str, tuple]) -> int:
    """数出 ``failure_reason`` 为空却没有 bool 型 ``metrics.success`` 的 episode。

    Args:
        conn: 已打开的 recording DB 连接。
        rows_filter: ``shard_rows_filter`` 算出的 (WHERE 子句, 参数)，只数本 shard 名下的行。

    Returns:
        坏结果条数。``get_result()`` 漏了 ``success`` 时 harness 不报错，聚合会把它当 0 分静默算进
        成功率；共库时只看本 shard 的行，否则一个 shard 的坏结果会让同 bench 所有 shard 一起判失败。
    """
    invalid = 0
    where, params = rows_filter
    rows = conn.execute(
        f"SELECT metrics FROM episode_results WHERE (failure_reason IS NULL OR failure_reason = '') AND {where}",
        params)
    for (raw,) in rows:
        try:
            metrics = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            metrics = None
        if not isinstance(metrics, dict) or not isinstance(metrics.get("success"), bool):
            invalid += 1
    return invalid


def _count_shard_episodes(conn: sqlite3.Connection, bench_id: str, shard_id: int) -> int:
    """本 shard 跑完的 episode 数。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id。
        shard_id: 本 shard 编号。

    Returns:
        episode 数。同 Pod 的 shard 共用一个 DB，按 ``work_queue`` 认领方区分；没走动态队列
        （表缺失或无本 bench 的行）时退回数本 bench 的全部 episode。
    """
    if has_work_queue(conn, bench_id):
        (done,) = conn.execute(
            "SELECT COUNT(*) FROM work_queue WHERE eval_id = ? AND shard_id = ? AND done = 1",
            (bench_id, shard_id)).fetchone()
        return int(done or 0)
    (total,) = conn.execute("SELECT COUNT(*) FROM episode_results WHERE eval_id = ?", (bench_id,)).fetchone()
    return int(total or 0)


def _episode_layout(conn: sqlite3.Connection, bench_id: str) -> tuple[int, int]:
    """本 bench 实际展开的轮次。

    Args:
        conn: 已打开的 recording DB 连接。
        bench_id: bench 级 eval_id。

    Returns:
        (work_queue 单 task 最多的 item 数, episode_id 最大值)，没有数据时都为 -1。
        ``eval_metadata`` 只留先写入 shard 的那份，读不到后写入 shard 的真实配置；而各 shard 都会
        把自己算出的 work items 以 ``INSERT OR IGNORE`` 并入 ``work_queue``、按自己的轮次写
        ``episode_id``，哪个 shard 多跑了轮次都会在这两处露出来，与写入先后无关。
    """
    max_items = -1
    if has_work_queue(conn, bench_id):
        (max_items,) = conn.execute(
            "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM work_queue WHERE eval_id = ? GROUP BY task_idx)",
            (bench_id,)).fetchone()
    (max_ep,) = conn.execute(
        "SELECT MAX(episode_id) FROM episode_results WHERE eval_id = ?", (bench_id,)).fetchone()
    return (-1 if max_items is None else int(max_items)), (-1 if max_ep is None else int(max_ep))


def _check_layout(layout: tuple[int, int], env: dict[str, str]) -> Mismatch | None:
    """单 task 的队列项或 episode_id 超出 ``EVAL_BA_EPISODES_PER_TASK`` 即判不符。

    Args:
        layout: ``_episode_layout`` 算出的 (单 task 最多 item 数, episode_id 最大值)。
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。

    Returns:
        Mismatch；在范围内、或 env 里没给轮次时为 None。少跑的一侧不在此拦：它若先写元数据由
        ``_check_metadata`` 报，后写则按共享队列取项越界、shard 异常退出。
    """
    want = _to_float(env.get("EVAL_BA_EPISODES_PER_TASK"))
    max_items, max_ep = layout
    if want is None or (max_items <= want and max_ep < want):
        return None
    return Mismatch("episodes_per_task", env.get("EVAL_BA_EPISODES_PER_TASK"),
                    {"queue_items_per_task": max_items, "max_episode_id": max_ep})


# 核对结果：mismatches 为不符项列表（空即通过）；invalid_results 为缺 success 的 episode 数；
# episodes 为本 shard 跑完的 episode 数（供 video_missing 比对）
CheckReport = collections.namedtuple("CheckReport", ["mismatches", "invalid_results", "episodes"])


def check_recording(db_bytes: bytes | None, env: dict[str, str], *, require_complete: bool) -> CheckReport:
    """核对一个 shard 的 recording DB 是否按平台下发的 ``EVAL_BA_*`` 运行（共八项）。

    Args:
        db_bytes: ``$EVAL_BA_OUTPUT_DIR/recording-<eval_id>.sqlite`` 的快照字节；None 表示 DB 不存在。
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。
        require_complete: 子进程正常退出时为 True，此时 ``eval_shards.complete`` 须为 1；
            非零退出本就判失败，只核对口径、不要求跑完。

    Returns:
        CheckReport。DB 不存在或读不了时 mismatches 只含一条 ``db``，其余计数为 0。
    """
    if not db_bytes:
        return CheckReport([Mismatch("db", "recording-<eval_id>.sqlite", None)], 0, 0)
    try:
        shard_id = int(env.get("EVAL_BA_SHARD_ID", "0"))
        num_shards = int(env.get("EVAL_BA_NUM_SHARDS", "1"))
    except ValueError:
        return CheckReport([Mismatch("env", "integer EVAL_BA_SHARD_ID / EVAL_BA_NUM_SHARDS",
                                     [env.get("EVAL_BA_SHARD_ID"), env.get("EVAL_BA_NUM_SHARDS")])], 0, 0)
    bench_id = bench_eval_id(env.get("EVAL_BA_EVAL_ID", ""), env.get("EVAL_BA_BENCH_NAME", ""))
    try:
        with _open_db(db_bytes) as conn:
            rows_filter = shard_rows_filter(conn, bench_id, shard_id)
            tables = _read_tables(conn, bench_id, shard_id, rows_filter)
            invalid = _count_invalid_results(conn, rows_filter)
            episodes = _count_shard_episodes(conn, bench_id, shard_id)
            layout = _episode_layout(conn, bench_id)
    except sqlite3.Error as exc:
        return CheckReport([Mismatch("db", "readable recording DB", str(exc))], 0, 0)

    found: list[Mismatch] = []
    meta_rows = tables["meta_rows"]
    if len(meta_rows) != 1 or meta_rows[0][0] != bench_id:
        found.append(Mismatch("single_entry", [bench_id], [row[0] for row in meta_rows]))
    shard_mismatch = _check_shard(tables["shard_row"], shard_id, num_shards, require_complete)
    if shard_mismatch is not None:
        found.append(shard_mismatch)
    if tables["steps"] > 0:
        found.append(Mismatch("record_step", 0, tables["steps"]))
    meta = _load_meta(meta_rows, bench_id)
    if meta is not None:
        found.extend(_check_metadata(meta, env))
    layout_mismatch = _check_layout(layout, env)
    if layout_mismatch is not None and all(m.check != "episodes_per_task" for m in found):
        found.append(layout_mismatch)
    timeout_mismatch = _check_timeouts(tables["timeouts"], env)
    if timeout_mismatch is not None:
        found.append(timeout_mismatch)
    return CheckReport(found, invalid, episodes)


def _load_meta(meta_rows: list, bench_id: str) -> dict | None:
    """取本 bench 那一行 metadata 并解析。

    Args:
        meta_rows: ``eval_metadata`` 的 (eval_id, metadata) 行列表。
        bench_id: bench 级 eval_id。

    Returns:
        解析出的 dict；没有该行、解析失败或不是 dict 时返回 None（已由 single_entry 报不符）。
    """
    for row_id, raw in meta_rows:
        if row_id != bench_id:
            continue
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None
    return None


# 键名（小写）含这些片段的值脱敏
_SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "access_key", "credential")
REDACTED = "***"

# harness_config 从 eval_metadata 取的字段（不含 server 段，harness 本就不写）
_HARNESS_META_KEYS = ("benchmark", "mode", "config", "metric_keys", "harness_version", "server_info", "render")


def redact(value: object) -> object:
    """递归脱敏。

    Args:
        value: 待脱敏的值，dict / list 会被逐层展开。

    Returns:
        新对象：dict 中键名敏感的值替换为 ``***``，其余原样保留。
    """
    if isinstance(value, dict):
        return {key: REDACTED if _is_sensitive_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _is_sensitive_key(key: object) -> bool:
    """键名小写后是否含任一敏感片段。

    Args:
        key: dict 的键，可为任意类型。

    Returns:
        命中 ``_SENSITIVE_KEY_PARTS`` 任一为 True。
    """
    lowered = str(key).lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _observed_timeout(details: list) -> float | None:
    """超时 episode 的 ``failure_detail`` 里解析出的首个超时秒数。

    Args:
        details: 全库超时 episode 的 failure_detail 列表。

    Returns:
        秒数；没有超时 episode 或都解析不出时为 None。
    """
    for detail in details:
        match = _TIMEOUT_DETAIL_RE.match(detail) if isinstance(detail, str) else None
        if match is not None:
            return float(match.group(1))
    return None


def extract_harness_config(db_path: str, bench_id: str | None = None) -> dict | None:
    """从 recording DB 取 harness 实际生效的配置（``eval_metadata`` 一行）。

    harness 在第一个 episode 之前写 eval_metadata，运行中即可读；DB 开了 WAL，用 backup API
    取一致快照，不打扰正在写库的 harness。

    Args:
        db_path: recording DB 路径。
        bench_id: bench 级 eval_id；给定时只取该行，否则取第一行。

    Returns:
        ``{benchmark, mode, config, metric_keys, harness_version, server_info, render,
        observed_server_timeout}``，各字段递归脱敏；DB 不存在、读不了或没有匹配行时为 None。
    """
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        dst = sqlite3.connect(":memory:")
        try:
            src.backup(dst)
            meta_rows = dst.execute("SELECT eval_id, metadata FROM eval_metadata").fetchall()
            details = [row[0] for row in dst.execute(
                "SELECT failure_detail FROM episode_results WHERE failure_reason = 'timeout'")]
        finally:
            dst.close()
    except sqlite3.Error:
        return None
    finally:
        src.close()
    if bench_id is None and meta_rows:
        bench_id = meta_rows[0][0]
    meta = _load_meta(meta_rows, bench_id)
    if meta is None:
        return None
    found = {key: redact(meta.get(key)) for key in _HARNESS_META_KEYS}
    found["observed_server_timeout"] = _observed_timeout(details)
    return found


def missing_videos(env: dict[str, str], episodes: int, video_count: int) -> int:
    """开录屏时缺失的视频数。

    Args:
        env: 子进程实际拿到的 env（含全部 ``EVAL_BA_*``）。
        episodes: 本 shard 跑完的 episode 数。
        video_count: 实际数到的视频文件数。

    Returns:
        episode 数减视频数，不小于 0；未开录屏恒为 0。
    """
    if env.get("EVAL_BA_RECORD_VIDEO") != "true":
        return 0
    return max(episodes - video_count, 0)
