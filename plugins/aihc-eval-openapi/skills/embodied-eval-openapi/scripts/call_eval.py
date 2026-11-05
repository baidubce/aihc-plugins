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

"""调用百舸具身评测 OpenAPI 的示例脚本（bce-auth-v1 签名），只依赖 Python 3 标准库。

用法：
    export BCE_AK=<AccessKeyId> BCE_SK=<SecretAccessKey>
    export AIHC_ENDPOINT=aihc.su.baidubce.com      # 服务域名，按地域填写；不设时默认北京 aihc.bj.baidubce.com
    python3 call_eval.py GET DescribeEmbodiedEvalModels source=public
    python3 call_eval.py GET DescribeEmbodiedEvals pageNumber=1 pageSize=10
    python3 call_eval.py POST CreateEmbodiedEval clientToken=$(uuidgen) --body create.json

参数：METHOD（GET / POST）、ACTION（接口名）、任意个 key=value 形式的 query 参数，
只有 CreateEmbodiedEval 需要 --body 指定 JSON 请求体文件。
输出：第一行是 HTTP 状态码，之后是返回体（JSON 格式化输出）。
退出码：0 成功；1 HTTP 状态码不是 2xx；2 参数不全或缺少 BCE_AK / BCE_SK；3 网络错误（可重试）。
"""
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "aihc.bj.baidubce.com"


def uri_encode(s, encode_slash=True):
    """按 BCE 规范做 URI 编码：只保留 A-Z a-z 0-9 - _ . ~。

    Args:
        s: 待编码的字符串。
        encode_slash: 是否把 `/` 也编码，路径部分传 False。

    Returns:
        str: 编码后的字符串。
    """
    return urllib.parse.quote(s, safe="-_.~" if encode_slash else "-_.~/")


def sign(ak, sk, method, host, path, params, timestamp, expire=1800):
    """生成 Authorization 头，签名 host 和 x-bce-date 两个请求头。

    Args:
        ak: AccessKeyId。
        sk: SecretAccessKey。
        method: HTTP 方法，大写。
        host: 服务域名。
        path: 请求路径，本接口固定为 `/`。
        params: query 参数字典（含 action）。
        timestamp: UTC 时间，格式 YYYY-MM-DDThh:mm:ssZ，与 x-bce-date 头相同。
        expire: 签名有效期，单位秒。

    Returns:
        str: Authorization 头的值。
    """
    auth_prefix = "bce-auth-v1/%s/%s/%d" % (ak, timestamp, expire)
    signing_key = hmac.new(sk.encode(), auth_prefix.encode(), hashlib.sha256).hexdigest()
    canonical_query = "&".join(sorted(
        "%s=%s" % (uri_encode(k), uri_encode(v)) for k, v in params.items() if k.lower() != "authorization"))
    canonical_headers = "\n".join(sorted([
        "host:" + uri_encode(host.strip()),
        "x-bce-date:" + uri_encode(timestamp),
    ]))
    canonical_request = "\n".join([method, uri_encode(path, False), canonical_query, canonical_headers])
    signature = hmac.new(signing_key.encode(), canonical_request.encode(), hashlib.sha256).hexdigest()
    return "%s/host;x-bce-date/%s" % (auth_prefix, signature)


def call(method, action, params, body=None):
    """调用一个接口。凭据从环境变量 BCE_AK / BCE_SK 读取，服务域名从 AIHC_ENDPOINT 读取。

    Args:
        method: HTTP 方法，GET 或 POST。
        action: 接口名，如 DescribeEmbodiedEvalModels。
        params: 其他 query 参数字典。
        body: 请求体（dict），只有 CreateEmbodiedEval 需要。

    Returns:
        tuple: (HTTP 状态码, 返回体)。返回体是 JSON 时为 dict，否则为原始文本。

    Raises:
        urllib.error.URLError: 网络不通或域名解析失败。
    """
    ak, sk = os.environ["BCE_AK"], os.environ["BCE_SK"]
    host = os.environ.get("AIHC_ENDPOINT", DEFAULT_ENDPOINT)
    params = dict(params, action=action)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {
        "Host": host,
        "x-bce-date": timestamp,
        "version": "v2",
        "Content-Type": "application/json",
        "Authorization": sign(ak, sk, method, host, "/", params, timestamp),
    }
    url = "https://%s/?%s" % (host, urllib.parse.urlencode(params, quote_via=urllib.parse.quote))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def main():
    """命令行入口：METHOD ACTION [key=value ...] [--body file.json]。

    Returns:
        int: 退出码。成功为 0，HTTP 状态码不是 2xx 时为 1，参数不全或缺少凭据时为 2，网络错误为 3。
    """
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    body = None
    if "--body" in args:
        i = args.index("--body")
        with open(args[i + 1]) as f:
            body = json.load(f)
        args = args[:i] + args[i + 2:]
    if len(args) < 2 or any("=" not in a for a in args[2:]):
        print("用法：call_eval.py METHOD ACTION [key=value ...] [--body file.json]，详见 --help", file=sys.stderr)
        return 2
    if not os.environ.get("BCE_AK") or not os.environ.get("BCE_SK"):
        print("缺少环境变量 BCE_AK / BCE_SK（百度智能云 Access Key）", file=sys.stderr)
        return 2
    method, action = args[0].upper(), args[1]
    params = dict(a.split("=", 1) for a in args[2:])
    try:
        status, out = call(method, action, params, body)
    except (urllib.error.URLError, OSError) as e:
        print("网络错误（%s）：检查 AIHC_ENDPOINT 和网络后重试；查询类接口可以直接重试" % e, file=sys.stderr)
        return 3
    print(status)
    print(json.dumps(out, ensure_ascii=False, indent=2) if isinstance(out, dict) else out)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
