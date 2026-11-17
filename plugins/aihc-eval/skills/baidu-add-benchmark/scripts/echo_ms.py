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
"""冒烟用的零动作模型服务：直接用上游 ``vla-eval test`` 的 echo server，每步返回 np.zeros(action_dim)。

v0.9.0 的 echo server 由 ``vla_eval.cli.smoke._make_echo_server`` 构造，``vla-eval test`` 用它配
``serve_async`` 起服务、再拉 docker 跑 benchmark。这里复用同一个工厂与 ``serve_async``，只去掉 docker，
保证冒烟与上游同一实现。工厂是私有符号，升级 harness 后不存在时直接报错退出，不自行实现兜底。

用法：
    python echo_ms.py --action-dim 7 [--host 127.0.0.1] [--port 0] [--port-file <path>]

``--port 0`` 绑空闲端口；就绪后 stdout 打一行 ``ECHO_MS_READY ws://<host>:<port>``，
给了 ``--port-file`` 时同时把端口写进该文件。进程前台阻塞，Ctrl-C 或 kill 结束。
退出码：被信号结束时由解释器决定；参数非法返回 2；harness 里找不到 echo server 工厂返回 3。
"""

from __future__ import annotations

import argparse
import functools
import sys

import anyio
from vla_eval.cli import smoke
from vla_eval.model_servers import serve as ms_serve


def make_server(action_dim: int):
    """用上游工厂构造 echo server。

    Args:
        action_dim: 动作维度，须与目标模型一致。

    Returns:
        上游 ``_EchoModelServer`` 实例（``PredictModelServer`` 子类）。

    Raises:
        RuntimeError: 当前 harness 没有 ``vla_eval.cli.smoke._make_echo_server``。
    """
    factory = getattr(smoke, "_make_echo_server", None)
    if factory is None:
        raise RuntimeError("vla_eval.cli.smoke._make_echo_server 不存在：harness 版本不是 v0.9.0，"
                           "对照新版本的 vla-eval test 实现更新本脚本")
    return factory(action_dim)


def parse_args(argv):
    """解析命令行参数。

    Args:
        argv: 参数列表（不含程序名），None 表示取 sys.argv[1:]。

    Returns:
        argparse.Namespace，含 action_dim、host、port、port_file 四个字段。

    Raises:
        SystemExit: 参数非法时 argparse 打印用法并以退出码 2 结束。
    """
    parser = argparse.ArgumentParser(description="零动作 echo 模型服务（冒烟用）")
    parser.add_argument("--action-dim", type=int, default=7, help="动作维度，默认 7")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示绑空闲端口")
    parser.add_argument("--port-file", default=None, help="就绪后把端口写进该文件")
    args = parser.parse_args(argv)
    if args.action_dim <= 0:
        parser.error("--action-dim 必须是正整数")
    return args


async def _run(args, server) -> None:
    """起服务，就绪后打印地址并写端口文件，然后一直阻塞。

    Args:
        args: parse_args 的返回值。
        server: make_server 构造出的 echo server 实例。

    Returns:
        None。函数被取消或服务退出时返回。
    """
    async with anyio.create_task_group() as tg:
        start = functools.partial(ms_serve.serve_async, server, args.host, args.port)
        port = await tg.start(start)
        if args.port_file is not None:
            with open(args.port_file, "w") as f:
                f.write(str(port))
        print("ECHO_MS_READY ws://%s:%d" % (args.host, port), flush=True)


def main(argv=None) -> int:
    """进程入口：解析参数、构造 echo server 并阻塞运行。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        进程退出码：0 正常结束或被 Ctrl-C 打断，3 harness 里找不到 echo server 工厂。
    """
    args = parse_args(argv)
    try:
        server = make_server(args.action_dim)
    except RuntimeError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3
    try:
        anyio.run(_run, args, server)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
