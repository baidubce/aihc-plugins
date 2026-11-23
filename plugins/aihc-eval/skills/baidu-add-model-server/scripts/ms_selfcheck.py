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
"""Model Server 协议自检（用户自定义镜像）。

对一台**已经起好**的 MS 连续跑一组可自动判定的断言，覆盖 `references/协议自检.md`
里能自动化的部分：`/config` 探活、HELLO 字段、**`action_spec` 与 benchmark 口径的一致性**、
信封四字段、seq 回显、首帧预算、ACTION 1-D 与有限值、图像四种编码、ERROR 不断连、
无 episode 上下文不崩、session 分桶、多 episode 状态重置、
`VECTOR_STEP` / `VECTOR_RESULT`（robodojo / ocbench 的硬门禁）。

**不依赖上游 harness**：只用 websockets + msgpack + numpy（png/jpeg 另需 pillow），
因此可以直接装进模型环境在开发机上跑。退出码 0 表示全过，非 0 表示有失败项。

用法：
    python ms_selfcheck.py --url ws://127.0.0.1:18000 --obs-spec obs.robodojo.json
    python ms_selfcheck.py --url ws://127.0.0.1:18000 --obs-spec obs.robodojo.json --benchmark robodojo
    python ms_selfcheck.py --url ws://127.0.0.1:18000 --obs-spec obs.robodojo.json --steps 3
    python ms_selfcheck.py --list
    python ms_selfcheck.py --url ws://127.0.0.1:18000 --skip image_formats,session_isolation
    # 自建推理服务带 Bearer 鉴权：Token 只从环境变量读，绝不走命令行（会进 shell 历史与 ps）
    VLA_EVAL_SERVER_TOKEN=<token> python ms_selfcheck.py --url ws://127.0.0.1:18000 \\
        --obs-spec obs.robodojo.json --require-auth

鉴权（`token_format` / `auth` 用例）与平台自建推理服务（selfHosted）同口径：设了
`--token-env` 指向的环境变量（默认 `VLA_EVAL_SERVER_TOKEN`，即 harness 客户端读 Token 的
变量）时，所有 HTTP 探活与 WS 握手都带 `Authorization: Bearer <token>`，并额外断言
「不带 / 带错 Token 的握手被 401/403 拒绝」。没设 Token 时这两个用例跳过；
`--require-auth` 把「没设 Token」本身判为 FAIL。输出里 Token 一律掩码为 `Bearer ***`。

`--obs-spec` 是交付门禁的一部分：**不给它 `obs_spec` 用例直接 FAIL**。相机键名必须
逐字等于目标 benchmark 实发的键名（不是模型内部的 image_prompt 名），否则自检会按
假键名自洽通过，上平台每个 episode 都 ERROR 或静默错分数。各 benchmark 的真实键名与
形状见 `references/评测集观测口径.md`。obs.json 形如（RoboDojo 的键名/维度口径）：
    {"images": {"cam_head": [480, 640, 3],
                "cam_left_wrist": [480, 640, 3],
                "cam_right_wrist": [480, 640, 3]},
     "states": 14,
     "task_description": "pick up the cup and place it on the tray"}

⚠️ 上例的 H/W 是 **RoboDojo 的实值**（已核实：RoboDojo `env_cfg/camera/template.py:2,9,16,23`
硬编码 `(640, 480)` 且无覆盖入口，harness 全链路不 resize，见 `评测集观测口径.md` §3.7）；
**换 benchmark 或上游换 pin 必须重查**，查法见 §3.9——到上游仿真器仓库查，不是只能实跑。
与 `references/开发机改造.md`「自检」一节的示例是同一份口径，两处要一起改。别拿参考 MS
`robodojo_pi05.py` warmup 里的 224×224 当 benchmark 实发尺寸——那是 openpi 内部 resize
后的模型输入尺寸；**写成 224 本自检照样全通过**（`obs_spec` 只校验 obs.json 的格式，不校验
它是否等于 benchmark 实发尺寸），要到平台上才暴露。

`states` 只是**维度声明**，不是线缆字段名；字段名由 `state_key` 给出，**按 benchmark 三分**：
`state`（robodojo；robocasa365 发的是嵌套 dict，本自检的向量口径不适用）、
`states`（libero / libero_pro / calvin / behavior1k / simpler；robocasa 9D 与 robotwin 16D 末端位姿
都要 `send_state=True` 才发）、`joint_state`（robotwin 14D，恒发）。不写 `state_key` 按 `state` 处理。
对照表见 `references/评测集观测口径.md` §4；**不要改 MS 去迁就自检**。

Authors: Baidu AI Cloud AIHC
"""
import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request

import msgpack
import numpy as np
import websockets

# ── 协议常量（与 harness vla_eval.protocol 同口径）
PROTOCOL_VERSION = 1
NDARRAY_KEY = "__ndarray__"
IMAGE_KEY = "__image__"
ENVELOPE_FIELDS = ("type", "payload", "seq", "timestamp")
ALLOWED_DTYPE_KINDS = frozenset("biuf")

MSG_HELLO = "hello"
MSG_OBSERVATION = "observation"
MSG_ACTION = "action"
MSG_EPISODE_START = "episode_start"
MSG_EPISODE_END = "episode_end"
MSG_ERROR = "error"
MSG_VECTOR_STEP = "vector_step"
MSG_VECTOR_RESULT = "vector_result"

# v0.9.0 里只有这两个 benchmark 是 `VectorStepBenchmark`（v0.9.0 实测：
# `git grep "VectorStepBenchmark)" v0.9.0 -- src/vla_eval/benchmarks/`）。
# 它们**必须**拿到 `capabilities.vector_step`，否则客户端连一条 episode 都不跑。
VECTOR_BENCHMARKS = frozenset({"robodojo", "ocbench"})

# ── benchmark 侧的 action_spec（逐字抄自 tag `v0.9.0` / commit 8798464 的
# `benchmarks/<name>/benchmark.py:get_action_spec()`）。运行时按构造参数分多套候选
# （robotwin 的 action_type、calvin 的 absolute_action），HELLO 与**任一**候选兼容即算过。
# `dims` 为 0 表示不校验长度。**不要照着某个模型改这张表**：它是 benchmark 的事实。
# harness 侧 `check_specs()` 对不上只产 warning、评测照跑 ⇒ 分数静默错；本用例把它做成硬门禁。
BENCH_ACTION_SPECS = {
    "robodojo": [
        {"actions": {"name": "actions", "dims": 14,
                     "format": "absolute_joint_positions_with_grippers"}},
    ],
    "robotwin": [
        # action_type="qpos"（默认）：14D 双臂关节
        {"joints": {"name": "joints", "dims": 14, "format": "joint_positions"}},
        # action_type="ee"：16D 双臂末端位姿
        {"ee_pose": {"name": "ee_pose", "dims": 16,
                     "format": "dual_eef_pos3_quatwxyz4_gripper1"}},
    ],
    "libero": [
        {"position": {"name": "position", "dims": 3, "format": "delta_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "axis_angle"},
         "gripper": {"name": "gripper", "dims": 1, "format": "binary_close_positive"}},
    ],
    "calvin": [
        # absolute_action=False（默认）：增量
        {"position": {"name": "position", "dims": 3, "format": "delta_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "euler_xyz"},
         "gripper": {"name": "gripper", "dims": 1, "format": "binary_close_negative"}},
        # absolute_action=True：rotation 走 ROTATION_EULER_ACCEPTS_AA（接受 axis_angle）
        {"position": {"name": "position", "dims": 3, "format": "absolute_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "euler_xyz",
                      "accepts": ["euler_xyz", "axis_angle"]},
         "gripper": {"name": "gripper", "dims": 1, "format": "binary_close_negative"}},
    ],
    "simpler": [
        {"position": {"name": "position", "dims": 3, "format": "delta_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "euler_xyz"},
         "gripper": {"name": "gripper", "dims": 1, "format": "binary_close_positive"}},
    ],
    "robocasa": [
        {"position": {"name": "position", "dims": 3, "format": "delta_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "euler_xyz"},
         "gripper": {"name": "gripper", "dims": 1, "format": "raw"}},
    ],
    "robocasa365": [
        {"position": {"name": "position", "dims": 3, "format": "delta_xyz"},
         "rotation": {"name": "rotation", "dims": 3, "format": "axis_angle"},
         "gripper": {"name": "gripper", "dims": 1, "format": "binary_close_01"},
         "base_motion": {"name": "base_motion", "dims": 4, "format": "base_velocity3_torso1"},
         "control_mode": {"name": "control_mode", "dims": 1, "format": "base_mode_01"}},
    ],
    "behavior_1k": [
        {"joints": {"name": "joints", "dims": 23, "format": "joint_positions_r1pro"}},
    ],
}

# benchmark 名的别名（调用方手填 / skill 文档两种写法都认）
BENCH_ALIASES = {
    "libero_pro": "libero",
    "libero_plus": "libero",
    "libero_mem": "libero",
    "behavior1k": "behavior_1k",
    "behavior-1k": "behavior_1k",
}

# ── 构造参数 → 它实际走哪一套 action spec。上面「与某候选兼容」只说明「MS 的说法是
# benchmark 的一种可能」；真正跑哪一套由提交评测时的 sub_bench 参数决定。robotwin 默认
# `action_type="qpos"`，此时 16D 的 `ee_pose` 会被**静默截断**成 14 跑完全程
# （`robotwin/benchmark.py:521-524`，不报错）；反过来 `ee` 模式 16D 不符才是 ValueError。
# 所以 `--bench-param` 传了就把候选收窄到实际那一套，不传只打提示。
# 值格式：参数名 → {取值: (候选必须含的段, 候选里必须出现的 format)}
BENCH_PARAM_FILTERS = {
    "robotwin": {"action_type": {"qpos": ({"joints"}, ""), "ee": ({"ee_pose"}, "")}},
    "calvin": {"absolute_action": {"false": (set(), "delta_xyz"), "true": (set(), "absolute_xyz")}},
}


def parse_bench_params(raw: list) -> dict:
    """把 `--bench-param` 的 `KEY=VALUE`（可重复、可逗号分隔）解析成 dict。

    Args:
        raw: `--bench-param` 的原始值列表。

    Returns:
        dict: 参数名 → 取值字符串；格式不对的项忽略。
    """
    out = {}
    for item in raw or []:
        for part in str(item).split(","):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            if name.strip():
                out[name.strip()] = value.strip()
    return out


def select_candidates(candidates: list, filters: dict, params: dict) -> list:
    """按构造参数把候选收窄到实际那一套。

    Args:
        candidates: `BENCH_ACTION_SPECS` 里该 benchmark 的候选列表。
        filters: `BENCH_PARAM_FILTERS` 里该 benchmark 的过滤表；空表示与参数无关。
        params: 已解析的 `--bench-param`。

    Returns:
        list: 收窄后的候选；参数与内置表不匹配时返回空列表。
    """
    keys = set()
    fmt = ""
    for name, spec in filters.items():
        value = params.get(name)
        if value is None:
            continue
        hit = spec.get(str(value).strip().lower())
        if hit is None:
            return []
        keys |= hit[0]
        fmt = hit[1] or fmt
    out = []
    for cand in candidates:
        if keys and not keys <= set(cand):
            continue
        if fmt and fmt not in {seg.get("format") for seg in cand.values()}:
            continue
        out.append(cand)
    return out

# ── 鉴权口径（与平台自建推理服务的创建期校验、harness 客户端同口径）
DEFAULT_TOKEN_ENV = "VLA_EVAL_SERVER_TOKEN"
TOKEN_MIN_BYTES = 16
TOKEN_MAX_BYTES = 4096
# RFC 6750 §2.1 b64token；同时拦住空白、控制字符与换行（防 header 注入）。
# 用 \Z 不用 $：Python 的 $ 会放过末尾一个 \n，而控制面 Go 正则的 $ 是严格串尾
TOKEN_RE = re.compile(r"^[A-Za-z0-9\-._~+/]+=*\Z")
# 带 Token 时 url query 不得再携带的凭据键（小写比对）
CREDENTIAL_QUERY_KEYS = frozenset(("token", "access_token", "auth", "key"))
# 平台探活把 401/403 判为 inference_auth_rejected 并立即停止重试，其余非 101 判为未就绪或被拒
AUTH_REJECT_STATUSES = (401, 403)
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*")
# websockets 14 起 connect() 的自定义头参数改名；harness 客户端按同一规则选参
_HEADERS_KWARG = ("additional_headers" if int(websockets.__version__.split(".")[0]) >= 14
                  else "extra_headers")


def auth_headers(token: str) -> dict:
    """生成鉴权头；无 Token 返回空 dict。

    Args:
        token: Bearer Token 原文，空串表示不鉴权。

    Returns:
        dict: `{"Authorization": "Bearer <token>"}` 或 `{}`。
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


def redact(text: str, secrets: typing.Iterable = ()) -> str:
    """掩码输出里的 Token：`Bearer <token>` → `Bearer ***`，Token 原文出现处 → `***`。

    Args:
        text: 待打印的文本。
        secrets: 需要整串替换的敏感值，空串忽略。

    Returns:
        str: 掩码后的文本。
    """
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return BEARER_RE.sub(r"\1***", text)

# 仅用于 `--skip obs_spec` 的快速试跑。**它的键名是假的**：不给 `--obs-spec` 时
# `obs_spec` 用例会 FAIL，因为按假键名自检只能自洽通过，上平台每个 episode 都 ERROR。
DEFAULT_OBS_SPEC = {
    "images": {"agentview": [256, 256, 3]},
    "states": 0,
    "task_description": "pick up the black bowl and place it on the plate",
}

def encode_hook(obj: typing.Any) -> typing.Any:
    """msgpack `default` 钩子：ndarray → `__ndarray__` dict。

    与 harness `numpy_codec.encode_ndarray` 的 `raw` 分支同口径（图像走
    `encode_image` 时已在构造观测时编好，这里只兜普通数组）。

    Args:
        obj: msgpack 无法原生序列化的对象；只处理 `np.ndarray`。

    Returns:
        dict: `{"__ndarray__": True, "data": 原始字节, "dtype": dtype.str, "shape": 形状列表}`。

    Raises:
        TypeError: `obj` 不是 `np.ndarray` 时抛出（交回 msgpack 报不可序列化）。
    """
    if isinstance(obj, np.ndarray):
        return {
            NDARRAY_KEY: True,
            "data": obj.tobytes(),
            "dtype": obj.dtype.str,
            "shape": list(obj.shape),
        }
    raise TypeError(f"不可序列化: {type(obj)}")


def decode_hook(dct: dict) -> typing.Any:
    """msgpack `object_hook`：`__ndarray__` / `__image__` → ndarray。

    Args:
        dct: msgpack 解出的一个 map。

    Returns:
        带 `__ndarray__` / `__image__` 标记时返回解码后的 `np.ndarray`（拷贝，可写）；
        否则原样返回 `dct`。

    Raises:
        ValueError: `__ndarray__` 的 dtype 种类不在 `ALLOWED_DTYPE_KINDS`（b/i/u/f）内。
    """
    if dct.get(NDARRAY_KEY):
        dtype = np.dtype(dct["dtype"])
        if dtype.kind not in ALLOWED_DTYPE_KINDS:
            raise ValueError(f"不允许的 dtype: {dtype.str}")
        return np.frombuffer(dct["data"], dtype=dtype).reshape(dct["shape"]).copy()
    if dct.get(IMAGE_KEY):
        return decode_image(dct)
    return dct


class MissingDependency(RuntimeError):
    """客户端侧缺可选依赖（如 pillow）。只影响该编码用例，不代表服务有问题。"""


def encode_image(img: np.ndarray, fmt: str) -> dict:
    """把 HWC uint8 图像编成 `__image__` dict。

    Args:
        img: `(H, W, C)` uint8，C ∈ {1, 3, 4}。
        fmt: `raw` / `png` / `jpeg`。

    Returns:
        dict: `__image__` 编码的 dict；`raw` 带 `dtype`，`png` / `jpeg` 的 `data`
        为压缩后字节（jpeg 质量 95）。

    Raises:
        MissingDependency: `fmt` 为 `png` / `jpeg` 但客户端没装 pillow。
        ValueError: `fmt` 为 `jpeg` 但通道数不是 3。
    """
    if fmt == "raw":
        return {IMAGE_KEY: True, "format": "raw", "data": img.tobytes(),
                "dtype": img.dtype.str, "shape": list(img.shape)}
    try:
        import PIL.Image
    except ImportError as exc:                      # png/jpeg 才需要 pillow
        raise MissingDependency(f"{fmt} 编码需要 pillow: {exc}") from exc
    if fmt == "jpeg" and img.shape[2] != 3:
        raise ValueError("JPEG 只支持 RGB")
    mode = {1: "L", 3: "RGB", 4: "RGBA"}[img.shape[2]]
    arr = img[:, :, 0] if img.shape[2] == 1 else img
    buf = io.BytesIO()
    PIL.Image.fromarray(arr, mode=mode).save(
        buf, format=fmt.upper(), **({"quality": 95} if fmt == "jpeg" else {}))
    return {IMAGE_KEY: True, "format": fmt, "data": buf.getvalue(), "shape": list(img.shape)}


def decode_image(dct: dict) -> np.ndarray:
    """`__image__` dict → ndarray（自检只在打印动作时用得到，留着对称）。

    Args:
        dct: 带 `__image__` 标记的 dict，`format` 为 `raw` / `png` / `jpeg`。

    Returns:
        np.ndarray: 解码后的图像；`raw` 按 `dtype` / `shape` 还原，其余由 pillow 解码。
    """
    if dct["format"] == "raw":
        return np.frombuffer(dct["data"], dtype=np.dtype(dct["dtype"])).reshape(dct["shape"]).copy()
    import PIL.Image
    return np.asarray(PIL.Image.open(io.BytesIO(dct["data"])))


def pack_message(mtype: str, payload: dict, seq: int) -> bytes:
    """打包一条消息（信封四字段 + `use_bin_type=True`，与 harness 一致）。

    Args:
        mtype: 消息类型，如 `MSG_HELLO` / `MSG_OBSERVATION`。
        payload: 消息体，其中的 ndarray 由 `encode_hook` 编码。
        seq: 本条消息的序号，服务端回包应原样回显。

    Returns:
        bytes: msgpack 编码后的整条信封，`timestamp` 取当前 `time.time()`。
    """
    raw = {"type": mtype, "payload": payload, "seq": seq, "timestamp": time.time()}
    return msgpack.packb(raw, default=encode_hook, use_bin_type=True)


def unpack_message(data: bytes) -> dict:
    """解包并校验信封四字段齐全。

    Args:
        data: 从 WebSocket 收到的一帧 msgpack 字节。

    Returns:
        dict: 解码后的信封，至少含 `type` / `payload` / `seq` / `timestamp`。

    Raises:
        ValueError: 顶层不是 map，或信封缺 `ENVELOPE_FIELDS` 中任一字段。
    """
    raw = msgpack.unpackb(data, object_hook=decode_hook, raw=False)
    if not isinstance(raw, dict):
        raise ValueError(f"顶层不是 map: {type(raw)}")
    missing = [k for k in ENVELOPE_FIELDS if k not in raw]
    if missing:
        raise ValueError(f"信封缺字段 {missing}，实际键={sorted(raw)}")
    return raw


class Report(object):
    """逐条记账，末尾汇总。"""

    def __init__(self, secrets: typing.Iterable = ()) -> None:
        """初始化。

        Args:
            secrets: 打印前要掩码的敏感值（Bearer Token 原文），空串忽略。
        """
        self.items = []
        self.secrets = tuple(s for s in secrets if s)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        """记一条结论并立刻打印。

        Args:
            name: 断言名，如 `hello.seq_echo`，失败时出现在汇总的失败项里。
            ok: 断言是否通过。
            detail: 附加说明，非空时接在结论后打印；打印前掩码 Token。

        Returns:
            传入的 `ok`，便于 `if not report.add(...): return`。
        """
        detail = redact(detail, self.secrets)
        self.items.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
              flush=True)
        return ok

    def note(self, text: str) -> None:
        """只打印不计入成败的观察项。

        Args:
            text: 要以 `[INFO]` 前缀打印的观察内容；打印前掩码 Token。
        """
        print(f"[INFO] {redact(text, self.secrets)}", flush=True)

    def failed(self) -> list:
        """返回失败项名字列表。"""
        return [n for n, ok, _ in self.items if not ok]

    def summary(self) -> int:
        """打印汇总，返回建议退出码。"""
        bad = self.failed()
        print(f"\n{'=' * 60}\n{len(self.items) - len(bad)}/{len(self.items)} 通过", flush=True)
        if bad:
            print("失败项：" + ", ".join(bad), flush=True)
        return 1 if bad else 0


class MsClient(object):
    """最小 WebSocket 客户端：手写信封、自管 seq。

    连接参数与 harness `Connection` 一致：`compression=None`、`max_size=None`、
    **`ping_interval=None`**（长推理会被库默认心跳误杀）；带 Token 时每次握手都发
    `Authorization: Bearer <token>`（harness 客户端重连也会重发）。
    """

    def __init__(self, url: str, timeout: float = 60.0, token: str = "") -> None:
        """初始化。

        Args:
            url: `ws://host:port`。
            timeout: 单次 recv 的超时秒数。
            token: Bearer Token，空串表示握手不带鉴权头。
        """
        self.url = url
        self.timeout = timeout
        self.token = token
        self.ws = None
        self.server_info = {}
        self._seq = 0

    async def __aenter__(self) -> "MsClient":
        """建连（不含 HELLO，便于单独断言 HELLO）。

        Returns:
            MsClient: 已建立 WebSocket 连接的自身实例。
        """
        kwargs = {_HEADERS_KWARG: auth_headers(self.token)} if self.token else {}
        self.ws = await websockets.connect(
            self.url, compression=None, max_size=None, ping_interval=None, **kwargs)
        return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        """关连接。

        Args:
            *exc_info: `async with` 传入的 `(exc_type, exc, traceback)`；不吞异常，仅用于签名兼容。
        """
        if self.ws is not None:
            await self.ws.close()

    @property
    def open(self) -> bool:
        """连接是否仍可用。

        Returns:
            bool: 已建连且未关闭时为 True；兼容新旧 websockets（`closed` 属性或 `close_code`）。
        """
        if self.ws is None:
            return False
        closed = getattr(self.ws, "closed", None)
        return not closed if closed is not None else self.ws.close_code is None

    async def send(self, mtype: str, payload: dict) -> int:
        """发一条消息，返回所用 seq。

        Args:
            mtype: 消息类型，如 `MSG_OBSERVATION`。
            payload: 消息体。

        Returns:
            int: 本条消息使用的 seq（连接内自增，从 1 开始）。
        """
        self._seq += 1
        await self.ws.send(pack_message(mtype, payload, self._seq))
        return self._seq

    async def recv(self, timeout: typing.Optional[float] = None) -> dict:
        """收一条消息（已校验信封）。

        Args:
            timeout: 本次等待秒数；为空或 0 时用构造时的 `self.timeout`。

        Returns:
            dict: 经 `unpack_message` 解码并校验过信封四字段的消息。

        Raises:
            asyncio.TimeoutError: 超时仍未收到消息。
            websockets.exceptions.ConnectionClosed: 等待期间连接被关闭。
            ValueError: 回包信封不合规（见 `unpack_message`）。
        """
        data = await asyncio.wait_for(self.ws.recv(), timeout or self.timeout)
        return unpack_message(data)

    async def hello(self) -> dict:
        """客户端先发 HELLO，返回服务端回包。"""
        seq = await self.send(MSG_HELLO, {"protocol_version": PROTOCOL_VERSION,
                                          "client": "ms_selfcheck"})
        msg = await self.recv()
        msg["_req_seq"] = seq
        self.server_info = msg.get("payload") or {}
        return msg


async def start_episode(cli: MsClient, name: str, sid: str = "") -> None:
    """发 EPISODE_START（不等回包；服务端此消息无响应）。

    Args:
        cli: 已建连的客户端。
        name: 任务名，只用于日志辨识。
        sid: 非空时作为 `recording.sid`，用于验证 session 分桶。
    """
    rec = {"eval_id": "selfcheck"}
    if sid:
        rec["sid"] = sid
    await cli.send(MSG_EPISODE_START, {"task": {"name": name}, "recording": rec})


async def end_episode(cli: MsClient, success: bool = False) -> None:
    """发 EPISODE_END。

    Args:
        cli: 已建连的客户端。
        success: 写进 payload 的 `success` 字段；自检不评成败，默认 False。
    """
    await cli.send(MSG_EPISODE_END, {"success": success})


async def act(cli: MsClient, obs: dict, timeout: typing.Optional[float] = None) -> tuple:
    """发一帧观测并等回包。

    Args:
        cli: 已完成 HELLO 的客户端。
        obs: 观测 payload，通常来自 `build_obs()`。
        timeout: 等回包的秒数；为空时用客户端默认超时。

    Returns:
        `(msg, elapsed)`：`msg` 是 ACTION 或 ERROR 的完整信封，`elapsed` 为秒。

    Raises:
        asyncio.TimeoutError: 超时未收到回包。
        websockets.exceptions.ConnectionClosed: 服务端在回包前断开连接。
        ValueError: 回包信封不合规（见 `unpack_message`）。
    """
    t0 = time.monotonic()
    seq = await cli.send(MSG_OBSERVATION, obs)
    msg = await cli.recv(timeout)
    msg["_req_seq"] = seq
    return msg, time.monotonic() - t0


def build_obs(spec: dict, image_format: str, rng: typing.Any) -> dict:
    """按 spec 造一帧**真实形状**的随机观测。

    Args:
        spec: 见 `DEFAULT_OBS_SPEC`。
        image_format: `ndarray`（harness 的 raw 口径，图像走 `__ndarray__`）
            或 `raw` / `png` / `jpeg`（走 `__image__`）。
        rng: `np.random.Generator`。

    Returns:
        可直接交给 `act()` 的 payload。
    """
    obs = {"images": {}}
    for key, shape in (spec.get("images") or {}).items():
        img = rng.integers(0, 255, tuple(shape), dtype=np.uint8)
        obs["images"][key] = img if image_format == "ndarray" else encode_image(img, image_format)
    if spec.get("states"):
        # spec 里的 `states` 是「维度声明」，**不是**线缆字段名；字段名取 `state_key`
        # （按 benchmark 三分：state / states / joint_state，见 评测集观测口径.md §4）。
        # 字段名填错会让 MS 在自检里看不到本体状态却照样跑完，上平台后同一份代码
        # 拿到的是另一个名字，两边口径不同 = 自检没验到。
        # 不用全零：零状态常被模型当合法输入，掩盖维度/量纲问题。
        obs[spec.get("state_key") or "state"] = rng.standard_normal(
            int(spec["states"])).astype(np.float32)
    if spec.get("task_description"):
        obs["task_description"] = spec["task_description"]
    return obs


def build_batch(spec: dict, image_format: str, rng: typing.Any, rows: int = 1) -> dict:
    """按 spec 造一个 **batch**（`VECTOR_STEP.observations` 的列式口径）。

    `VECTOR_STEP` 的观测不是单帧，而是「每列一个带行维的数组」：第 i 行对应
    `slots[i]` 这个环境槽位。单槽位时行数为 1，形状是 `[1, ...]` 而不是 `[...]`。

    Args:
        spec: 见 `DEFAULT_OBS_SPEC`。
        image_format: 同 `build_obs()`。
        rng: `np.random.Generator`。
        rows: 行数（= 本轮 `slots` 的个数）。

    Returns:
        可直接放进 `VECTOR_STEP.payload["observations"]` 的列式 batch。

    Raises:
        ValueError: rows 小于 1。
    """
    if rows < 1:
        raise ValueError(f"rows 必须 >= 1，实际 {rows}")
    batch = {"images": {}}
    for key, shape in (spec.get("images") or {}).items():
        cols = []
        for _ in range(rows):
            img = rng.integers(0, 255, tuple(shape), dtype=np.uint8)
            cols.append(img if image_format == "ndarray" else encode_image(img, image_format))
        batch["images"][key] = np.stack(cols) if image_format == "ndarray" else cols
    if spec.get("states"):
        batch[spec.get("state_key") or "state"] = rng.standard_normal(
            (rows, int(spec["states"]))).astype(np.float32)
    if spec.get("task_description"):
        batch["task_description"] = [spec["task_description"]] * rows
    return batch


def batch_rows(batch: typing.Any) -> typing.Optional[int]:
    """列式 batch 的行数；各列长度不一致或为空时返回 None（与 harness `columns.num_rows` 同口径）。

    Args:
        batch: 列式 batch，可能是嵌套 dict（`images`）或直接是 ndarray。

    Returns:
        行数；无法判定时为 None。
    """
    lengths = set()

    def walk(node: typing.Any) -> None:
        """递归收集 node 中各数组（list / tuple / ndarray）的长度，写入 lengths。

        Args:
            node: batch 或其中的子节点。

        Returns:
            None。
        """
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple, np.ndarray)):
            lengths.add(len(node))

    walk(batch)
    return lengths.pop() if len(lengths) == 1 else None


async def vector_step(cli: MsClient, payload: dict) -> tuple:
    """发一条 `VECTOR_STEP` 并等 `VECTOR_RESULT`。

    Args:
        cli: 已完成 HELLO 的客户端。
        payload: `{ends, starts, slots, observations}`。

    Returns:
        `(msg, elapsed)`：`msg` 是 VECTOR_RESULT 或 ERROR 的完整信封。

    Raises:
        asyncio.TimeoutError: 服务端不认这条消息（不实现 vector 时最常见的形态）。
        websockets.exceptions.ConnectionClosed: 服务端在回包前断开。
        ValueError: 回包信封不合规。
    """
    t0 = time.monotonic()
    seq = await cli.send(MSG_VECTOR_STEP, payload)
    msg = await cli.recv()
    msg["_req_seq"] = seq
    return msg, time.monotonic() - t0


def assert_action(msg: dict, report: Report, tag: str, expect_dim: int = 0) -> int:
    """校验一条 ACTION 回包，返回动作维度（失败返 0）。

    断言：类型是 ACTION、`seq` 回显请求的 seq、`actions` 是 1-D、全有限值、
    维度与 HELLO 声明的 `action_spec` 一致。

    Args:
        msg: `act()` 返回的回包信封，需带 `_req_seq`。
        report: 记账对象，每条断言都记一条。
        tag: 断言名前缀，如 `ep0.step1`。
        expect_dim: 期望动作维度；0 表示不校验维度。

    Returns:
        int: `actions` 为 1-D 时返回其长度；类型不是 ACTION、缺 `actions` 或非 1-D 时返回 0。
    """
    if msg["type"] != MSG_ACTION:
        report.add(f"{tag}.type", False,
                   f"期望 action，实际 {msg['type']}: {str(msg.get('payload'))[:200]}")
        return 0
    report.add(f"{tag}.seq_echo", msg["seq"] == msg["_req_seq"],
               f"req={msg['_req_seq']} resp={msg['seq']}（应回显）")
    raw = (msg.get("payload") or {}).get("actions")
    if raw is None:
        report.add(f"{tag}.actions", False, f"payload 无 actions 键: {sorted(msg.get('payload') or {})}")
        return 0
    report.add(f"{tag}.ndarray_encoded", isinstance(raw, np.ndarray),
               f"actions 类型 {type(raw).__name__}（应为 __ndarray__ 编码）")
    arr = np.asarray(raw)
    report.add(f"{tag}.one_dim", arr.ndim == 1,
               f"shape={arr.shape}（线上必须是 1-D 单步，chunk 只在服务内缓存逐帧 pop）")
    report.add(f"{tag}.finite", bool(np.isfinite(arr).all()),
               f"dtype={arr.dtype} 含 nan/inf={not np.isfinite(arr).all()}")
    if expect_dim and arr.ndim == 1:
        report.add(f"{tag}.dim_match", arr.shape[0] == expect_dim,
                   f"actions {arr.shape[0]} 维 vs HELLO action_spec {expect_dim} 维")
    return int(arr.shape[0]) if arr.ndim == 1 else 0


def spec_dim(server_info: dict) -> int:
    """从 HELLO 的 `action_spec` 推出动作总维度，推不出返 0。

    Args:
        server_info: 服务端 HELLO 的 payload。

    Returns:
        int: `action_spec` 认定字段是 v0.9.0 的 `dims`（`shape` 只作兜底，好让本函数在
        `action_spec.schema` 已判 FAIL 时仍能给出维度提示）；`dims` 为 int 时直接加，
        为 list/tuple 时取其元素数（空计 1）；整体为 int 时直接返回；含无法识别的项或缺失时返回 0。
    """
    spec = server_info.get("action_spec")
    if isinstance(spec, dict):
        total = 0
        for val in spec.values():
            if isinstance(val, dict):
                dims = val.get("dims", val.get("shape"))
                if isinstance(dims, int):
                    total += dims
                elif isinstance(dims, (list, tuple)):
                    total += int(np.prod(dims)) if dims else 1
                else:
                    return 0
            elif isinstance(val, int):
                total += val
            else:
                return 0
        return total
    if isinstance(spec, int):
        return spec
    return 0


def http_get(url: str, timeout: float = 10.0, token: str = "") -> tuple:
    """不经代理地 GET 一次（自检环境里代理指向死端口）。

    Args:
        url: 完整 HTTP 地址，如 `http://127.0.0.1:18000/config`。
        timeout: 请求超时秒数。
        token: Bearer Token，非空时带 `Authorization` 头（平台探活每一步都带）。

    Returns:
        `(status, body_bytes)`；连不上则 `(0, 错误信息 bytes)`。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=auth_headers(token))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return getattr(resp, "status", resp.getcode()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:                        # 连接层失败统一归一
        return 0, str(exc).encode()


async def check_obs_spec(args: argparse.Namespace, report: Report) -> None:
    """观测口径门禁：`--obs-spec` 必须给、且像真实 benchmark 的观测。

    这条是「相机键名不符 → 上平台静默 0 分」唯一的机械防线。模型内部命名
    （image_prompts / 训练口径）与 benchmark 实发的观测键名是**两套命名空间**，
    用模型内部命名写 obs.json 会让自检自洽通过、上平台每个 episode 都 ERROR。
    真实键名的三个来源：harness 同 benchmark 的参考 MS 实现
    `src/vla_eval/model_servers/<name>.py`、benchmark 实现本身、
    上游 `docs/reproductions/<name>.md`。

    快速试跑可 `--skip obs_spec`，但交付门禁不许跳。

    Args:
        args: `parse_args()` 的结果，用到 `obs_spec_path` 与已加载的 `obs_spec`。
        report: 记账对象；未给 `--obs-spec` 或无相机键时记 FAIL 并提前返回。
    """
    if not report.add("obs_spec.provided", bool(getattr(args, "obs_spec_path", "")),
                      "必须用 --obs-spec 给目标 benchmark 的真实观测；"
                      "缺省会按 DEFAULT_OBS_SPEC 的假键名自洽通过，上平台静默错分数"):
        return
    spec = args.obs_spec if isinstance(args.obs_spec, dict) else {}
    images = spec.get("images") or {}
    if not report.add("obs_spec.images", isinstance(images, dict) and bool(images),
                      f"相机键名 {sorted(map(str, images))}"):
        return
    bad = [f"{k}={v}" for k, v in images.items()
           if not (isinstance(v, (list, tuple)) and len(v) == 3
                   and all(isinstance(n, int) and n > 0 for n in v) and v[2] in (1, 3, 4))]
    report.add("obs_spec.image_shape", not bad,
               "每路必须是 [H, W, C] 正整数且 C∈{1,3,4}" + (f"，不合规 {bad}" if bad else ""))
    states = spec.get("states")
    report.add("obs_spec.states", isinstance(states, int) and states >= 0,
               f"states={states!r}（必须显式给整数维度；0 表示该 benchmark 不发 state）")
    state_key = spec.get("state_key") or "state"
    report.add("obs_spec.state_key", state_key in ("state", "states", "joint_state"),
               f"state_key={state_key!r}（线缆字段名按 benchmark 三分："
               "state / states / joint_state，必须等于 评测集观测口径.md §4 那一格）")
    task = spec.get("task_description")
    report.add("obs_spec.task_description", isinstance(task, str) and bool(task.strip()),
               f"task_description={task!r}")
    report.note("相机键名必须逐字等于 benchmark 实发的键名，不是模型内部的 image_prompt 名")
    report.note("键名/形状/state 维度照 references/评测集观测口径.md 核对，别照开发机手工跑通那套")


async def check_config(args: argparse.Namespace, report: Report) -> None:
    """`GET /config` 探活：平台就是用这条判就绪的（**不是** `/health`）。

    Args:
        args: `parse_args()` 的结果，用到 `http`（HTTP 基址）与 `http_timeout`。
        report: 记账对象；`/config` 非 200 时记 FAIL 并跳过白名单参数用例。
    """
    status, body = http_get(f"{args.http}/config", args.http_timeout, args.token)
    if not report.add("config.status_200", status == 200,
                      f"{args.http}/config → {status or '连接失败'} {body[:200].decode(errors='replace')}"):
        return
    report.note(f"/config body: {body[:300].decode(errors='replace')}")
    status, body = http_get(f"{args.http}/config?max_batch_size=1", args.http_timeout, args.token)
    report.add("config.query_whitelist", status == 200,
               f"?max_batch_size=1 → {status}（白名单参数应仍返 200）")


async def check_hello(args: argparse.Namespace, report: Report) -> dict:
    """HELLO 握手：客户端先发，服务端回 HELLO 且带协议版本与 specs。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout` 与 `obs_spec`（比对相机键名）。
        report: 记账对象。

    Returns:
        服务端 HELLO 的 payload（后续用例用它取 `action_spec`）；握手异常时返回空 dict。
    """
    async with MsClient(args.url, args.timeout, args.token) as cli:
        try:
            msg = await cli.hello()
        except Exception as exc:
            report.add("hello.roundtrip", False, f"{type(exc).__name__}: {exc}")
            return {}
        report.add("hello.roundtrip", True, "信封四字段齐全")
        report.add("hello.type", msg["type"] == MSG_HELLO, f"回包 type={msg['type']}")
        report.add("hello.seq_echo", msg["seq"] == msg["_req_seq"],
                   f"req={msg['_req_seq']} resp={msg['seq']}")
        info = msg.get("payload") or {}
        report.add("hello.protocol_version", info.get("protocol_version") == PROTOCOL_VERSION,
                   f"protocol_version={info.get('protocol_version')}（必须为 {PROTOCOL_VERSION}）")
        report.add("hello.model_server", bool(info.get("model_server")),
                   f"model_server={info.get('model_server')!r}")
        report.note(f"HELLO payload 键: {sorted(info)}")
        report.note(f"action_spec={info.get('action_spec')} 推出维度={spec_dim(info)}")
        legacy = [key for key, val in (info.get("observation_spec") or {}).items()
                  if isinstance(val, dict) and not {"name", "dims", "format"} <= set(val)]
        if legacy:
            report.add("hello.observation_spec_schema", False,
                       f"{legacy} 缺 v0.9.0 的 name/dims/format（写成了按别名嵌套的 map？）。"
                       "`DimSpec.from_dict` 会抛 KeyError('name')，被 orchestrator 吞成一条 warning，"
                       "跨端观测校验等于没做；相机别名声明放到 HELLO 的另一个键里")
        declared = (info.get("camera_slots") or (info.get("observation_spec") or {}).get("images") or {})
        sending = (args.obs_spec or {}).get("images") or {}
        if isinstance(declared, dict) and declared and isinstance(sending, dict) and sending:
            lhs = {str(k).strip().lower() for key, val in declared.items()
                   for k in ([key] + list(val) if isinstance(val, list) else [key])}
            rhs = {str(k).strip().lower() for k in sending}
            if not (lhs & rhs):
                report.note(
                    f"MS 声明的相机键名 {sorted(lhs)} 与将发送的 {sorted(rhs)} 零交集："
                    "MS 内部必须做「观测键名 → 模型槽位」的别名映射，否则 inference 会 ERROR；"
                    "不要反过来改模型参数去迁就观测键名（会静默改变模型输入）")
        return info


def action_spec_reasons(server: dict, expect: dict) -> list:
    """复刻 harness `DimSpec.is_compatible()` + `check_specs()` 的动作半边语义。

    Args:
        server: HELLO 声明的 `action_spec`（key → `{name, dims, format, ...}`）。
        expect: `BENCH_ACTION_SPECS` 里的一套候选（key → 期望段）。

    Returns:
        list: 不兼容原因，空列表表示兼容。规则与上游一致：benchmark 要的段缺失即不兼容；
        交集内 `accepts` 存在时查成员、否则 `format` 等值，`format` 相同才比 `dims`
        （两端都 >0 才比）。
    """
    reasons = []
    for key, want in expect.items():
        got = server.get(key)
        if not isinstance(got, dict):
            reasons.append(f"{key}: benchmark 要这段，MS 没声明")
            continue
        accepts = want.get("accepts")
        got_fmt = got.get("format")
        if accepts is not None:
            if got_fmt not in accepts:
                reasons.append(f"{key}: format `{got_fmt}` 不在 benchmark accepts {sorted(accepts)} 内")
            continue
        if got_fmt != want.get("format"):
            reasons.append(f"{key}: format `{got_fmt}` vs benchmark `{want.get('format')}`")
            continue
        got_dims = got.get("dims", got.get("shape"))
        want_dims = want.get("dims", 0)
        if isinstance(got_dims, int) and want_dims > 0 and got_dims > 0 and got_dims != want_dims:
            reasons.append(f"{key}: dims {got_dims} vs benchmark {want_dims}")
    return reasons


async def check_action_spec(args: argparse.Namespace, report: Report, info: dict) -> None:
    """动作口径门禁：HELLO 的 `action_spec` 必须与 benchmark 的 `get_action_spec()` 兼容。

    harness 侧对不上**只产一条 warning、不中断**，评测照跑、分数是错的——这条用例把
    同一份比对搬到交付门禁上，做成硬失败。比对算法与 `is_compatible()` 一致：
    benchmark 要的段 MS 没声明即不兼容；交集内 `accepts` 存在时查成员，否则 `format`
    等值（`format` 相同才比 `dims`）。

    内置表只覆盖常用 benchmark；表外的名字**不判 FAIL**（只提示人工核），因为凭记忆
    编一个期望值比不查更糟。

    Args:
        args: `parse_args()` 的结果，用到 `benchmark`（为空时取 HELLO 的 `benchmark`）与
            `bench_param`（提交评测时的构造参数，如 `action_type=ee`）。
        report: 记账对象。
        info: `check_hello()` 返回的 HELLO payload。
    """
    spec = info.get("action_spec")
    if not report.add("action_spec.declared", isinstance(spec, dict) and bool(spec),
                      f"HELLO action_spec={spec!r}（必须声明；每段是 name/dims/format 的 DimSpec 口径）"):
        return
    # schema 与维度同样要门禁：写成旧写法的 `shape` 时 `dims` 缺失，下面的维度比对会被跳过，
    # 一个 16D 的 `joints` 就溜过去了。v0.9.0 的 `to_dict()` 一定输出整数 `dims`，
    # harness `DimSpec.from_dict()` 也直接取 `d["dims"]`（缺键即 KeyError）。
    bad = [key for key, val in spec.items()
           if not (isinstance(val, dict) and {"name", "dims", "format"} <= set(val)
                   and isinstance(val.get("dims"), int))]
    detail = (f"{bad} 缺 v0.9.0 的 name/dims/format，或 dims 不是整数（写成 shape/dtype 即视为旧口径）。"
              "`DimSpec.from_dict` 会抛 KeyError('name')，被 orchestrator 吞成一条 warning，"
              "跨端动作校验等于没做" if bad
              else "每段都是 name + 整数 dims + format")
    if not report.add("action_spec.schema", not bad, detail):
        return
    bench = str(args.benchmark or info.get("benchmark") or "").strip().lower()
    key = BENCH_ALIASES.get(bench, bench)
    candidates = BENCH_ACTION_SPECS.get(key)
    if not candidates:
        report.add("action_spec.compatible", True,
                   f"benchmark={bench or '<未声明>'} 不在内置表 {sorted(BENCH_ACTION_SPECS)} 内，"
                   "本条不判 FAIL：请按 `git show v0.9.0:src/vla_eval/benchmarks/<name>/benchmark.py` "
                   "的 get_action_spec() 人工核 format/dims（harness 侧 mismatch 只打 warning，分数会静默错）")
        return
    tried = []
    filters = BENCH_PARAM_FILTERS.get(key, {})
    params = parse_bench_params(getattr(args, "bench_param", None))
    unknown = sorted(set(params) - set(filters))
    if unknown:
        report.add("action_spec.compatible", False,
                   f"benchmark={key} 不认识 --bench-param {unknown}，本用例等于没被钉住。"
                   f"可选参数: { {k: sorted(v) for k, v in filters.items()} or '<该 benchmark 无构造参数维度>'}。"
                   "拼错的参数会静默失效，所以这里判 FAIL")
        return
    narrowed = select_candidates(candidates, filters, params)
    if not narrowed:
        report.add("action_spec.compatible", False,
                   f"benchmark={key} 传了 {params}，但内置表没有匹配的候选（该 benchmark 的合法取值："
                   f"{ {k: sorted(v) for k, v in filters.items()} }）")
        return
    for cand in narrowed:
        reasons = action_spec_reasons(spec, cand)
        if not reasons:
            report.add("action_spec.compatible", True,
                       f"benchmark={key} 命中候选 {sorted(cand)}"
                       f"{'（' + str(params) + '）' if params else ''}；MS 声明 "
                       f"{ {k: v.get('format') for k, v in spec.items()} }")
            extra = sorted(set(spec) - set(cand))
            if extra:
                report.note(f"MS 多声明了 benchmark 不消费的段 {extra}：harness 不报错，"
                            "但这段格式不会被校验，确认是别名/冗余声明而非把两套口径混在一起")
            if filters and not params:
                report.note(
                    f"benchmark={key} 的动作口径还由构造参数决定：{ {k: sorted(v) for k, v in filters.items()} }。"
                    "本条只验「MS 声明的段」自洽，**不知道 BA 侧实际传了什么**：robotwin 默认 "
                    "action_type=qpos 时 16D 的 ee_pose 会被静默截断成 14 跑完全程（不报错）。"
                    "按提交评测的参数用 --bench-param 复跑一遍把它钉住")
            return
        tried.append(f"候选 {sorted(cand)}: " + "; ".join(reasons))
    report.add("action_spec.compatible", False,
               f"benchmark={key} 的 {len(narrowed)} 个候选都不兼容：\n    "
               + "\n    ".join(tried)
               + f"\n  MS 声明: {json.dumps(spec, ensure_ascii=False)}"
               "\n  harness 侧只打 warning、评测照跑，分数是错的。逐字抄 benchmark 的 format 串；"
               "模型语义真冲突（delta vs absolute、夹爪正负）时改串只是藏起冲突，要在 MS 里做转换并停下确认")


async def check_inference(args: argparse.Namespace, report: Report, info: dict) -> None:
    """同一连接连跑两个 episode：首帧预算、逐步动作合法、第二个 episode 不受第一个影响。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout`、`steps`、`first_frame_budget`、
            `obs_spec` 与 `image_format`。
        report: 记账对象；某一步收发异常时记 FAIL 并结束本用例。
        info: `check_hello()` 返回的 HELLO payload，用于推期望动作维度。
    """
    dim = spec_dim(info)
    rng = np.random.default_rng(0)
    budget = args.first_frame_budget
    firsts = {}
    async with MsClient(args.url, args.timeout, args.token) as cli:
        await cli.hello()
        for ep in range(2):
            await start_episode(cli, f"selfcheck-ep{ep}")
            for step in range(args.steps):
                obs = build_obs(args.obs_spec, args.image_format, rng)
                tag = f"ep{ep}.step{step}"
                try:
                    msg, elapsed = await act(cli, obs, max(budget, args.timeout))
                except Exception as exc:
                    report.add(tag, False, f"{type(exc).__name__}: {exc}")
                    return
                got = assert_action(msg, report, tag, dim)
                dim = dim or got
                report.note(f"{tag} {elapsed:.2f}s")
                if step == 0:
                    firsts[ep] = (msg.get("payload") or {}).get("actions")
                    if ep == 0:
                        report.add("first_frame.budget", elapsed < budget,
                                   f"{elapsed:.2f}s（预算 {budget}s：/config 200 后平台立刻发首帧）")
            await end_episode(cli)
    report.add("episode2.ok", 1 in firsts, "第二个 episode 跑完且无异常")
    # 两帧都真拿到 ACTION 才比：回了 ERROR 时 firsts[ep] 是 None，
    # np.asarray(None) 是 object dtype 的 0-d 数组（.size 为 1，骗过 size 守卫），
    # 送进 allclose 会以 isfinite 的 TypeError 让本用例自身崩掉，
    # 掩盖掉「每帧都 ERROR」这个真实失败形态。
    if firsts.get(0) is not None and firsts.get(1) is not None:
        a0, a1 = np.asarray(firsts[0]), np.asarray(firsts[1])
        if a0.shape == a1.shape and a0.size:
            same = bool(np.allclose(a0, a1))
            report.note("两 episode 首步动作" + ("完全相同（确定性模型正常；有状态模型说明 EPISODE_START 已清状态）"
                                                if same else "不同（随机采样正常；否则怀疑状态未按 session 清空）"))
    else:
        report.note("跳过两 episode 首步动作比较：至少一个 episode 的首帧未返回 ACTION（见上面的 FAIL 项）")


async def check_vector_step(args: argparse.Namespace, report: Report, info: dict) -> None:
    """`VECTOR_STEP` / `VECTOR_RESULT`：robodojo / ocbench 的**硬门禁**，缺了连一个 episode 都不跑。

    v0.9.0 的 `_run_vector` 开头就是
    `if not conn.server_info["capabilities"].get("vector_step"): raise RuntimeError(...)`；
    benchmark 侧 `hard_exit = True` 把这个异常吞掉后 `os._exit(1)`，所以线上失败形态是
    「提交日志只有 exit code 1 与 0 episode、没有任何 traceback；MS 日志只有 connect→close」。

    非 vector benchmark 不声明 `capabilities` 才是对的（见 `references/协议自检.md`）。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout`、`benchmark`、`obs_spec` 与 `image_format`。
        report: 记账对象。
        info: `check_hello()` 返回的 HELLO payload，取其 `capabilities` / `benchmark` / `action_spec`。
    """
    declared = bool((info.get("capabilities") or {}).get("vector_step"))
    bench = str(args.benchmark or info.get("benchmark") or "").strip().lower()
    if not declared:
        required = bench in VECTOR_BENCHMARKS
        report.add("vector_step.capability", not required,
                   f"HELLO 未声明 capabilities.vector_step（benchmark={bench or '未知'}）。"
                   + (f"{bench} 是 VectorStepBenchmark：v0.9.0 在 _run_vector 开头直接 raise RuntimeError，"
                      "benchmark 侧 hard_exit 吞掉异常后 os._exit(1)——线上只看到 exit code 1 与 0 episode。"
                      "形态 B 必须声明 vector_step 并实现 VECTOR_STEP/VECTOR_RESULT"
                      if required else "非 vector benchmark 时不声明才是对的"))
        return
    report.add("vector_step.capability", True,
               f"HELLO 声明 capabilities.vector_step（benchmark={bench or '未知'}）")

    dim = spec_dim(info)
    rng = np.random.default_rng(3)
    async with MsClient(args.url, args.timeout, args.token) as cli:
        await cli.hello()
        start = {"task": {"name": "selfcheck-vector"}, "recording": {"eval_id": "selfcheck"}}
        payload = {"ends": [], "starts": [[0, start]], "slots": [0],
                   "observations": build_batch(args.obs_spec, args.image_format, rng, 1)}
        try:
            msg, elapsed = await vector_step(cli, payload)
        except Exception as exc:
            report.add("vector_step.roundtrip", False,
                       f"{type(exc).__name__}: {exc}（服务端不认 vector_step，或迟未回包）")
            return
        if not report.add("vector_step.roundtrip", msg["type"] == MSG_VECTOR_RESULT,
                          f"回包 type={msg['type']}（期望 {MSG_VECTOR_RESULT}），{elapsed:.2f}s"):
            report.note(f"回包 payload: {str(msg.get('payload'))[:300]}")
            return
        report.add("vector_step.seq_echo", msg["seq"] == msg["_req_seq"],
                   f"req={msg['_req_seq']} resp={msg['seq']}")
        reply = msg.get("payload") or {}
        if reply.get("errors"):
            report.note(f"回包 errors={reply['errors']}（失败的 slot 不出现在 actions 行里）")
        actions = reply.get("actions") or {}
        rows = batch_rows(actions)
        report.add("vector_step.rows", rows == 1,
                   f"actions 行数={rows}（slots=1 时应为 1，且不含失败的 slot）")
        arr = actions.get("actions") if isinstance(actions, dict) else actions
        if arr is None:
            report.add("vector_step.actions", False, f"回包里没有动作列：{str(reply)[:300]}")
            return
        arr = np.asarray(arr)
        want = dim or int(args.obs_spec.get("states") or 0)
        report.add("vector_step.actions_shape", arr.ndim == 2 and arr.shape[-1] == want,
                   f"actions shape={arr.shape}（应为 (1, {want})：行=槽位，列=动作维）")
        report.add("vector_step.actions_finite", bool(np.isfinite(arr).all()), f"dtype={arr.dtype}")
        try:
            msg, _ = await vector_step(cli, {"ends": [[0, {"success": False}]], "starts": [],
                                            "slots": [], "observations": {}})
        except Exception as exc:
            report.add("vector_step.end", False, f"{type(exc).__name__}: {exc}")
            return
        left = batch_rows((msg.get("payload") or {}).get("actions") or {})
        report.add("vector_step.end", msg["type"] == MSG_VECTOR_RESULT and left is None,
                   "ends 后回空 actions" if left is None
                   else f"ends 后仍有 {left} 行动作（槽位没被释放）")


async def check_image_formats(args: argparse.Namespace, report: Report, info: dict) -> None:
    """四种图像编码都要能被服务端解：`__ndarray__` 与 `__image__` 的 raw/png/jpeg。

    harness 侧由 `set_image_format()` 决定发哪种，服务端不能只支持一种。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout` 与 `obs_spec`。
        report: 记账对象；缺 pillow 或有非 3 通道图像时对应编码只记 INFO 跳过。
        info: `check_hello()` 返回的 HELLO payload，用于推期望动作维度。
    """
    dim = spec_dim(info)
    rng = np.random.default_rng(1)
    for fmt in ("ndarray", "raw", "png", "jpeg"):
        if fmt == "jpeg" and any(tuple(s)[-1] != 3 for s in (args.obs_spec.get("images") or {}).values()):
            report.note("跳过 jpeg：观测里有非 3 通道图像，JPEG 只支持 RGB")
            continue
        try:
            obs = build_obs(args.obs_spec, fmt, rng)
        except MissingDependency as exc:
            report.note(f"跳过 {fmt}：{exc}。装 pillow 后重跑才能覆盖该编码")
            continue
        except Exception as exc:
            report.add(f"image.{fmt}", False, f"客户端编码失败: {exc}")
            continue
        async with MsClient(args.url, args.timeout, args.token) as cli:
            await cli.hello()
            await start_episode(cli, f"selfcheck-img-{fmt}")
            try:
                msg, _ = await act(cli, obs)
            except Exception as exc:
                report.add(f"image.{fmt}", False, f"{type(exc).__name__}: {exc}")
                continue
            assert_action(msg, report, f"image.{fmt}", dim)
            await end_episode(cli)


async def check_error_resilience(args: argparse.Namespace, report: Report, info: dict) -> None:
    """推理异常必须回 ERROR 且**不关连接、不返零动作**，之后还能继续正常推理。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout`、`obs_spec` 与 `image_format`。
        report: 记账对象；畸形观测导致断连或之后无法恢复推理时记 FAIL。
        info: `check_hello()` 返回的 HELLO payload，用于推期望动作维度。
    """
    dim = spec_dim(info)
    rng = np.random.default_rng(2)
    bad = {"images": {}, "task_description": args.obs_spec.get("task_description", "")}
    for key in (args.obs_spec.get("images") or {}):
        bad["images"][key] = rng.integers(0, 255, (7, 5, 3), dtype=np.uint8)   # 故意错分辨率
    # state 照规格给合法值：只让图像畸形，否则服务先被 state 校验拦住，
    # 这条用例就变成了「测 state 必填」而不是「测畸形观测的韧性」。
    states = args.obs_spec.get("states")
    if states:
        bad[args.obs_spec.get("state_key") or "state"] = rng.standard_normal(
            int(states)).astype(np.float32)
    async with MsClient(args.url, args.timeout, args.token) as cli:
        await cli.hello()
        await start_episode(cli, "selfcheck-error")
        try:
            msg, _ = await act(cli, bad)
        except Exception as exc:
            report.add("error.no_disconnect", False, f"畸形观测把连接搞断了: {type(exc).__name__}: {exc}")
            return
        kind = msg["type"]
        report.note(f"畸形观测（图像 7x5x3）回 {kind}: {str(msg.get('payload'))[:200]}")
        report.add("error.kind", kind in (MSG_ERROR, MSG_ACTION),
                   f"回包 type={kind}（期望 error；若模型能容忍该形状则 action 也可接受）")
        report.add("error.no_disconnect", cli.open, "回 ERROR 后连接仍开（不许关连接）")
        if not cli.open:
            return
        try:
            msg, _ = await act(cli, build_obs(args.obs_spec, args.image_format, rng))
        except Exception as exc:
            report.add("error.recover", False, f"ERROR 后无法继续推理: {type(exc).__name__}: {exc}")
            return
        report.add("error.recover", msg["type"] == MSG_ACTION, f"ERROR 后下一帧 type={msg['type']}")
        assert_action(msg, report, "error.next", dim)


async def check_no_episode_context(args: argparse.Namespace, report: Report, info: dict) -> None:
    """没有 EPISODE_START 就直接发观测时，服务端不许崩（回 ACTION 或 ERROR 都算过）。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout`、`obs_spec` 与 `image_format`。
        report: 记账对象；收发异常（服务被打挂）时记 FAIL。
        info: `check_hello()` 返回的 HELLO payload；本用例不用，仅为与其他用例同签名。
    """
    rng = np.random.default_rng(3)
    async with MsClient(args.url, args.timeout, args.token) as cli:
        await cli.hello()
        try:
            msg, _ = await act(cli, build_obs(args.obs_spec, args.image_format, rng))
        except Exception as exc:
            report.add("no_episode.survives", False,
                       f"无 episode 上下文直接发观测把服务打挂: {type(exc).__name__}: {exc}")
            return
        report.add("no_episode.survives", msg["type"] in (MSG_ACTION, MSG_ERROR),
                   f"回 type={msg['type']}，连接仍开={cli.open}")


async def check_session_isolation(args: argparse.Namespace, report: Report, info: dict) -> None:
    """两条并发连接各跑一个 episode：per-episode 状态必须按 session 分桶。

    线上多个 shard 共享同一台 MS，分桶错了会串状态——那是「有分数但分数是错的」。

    Args:
        args: `parse_args()` 的结果，用到 `url`、`timeout`、`obs_spec` 与 `image_format`。
        report: 记账对象；任一连接异常时记该 session FAIL。
        info: `check_hello()` 返回的 HELLO payload，用于推期望动作维度。
    """
    dim = spec_dim(info)

    async def one(idx: int) -> dict:
        """单条连接跑一帧，返回回包。

        Args:
            idx: 连接编号，决定随机种子（10 + idx）与 `recording.sid`。

        Returns:
            dict: 该连接首帧观测的回包信封（ACTION 或 ERROR）。

        Raises:
            Exception: 建连或收发失败时原样抛出，由 `asyncio.gather(return_exceptions=True)` 收集。
        """
        rng = np.random.default_rng(10 + idx)
        async with MsClient(args.url, args.timeout, args.token) as cli:
            await cli.hello()
            await start_episode(cli, f"selfcheck-sess{idx}", sid=f"selfcheck-sid-{idx}")
            msg, _ = await act(cli, build_obs(args.obs_spec, args.image_format, rng))
            await end_episode(cli)
            return msg

    results = await asyncio.gather(one(0), one(1), return_exceptions=True)
    for idx, res in enumerate(results):
        if isinstance(res, BaseException):
            report.add(f"session{idx}.ok", False, f"{type(res).__name__}: {res}")
            continue
        report.add(f"session{idx}.ok", True, "并发连接各自拿到回包")
        assert_action(res, report, f"session{idx}", dim)


async def check_token_format(args: argparse.Namespace, report: Report) -> None:
    """Token 与地址格式：按平台创建自建推理服务时的校验规则逐条核对，不过的平台直接 400。

    Args:
        args: `parse_args()` 的结果，用到 `token`、`token_env`、`require_auth` 与 `url`。
        report: 记账对象；未设 Token 时按 `require_auth` 决定 FAIL 或跳过。
    """
    if not args.token:
        if args.require_auth:
            report.add("token.provided", False, f"--require-auth 要求设置环境变量 {args.token_env}")
        else:
            report.note(f"未设置 {args.token_env}，跳过鉴权用例（MS 不鉴权时正常）")
        return
    token = args.token
    report.add("token.no_bearer_prefix", not token.lower().startswith("bearer "),
               "Token 只填原文，不带 `Bearer ` 前缀（客户端自己拼）")
    size = len(token.encode())
    report.add("token.length", TOKEN_MIN_BYTES <= size <= TOKEN_MAX_BYTES,
               f"{size} 字节，须在 [{TOKEN_MIN_BYTES}, {TOKEN_MAX_BYTES}]")
    report.add("token.charset", bool(TOKEN_RE.match(token)),
               "字符集限 RFC 6750 b64token `[A-Za-z0-9-._~+/]+=*`，首尾空白与换行也算违规")
    parts = urllib.parse.urlsplit(args.url)
    report.add("url.scheme", parts.scheme.lower() in ("ws", "wss"), f"scheme={parts.scheme!r}，须为 ws / wss")
    report.add("url.no_userinfo_fragment", "@" not in parts.netloc and "#" not in args.url,
               "地址不得带 userinfo 或 fragment")
    keys = sorted({k for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() in CREDENTIAL_QUERY_KEYS})
    report.add("url.no_query_credentials", not keys,
               "带 Token 时 query 不得再带 token / access_token / auth / key" + (f"，命中 {keys}" if keys else ""))


async def ws_handshake_status(url: str, token: str, timeout: float) -> int:
    """发一次 WS 握手后立即关闭，返回握手的 HTTP 状态码（101 表示握手成功）。

    与平台探活同口径：只握手不发 HELLO。

    Args:
        url: MS 的 WebSocket 地址（含 path 与 query）。
        token: Bearer Token，空串表示不带鉴权头。
        timeout: 握手超时秒数。

    Returns:
        int: 101，或服务端拒绝握手时回的状态码。

    Raises:
        Exception: 连接层失败（拒连、超时）等拿不到状态码的错误原样抛出。
    """
    kwargs = {_HEADERS_KWARG: auth_headers(token)} if token else {}
    try:
        async with websockets.connect(url, compression=None, max_size=None, ping_interval=None,
                                      open_timeout=timeout, **kwargs):
            return 101
    except Exception as exc:
        # websockets ≥14 是 InvalidStatus(.response.status_code)，旧版是 InvalidStatusCode(.status_code)
        code = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
        if code:
            return int(code)
        raise


async def check_auth(args: argparse.Namespace, report: Report) -> None:
    """Bearer 鉴权：按平台自建推理服务的探活顺序走一遍，再验不带 / 带错 Token 被拒。

    平台探活每一步都带 Token：`GET /health`，非 200 再 `GET /config`，二者之一 200；
    随后对完整地址发 WS 握手，只认 101。401/403 判为 `inference_auth_rejected` 并立即放弃，
    5xx 判为未就绪继续重试，其余非 101 判为握手被拒。

    Args:
        args: `parse_args()` 的结果，用到 `token`、`url`、`http` 与 `http_timeout`。
        report: 记账对象；未设 Token 时直接返回（由 `token_format` 用例说明）。
    """
    if not args.token:
        return
    probes = []
    for path in ("/health", "/config"):
        status, _ = http_get(f"{args.http}{path}", args.http_timeout, args.token)
        probes.append(f"{path}→{status or '连接失败'}")
        if status == 200:
            break
    report.add("auth.http_probe", status == 200, "带 Token 的探活 " + "，".join(probes) + "（须有一个 200）")
    status, _ = http_get(f"{args.http}/config", args.http_timeout)
    report.note(f"不带 Token 的 GET /config → {status or '连接失败'}（平台探活总带 Token，放行或拒绝都可）")

    async def handshake(tag: str, token: str) -> typing.Optional[int]:
        """握一次手并把连接层异常记成 FAIL。

        Args:
            tag: 断言名。
            token: 本次握手用的 Token。

        Returns:
            typing.Optional[int]: 状态码；连接层失败时为 None。
        """
        try:
            return await ws_handshake_status(args.url, token, args.http_timeout)
        except Exception as exc:
            report.add(tag, False, f"握手连接层失败 {type(exc).__name__}: {exc}")
            return None

    code = await handshake("auth.ws_with_token", args.token)
    if code is not None:
        report.add("auth.ws_with_token", code == 101, f"带正确 Token 握手 → {code}（须 101）")
    wrong = "selfcheck-wrong-" + os.urandom(16).hex()
    for tag, token, desc in (("auth.reject_missing", "", "不带 Token"), ("auth.reject_wrong", wrong, "带错 Token")):
        code = await handshake(tag, token)
        if code is not None:
            report.add(tag, code in AUTH_REJECT_STATUSES,
                       f"{desc} 握手 → {code}（须 401/403；101 表示 MS 没校验 Token，"
                       "5xx 会被平台当成未就绪反复重试到 attach 超时）")


CHECKS = (
    ("obs_spec", check_obs_spec, False),
    ("token_format", check_token_format, False),
    ("config", check_config, False),
    ("auth", check_auth, False),
    ("hello", check_hello, False),
    ("action_spec", check_action_spec, True),
    ("inference", check_inference, True),
    ("vector_step", check_vector_step, True),
    ("image_formats", check_image_formats, True),
    ("error_resilience", check_error_resilience, True),
    ("no_episode_context", check_no_episode_context, True),
    ("session_isolation", check_session_isolation, True),
)

def http_base(ws_url: str) -> str:
    """`ws://host:port/...` → `http://host:port`（`/config` 与 WS 同端口）。

    Args:
        ws_url: MS 的 WebSocket 地址；不带 scheme 时按 http 处理。

    Returns:
        str: 去掉路径的 HTTP 基址，`ws` → `http`、`wss` → `https`。
    """
    parts = urllib.parse.urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme or "http")
    return f"{scheme}://{parts.netloc or parts.path}"


def parse_args(argv: list) -> argparse.Namespace:
    """解析命令行。

    Args:
        argv: 命令行参数列表（不含程序名），通常为 `sys.argv[1:]`。

    Returns:
        argparse.Namespace: 解析结果；`http` 已按 `--url` 补全，`obs_spec_path` 保留原始路径，
        `obs_spec` 替换为加载后的 dict（未给时为 `DEFAULT_OBS_SPEC` 的副本）。

    Raises:
        SystemExit: `--obs-spec` 文件读不到、不是合法 JSON 或顶层不是对象；
            以及 argparse 遇到 `--help` 或非法参数时。
    """
    p = argparse.ArgumentParser(description="MS 协议自检（用户自定义镜像）")
    p.add_argument("--url", default="ws://127.0.0.1:18000", help="MS 的 WebSocket 地址")
    p.add_argument("--http", default="", help="HTTP 基址，默认由 --url 推导")
    p.add_argument("--obs-spec", default="",
                   help="目标 benchmark 的真实观测 JSON 文件。**不给会让 obs_spec 用例 FAIL**："
                        "按 DEFAULT_OBS_SPEC 的假键名自检只能自洽通过，上平台静默错分数")
    p.add_argument("--image-format", default="png", choices=("ndarray", "raw", "png", "jpeg"),
                   help="主用例的图像编码（harness 默认 png）")
    p.add_argument("--steps", type=int, default=3, help="每个 episode 的步数")
    p.add_argument("--benchmark", default="",
                   help="目标 benchmark 名（robodojo / robotwin / ...）。不给时取 HELLO 回包的 benchmark 字段。"
                        "robodojo、ocbench 是 vector 协议，未声明 capabilities.vector_step 直接判 FAIL；"
                        "同时决定 action_spec 用例拿哪一份期望 format/dims 来比对（内置 8 个常用 benchmark，"
                        "表外名字只提示人工核、不判 FAIL）")
    p.add_argument("--bench-param", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="提交评测时该 benchmark 的构造参数，可重复或逗号分隔（robotwin: action_type=qpos|ee；"
                        "calvin: absolute_action=true|false）。不传只打提示；传了就把 action_spec 的候选收窄到"
                        "实际那一套——robotwin 默认 qpos 时 16D 的 ee_pose 会被静默截断成 14、不报错")
    p.add_argument("--first-frame-budget", type=float, default=30.0, help="首帧耗时预算（秒）")
    p.add_argument("--timeout", type=float, default=120.0, help="单次 recv 超时（秒）")
    p.add_argument("--http-timeout", type=float, default=10.0, help="/config 超时（秒）")
    p.add_argument("--skip", default="", help="跳过的用例名，逗号分隔")
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                   help="从该环境变量读 Bearer Token（Token 不接受命令行传值）")
    p.add_argument("--require-auth", action="store_true",
                   help="MS 要按 Bearer 鉴权：未设 Token 时判 FAIL 而不是跳过")
    p.add_argument("--list", action="store_true", help="只列用例名")
    args = p.parse_args(argv)
    args.http = args.http or http_base(args.url)
    args.token = os.environ.get(args.token_env, "")
    args.obs_spec_path = args.obs_spec
    if args.obs_spec:
        # 顶层类型在加载处一次性校验：否则 [1,2,3] / "abc" / 5 这类合法 JSON 会让
        # 后面的 .get() 抛 AttributeError，被记成 hello.crashed（'list' object has
        # no attribute 'get'），且 info 不落盘、依赖 observation_spec 的用例全部连带失效，
        # 报出来的症状与「obs.json 格式错误」这个真实原因相去甚远。
        try:
            with open(args.obs_spec, encoding="utf-8") as fh:
                spec = json.load(fh)
        except OSError as exc:
            raise SystemExit(f"--obs-spec {args.obs_spec} 读不到：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--obs-spec {args.obs_spec} 不是合法 JSON：{exc}") from exc
        if not isinstance(spec, dict):
            raise SystemExit(
                f"--obs-spec {args.obs_spec} 顶层必须是 JSON 对象"
                f"（形如 {{\"images\": {{...}}, \"states\": 14, \"task_description\": \"...\"}}），"
                f"实际是 {type(spec).__name__}")
        args.obs_spec = spec
    else:
        args.obs_spec = dict(DEFAULT_OBS_SPEC)
    return args


async def run(args: argparse.Namespace, report: Report) -> None:
    """按序跑全部用例，单个用例异常不中断后续。

    Args:
        args: `parse_args()` 的结果，`skip` 中列出的用例会被跳过。
        report: 记账对象；用例自身抛异常时记一条 `<name>.crashed` FAIL。
    """
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    info = {}
    for name, fn, needs_info in CHECKS:
        if name in skip:
            report.note(f"跳过用例 {name}")
            continue
        print(f"\n── {name} " + "─" * (56 - len(name)), flush=True)
        try:
            res = await (fn(args, report, info) if needs_info else fn(args, report))
        except Exception as exc:
            report.add(f"{name}.crashed", False, f"用例自身异常 {type(exc).__name__}: {exc}")
            continue
        if name == "hello":
            info = res or {}


def main() -> int:
    """入口。

    Returns:
        int: 进程退出码；`--list` 或全部用例通过时为 0，有失败项时为 1。
    """
    args = parse_args(sys.argv[1:])
    if args.list:
        print("\n".join(name for name, _, _ in CHECKS))
        return 0
    print(f"目标 {args.url} / {args.http}\n观测 {json.dumps(args.obs_spec, ensure_ascii=False)}\n"
          f"鉴权 {'Bearer ***（' + args.token_env + '）' if args.token else '无'}", flush=True)
    report = Report(secrets=(args.token,))
    asyncio.run(run(args, report))
    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
