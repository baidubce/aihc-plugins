#!/usr/bin/env python3
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

"""插件版本检查：看 GitHub 公开插件市场上这个插件有没有更新的版本。

随对外版插件交付（scripts/version_check.py），读同目录上一级的 references/release.json
（插件名、当前版本、公开仓库）。从 raw.githubusercontent.com 读仓库 main 分支上插件的
.claude-plugin/plugin.json，比较版本号。只读，不调用模型。

结果缓存在 ~/.cache/knowship/check/<插件名>.json，10 小时内不重复查（--refresh 强制重查）。

用法：python3 <skill目录>/scripts/version_check.py [--refresh]
输出：第一行是给人看的结论，第二行是 JSON。
退出码：0 已是最新，1 有新版本，3 查不了（网络不通等）。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

RELEASE_FILE = Path(__file__).resolve().parents[1] / "references" / "release.json"
CACHE_HOURS = 10
RAW = "https://raw.githubusercontent.com/{repo}/main/plugins/{plugin}/.claude-plugin/plugin.json"
EXIT_OK, EXIT_NEWER, EXIT_UNKNOWN = 0, 1, 3


def parse_version(text):
    """把 1.2.3 这样的版本号转成可比较的元组，不认识的部分记 0。

    Args:
        text: 版本号。

    Returns:
        tuple: 数字元组。
    """
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", str(text or "0"))[:3])


def latest(repo, plugin, timeout=10):
    """读公开仓库 main 分支上插件的版本号。

    Args:
        repo: owner/name。
        plugin: 插件名。
        timeout: 超时秒数。

    Returns:
        str: 版本号。

    Raises:
        OSError: 网络不通或返回内容不对。
    """
    url = RAW.format(repo=repo, plugin=plugin)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as e:
        raise OSError(str(e)) from e
    if not data.get("version"):
        raise OSError("plugin.json 里没有 version")
    return data["version"]


def compare(release, newest):
    """比较当前版本和公开市场上的版本。

    Args:
        release: release.json 内容。
        newest: 公开市场上的版本号。

    Returns:
        dict: {"status": fresh / newer, "text", "current", "latest"}。
    """
    cur = release["version"]
    if parse_version(newest) > parse_version(cur):
        return {"status": "newer", "current": cur, "latest": newest,
                "text": f"版本检查：{release['plugin']} 有新版本 {newest}（当前 {cur}），"
                        f"可运行 /plugin marketplace update {release.get('marketplace') or 'aihc-plugins'} 后重新安装"}
    return {"status": "fresh", "current": cur, "latest": newest,
            "text": f"版本检查：{release['plugin']} {cur} 已是最新"}


def cache_path(name):
    """缓存文件位置。

    Args:
        name: 插件名。

    Returns:
        Path: 缓存文件。
    """
    root = os.environ.get("KNOWSHIP_CHECK_CACHE") or Path.home() / ".cache" / "knowship" / "check"
    return Path(root) / f"{name}.json"


def load_cache(path, key, now):
    """读未过期的缓存结果。

    Args:
        path: 缓存文件。
        key: release.json 的 sha256。
        now: 当前时间。

    Returns:
        dict: 缓存内容；没有或过期时为 None。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.datetime.fromisoformat(data["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if data.get("key") != key or now - at > datetime.timedelta(hours=CACHE_HOURS) or at > now:
        return None
    return data


def main(argv=None):
    """读 release.json、查公开市场（或用缓存）、输出结论。

    Args:
        argv: 命令行参数。

    Returns:
        int: 退出码（0 最新，1 有新版本，3 查不了）。
    """
    ap = argparse.ArgumentParser(description="检查公开插件市场上有没有新版本")
    ap.add_argument("--refresh", action="store_true", help="不用缓存，重新查")
    ap.add_argument("--release", default=str(RELEASE_FILE), help="缺省 references/release.json")
    a = ap.parse_args(argv)
    try:
        raw = Path(a.release).read_bytes()
        release = json.loads(raw.decode("utf-8"))
        name = release["plugin"]
    except (OSError, ValueError, KeyError) as e:
        print(f"版本检查：查不了，读不到 release.json（{e}）")
        print(json.dumps({"status": "unknown"}, ensure_ascii=False))
        return EXIT_UNKNOWN
    key = hashlib.sha256(raw).hexdigest()
    now = datetime.datetime.now().astimezone()
    path = cache_path(name)
    cached = None if a.refresh else load_cache(path, key, now)
    if cached:
        result = cached["result"]
    else:
        try:
            result = compare(release, latest(release.get("repo") or "baidubce/aihc-plugins", name))
        except OSError as e:
            result = {"status": "unknown", "current": release.get("version"), "latest": None,
                      "text": f"版本检查：查不了公开市场上的版本（{str(e)[:120]}）"}
        if result["status"] != "unknown":
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"key": key, "checked_at": now.isoformat(timespec="seconds"),
                                            "result": result}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
    print(result["text"])
    print(json.dumps({k: result.get(k) for k in ("status", "current", "latest")}, ensure_ascii=False))
    return {"fresh": EXIT_OK, "newer": EXIT_NEWER}.get(result["status"], EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
