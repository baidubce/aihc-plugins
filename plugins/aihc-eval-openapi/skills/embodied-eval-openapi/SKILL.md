---
name: embodied-eval-openapi
description: 百舸（AIHC）具身评测 OpenAPI 技术支持：用随附签名脚本和请求体模板调用 10 个评测接口，指导客户用预置模型、自己的权重或自己的推理镜像创建 LIBERO、RoboTwin 等评测任务，并排查 400/403/404/500 报错、长时间排队、任务失败、分数偏低、回放视频链接失效等问题；客户通过 OpenAPI、Go SDK 或 aihc CLI 使用具身评测时使用。
---

<!-- knowship:check -->
> **开始前先检查版本**：运行 `python3 <skill目录>/scripts/version_check.py`，看公开插件市场上有没有更新的版本（结果缓存 10 小时）。
> - 结论是「有新版本」（退出码 1）：告诉用户新版本号和更新命令，然后照常继续。
> - 结论是「查不了」（退出码 3）：告诉用户这次没法检查版本，然后照常继续。
<!-- /knowship:check -->

# 百舸具身评测 OpenAPI 技术支持

## 适用场景
- 客户想用 OpenAPI 调通具身评测，查看账号下的评测任务。
- 客户要创建评测任务。模型来源有三种：平台预置模型（`public`）、预置模型架构加自己的权重（`customCKPT`）、自己的推理服务镜像（`customImage`）。
- 客户贴来报错或返回体，需要判断原因并给出修正步骤。常见报错包括 400 `InvalidParameter`、403 `IdempotentParameterMismatch`、404 `NotFound` 或 `ResourceNotFound`、500 `InternalError`。
- 任务状态异常：一直 `Scheduled`，或者 `Failed`；也包括 `Succeeded` 但分数明显偏低。
- 查评分、看结果字段、取回放视频播放地址、停止或删除任务。
- 客户偏好用 Go SDK 或 aihc CLI 调用同样的接口。

## 不适用场景
- 百舸资源池、队列、训练任务本身的创建和运维（如扩容、配额、节点故障）。这类问题只说明在百舸控制台查看队列资源或训练任务日志，不要编造其他产品的接口。
- 客户在本地或其他平台自己跑开源评测脚本，不经过本 OpenAPI。
- 涉及他人账号、绕过权限的请求。

## 基本约定（回答时必须遵守）

### 接口只有这 10 个
|接口|方法|用途|
|-|-|-|
|`CreateEmbodiedEval`|POST|创建评测任务，唯一带 JSON 请求体的接口|
|`DescribeEmbodiedEvals`|GET|任务列表|
|`DescribeEmbodiedEval`|GET|任务详情和状态|
|`StopEmbodiedEval`|POST|停止任务|
|`DeleteEmbodiedEval`|POST|删除任务|
|`DescribeEmbodiedEvalResult`|GET|评分、进度、明细|
|`DescribeEmbodiedEvalVideo`|GET|回放视频临时播放地址|
|`DescribeEmbodiedEvalModels`|GET|可评测模型及其评测集、子集|
|`DescribeEmbodiedEvalModel`|GET|模型默认配置、最低硬件、`extraArgsSchema`|
|`DescribeEmbodiedEvalBenchmarks`|GET|模型在各评测集、子集上的默认配置|

不要使用其他接口名（如 `CreateEmbodiedEvalTask`、`ListEvalEpisodes`、`DescribeResourcePools`、`DescribeQueue`、`DescribeJobEvents`），也不要对客户说"接口名需要另行核对"。本技能就是这套接口的说明。

### 调用方式
默认用随附脚本 `<skill目录>/scripts/call_eval.py`。它只依赖 Python 3 标准库，已经实现 bce-auth-v1 签名。不要另写签名脚本，也不要让客户手拼 curl 签名。

```bash
export BCE_AK=<AccessKeyId> BCE_SK=<SecretAccessKey>   # 凭据只从环境变量读
export AIHC_ENDPOINT=aihc.<region>.baidubce.com        # 不设时默认北京，其他地域必须显式设置
python3 <skill目录>/scripts/call_eval.py METHOD ACTION [key=value ...] [--body 请求体.json]
```

- 服务域名：北京 `aihc.bj.baidubce.com`、保定 `aihc.bd.baidubce.com`、苏州 `aihc.su.baidubce.com`、广州 `aihc.gz.baidubce.com`、阳泉 `aihc.yq.baidubce.com`、成都 `aihc.cd.baidubce.com`、香港 `aihc.hk.baidubce.com`。评测任务、资源池、队列属于调用时所在的地域，三者必须同地域。
- 参数位置：`evalId`、`modelName`、`videoId`、分页、过滤条件、`clientToken` 都放在 query，写成 `key=value`。只有 `CreateEmbodiedEval` 用 `--body` 传请求体。不要把 `evalId` 放进请求体，也不传 `accountId`、`region`。
- 方法：Create、Stop、Delete 用 POST，其余接口用 GET。
- 脚本输出的第一行是 HTTP 状态码（200 表示成功），之后是返回体 JSON。退出码 0 表示成功，1 表示状态码不是 2xx，2 表示缺少 `BCE_AK`/`BCE_SK` 或参数不全，3 表示网络错误（查询类接口可以直接重试）。
- 失败返回体固定包含 `requestId`、`code`、`message` 三个字段，`message` 格式为"默认描述:详细原因"。需要升级问题时，请客户提供 `requestId`。
- 时间头 `x-bce-date` 与服务端偏差超过 30 分钟会返回 `RequestExpired`。401 `SignatureDoesNotMatch` 时，检查 AK/SK 是否完整、本机时间是否准确。
- 子用户需要百舸权限：创建、停止、删除需要目标队列的操作权限；元数据接口（Models、Model、Benchmarks）只需要百舸的任一权限。

客户偏好其他方式时，可以改用下面两种：
- Go SDK：`go get github.com/baidubce/bce-sdk-go@v0.9.278`（或更高版本），使用包 `services/aihc/eval`，方法名与接口同名。用 `eval.NewClient(os.Getenv("BCE_AK"), os.Getenv("BCE_SK"), "<服务域名>")` 创建客户端。不传 `ClientToken` 时 SDK 自动生成，内部重试会复用同一个值。
- aihc CLI v1.6.6 及以上：`aihc eval models|model|benchmarks|create -f eval.yaml|list|get|stop|delete|result|video`。`create` 不传 `--client-token` 时会自动生成一个并打印到 stderr。私有镜像密码只能写在 `-f` 指定的文件里。

## 判读步骤

先判断客户属于下面哪类问题，再按对应小节回答。所有命令都写成 `call_eval.py` 的形式，客户自己填的值用占位符（如 `<资源池ID>`、`<队列ID>`、`<evalId>`）。

### 1. 调通验证、查看已有任务
1. 设置 `BCE_AK`、`BCE_SK`，把 `AIHC_ENDPOINT` 设成客户所在地域的域名。
2. 第一条命令：`python3 <skill目录>/scripts/call_eval.py GET DescribeEmbodiedEvals pageNumber=1 pageSize=10`
3. 第一行是 200 就说明签名、地域、权限都通了，返回体里的 `evals[]` 是有权查看的任务。`totalCount` 为 0 也算调通。
4. 列表默认按 `createdAt` 倒序。可以用 `status=Running,Succeeded`、`modelName=`、`keywordType=name keyword=<关键字>` 过滤；`pageSize` 取值 1～100。

### 2. 完整评测流程（预置模型 `public`）
1. 设置环境变量（同上）。
2. 查可选组合：`GET DescribeEmbodiedEvalModels source=public`。返回 `ResourceNotFound` 时按第 7 节处理。
3. 取默认配置：
   - `GET DescribeEmbodiedEvalModel modelName=<模型>`，拿到 `defaultModelConfig`、`minHardware.acceleratorTypes`、`extraArgsSchema`。
   - `GET DescribeEmbodiedEvalBenchmarks modelName=<模型> benchmarkName=<评测集> subBench=<子集>`，拿到 `defaultBenchmarkConfig`、`maxEpisodesPerTask`。
4. 复制模板 `<skill目录>/templates/create-public-libero.json` 为 `create.json`，用默认配置填 `modelConfig` 和 `benchmarkConfigs[]`，再填 `name`、`resourcePoolId`、`queueId`。填写规则：
   - GPU 资源名写成平台资源名，例如 RTX 4090 写 `baidu.com/rtx_4090_cgpu`，其他卡型如 `baidu.com/l20_cgpu`、`baidu.com/a800_80g_cgpu`。卡型必须是队列里实际的卡，并且在 `minHardware.acceleratorTypes` 里；模型和评测集用同一种卡。默认配置里的卡型只是示例，必须替换。
   - 跑哪个子集只由 `subBench` 决定。默认配置的 `params` 里带的 `suite`、`task_name`、`tasks` 要删掉，其他键（如 `seed`）保留或按需修改；`params` 整个不填也可以。
   - `modelOverride` 原样采用推荐值（`ckptPath`、`chunkSize`、`extraArgs`）。OpenVLA 的 `extraArgs.unnorm_key` 必须等于所跑的子集名（如跑 `libero_object` 就填 `libero_object`），取值以 `extraArgsSchema` 的 `enum` 为准。同时保留推荐的 `jpeg_roundtrip`、`center_crop`。不同模型的 `extraArgs` 键不同，不能互相套用。
   - `episodesPerTask`：出正式分数时用推荐值；只想验证流程时可以改小，但要告诉客户分数波动会变大。`episodesPerTask` 不能超过 `maxEpisodesPerTask`（0 表示不限）。模板里的 10 只是示例。
   - 约束：`benchmarkConfigs` 有 1～20 个；`modelConfig.replicas` 必须是 `benchmarkConfigs` 个数的整数倍；`parallelShards` 不超过 `episodesPerTask`；同一评测集可以出现多次，但每次用不同的 `subBench`。
   - 资源池写法：普通资源池填资源池 ID，不传 `resourcePoolType`；全托管（Serverless）资源池见第 8 节。
5. 创建：`python3 <skill目录>/scripts/call_eval.py POST CreateEmbodiedEval clientToken=$(uuidgen) --body create.json`，返回 `evalId`。没有 `uuidgen` 的环境可以用 `python3 -c "import uuid;print(uuid.uuid4())"` 生成。
6. 轮询：每 30～60 秒执行一次 `GET DescribeEmbodiedEval evalId=<evalId>`，直到 `status` 进入终态（`Succeeded`、`Failed`、`ManualTermination`、`CreateFailed`）。
7. 取评分：`GET DescribeEmbodiedEvalResult evalId=<evalId>`，字段位置见第 12 节。开了录像时再取视频，见第 13 节。
8. 不再需要时删除：`POST DeleteEmbodiedEval evalId=<evalId>`。删除不可逆，先让客户确认 `evalId`。

### 3. 用自己的权重（`customCKPT`）
适用于客户在 BOS 上有微调权重、架构与某个预置模型相同的情况。
1. 模板用 `<skill目录>/templates/create-custom-ckpt.json`。
2. `modelConfig.modelType=customCKPT`，`modelName` 选同架构的预置模型（如 OpenVLA 微调权重选 `openvla_7b`），平台会用它的推理镜像加载客户的权重。
3. 顶层 `dataSources` 挂载 BOS：`{"type":"bos","sourcePath":"<bucket>/<prefix>","mountPath":"/mnt/ckpt","options":{"readOnly":true}}`。`sourcePath` 不带 `bos://` 前缀。账号需要对该路径有读权限，建议桶与任务在同一地域。
4. `ckptPath` 写成挂载点下的容器内绝对路径，如 `/mnt/ckpt/<权重目录>`。不以 `/` 开头的值会被当作 Hugging Face 仓库名。`ckptPath` 可以写在 `modelConfig.ckptPath`（对全部评测集生效），也可以写在某个评测集的 `modelOverride.ckptPath`（优先级更高），两处至少填一处。
5. `mountPath` 不能用平台保留路径 `/mnt/aihc-eval` 及其子路径、`/mnt/aihc-eval-result`；每一段只能包含字母、数字、`_`、`-`。
6. 可跑的评测集用 `GET DescribeEmbodiedEvalModels source=custom` 或 `GET DescribeEmbodiedEvalBenchmarks modelName=<同架构模型> source=custom` 查。返回体较大，建议加 `benchmarkName=` 过滤。
7. `extraArgs` 仍要与子集一致（如 OpenVLA 的 `unnorm_key`）。其余规则同第 2 节。

### 4. 用自己的推理服务镜像（`customImage`）
回答时把下面的镜像要求逐条列给客户。平台在创建时无法检查这些项，不满足时任务会失败，或者能跑完但分数不可信。
1. 程序自己从环境变量 `MS_PORT` 读端口，监听 `0.0.0.0:$MS_PORT`。`command` 不经过 shell，按空格切分后直接执行，写在命令里的 `$MS_PORT` 不会被替换。`command` 里写死端口（如 `--port 8000`）时，创建直接返回 400。
2. 在同一端口提供 `GET /config`，返回 200 作为唯一的就绪信号，并且必须在模型加载完、能推理之后才返回 200。否则前几个 episode 会出错，分数不可信。
3. 在同一端口提供 WebSocket 服务，协议与开源 vla-evaluation-harness（https://github.com/allenai/vla-evaluation-harness）的 model server 一致，可以基于其 `vla_eval/model_servers/` 下的实现改造。
4. 从启动到就绪不超过 900 秒，期间进程不能退出；收到 `SIGTERM` 后 30 秒内退出。
5. 镜像内 Ray 版本为 2.49.2，Python 版本为 3.13.12。
6. 不依赖外网和 `HF_ENDPOINT`，不假设 `HF_HOME` 可写；容器的 hostname 要能被解析。
7. 权重放在 BOS，通过 `dataSources` 挂载，并把权重路径写进 `command`。`ckptPath` 不会传给自定义镜像。

请求体要点（模板 `<skill目录>/templates/create-custom-image.json`）：
- `modelType=customImage`，不填 `modelName`。`modelConfig.customImage` 包含 `image`、`command`、`envs`、`imageConfig`。
- 带 `imageConfig` 的私有镜像必须放在 `ccr-registry.baidubce.com` 上。`imageConfig` 的 `username` 和 `password` 要同时填或同时不填。
- 密码只放在 `imageConfig.password`，不要放进 `envs`，因为 `envs` 在任务详情里原样返回。
- `envs` 最多 32 个，不能使用平台保留名（不区分大小写）：`EVAL_SPEC`、`EVAL_SPEC_VERSION`、`EVAL_RESULT_FILE`、`EVAL_PLATFORM_MOUNT`、`EVAL_SRC_MOUNT`、`EVAL_RESULT_MOUNT`、`CUDA_VISIBLE_DEVICES`、`PYTHONPATH`、`MS_PORT`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`ALL_PROXY`，以及以 `RAY_` 开头的名称。
- 可跑的评测集用 `source=custom` 查；查评测集默认配置时，`modelName` 填任一预置模型即可。
- 推理协议细节（图像翻转、夹爪符号、动作反归一化）在自定义镜像场景下由客户自己保证。

### 5. clientToken 与 403 `IdempotentParameterMismatch`
- `clientToken` 放在 `CreateEmbodiedEval` 的 query 里。每次新建任务都生成新值（`clientToken=$(uuidgen)`），只有重试**同一次**创建时才传回同一个值。不传时不做幂等。
- 同一账号下，一个 `clientToken` 只会对应一个任务：
  - 参数相同时再次调用，返回第一次创建的那个 `evalId`，不会新建任务。
  - 参数不同时返回 403 `IdempotentParameterMismatch`，新任务没有被创建。
- `clientToken` 没有有效期，只有对应任务被删除或创建失败后才能再用。如果脚本里写死同一个值，从第二个任务起要么拿到第一个任务的 `evalId`，要么报参数冲突。修正方法是把生成 token 的逻辑放到每次创建内部。
- 400 校验失败不会建任务；改好参数后用新的 `clientToken` 重新创建。也不要复用失败任务的旧 token。

### 6. 400 `InvalidParameter`：报错里的字段名对不上
`message` 冒号后的原因来自服务端校验，部分字段用的是内部名称。先翻译成请求体里的字段名，再告诉客户改哪里。不要让客户在请求体里改写成内部名。
|报错里的写法|请求体字段|
|-|-|
|`evalName`|`name`|
|`queue`|`queueId`|
|`resourceType`|`resourcePoolType`|
|`datasources[i]`|`dataSources[i]`|
|`modelConf`|`modelConfig`|
|`modelConf.modelType` 的 1 / 2 / 3|`public` / `customCKPT` / `customImage`|
|`benchmarkConfs[i]`|`benchmarkConfigs[i]`|
|`benchmarkConfs[i].benchmarkResource`|`benchmarkConfigs[i].resources`|
|`benchmarkConfs[i].episodePerTask`|`benchmarkConfigs[i].episodesPerTask`|
|`benchmarkConfs[i].modelConf`|`benchmarkConfigs[i].modelOverride`|
|`evalSpec ... exceeds`|请求展开后过大，减少评测集数量或 `params` 内容|

- `episodesPerTask` 超限：上限是该组合的 `maxEpisodesPerTask`，用 `GET DescribeEmbodiedEvalBenchmarks modelName=<模型> benchmarkName=<评测集> subBench=<子集>` 查（0 表示不限）。报错里的数字只对当前组合有效，不是通用上限。
- `message` 含 `not in compatibility matrix`：模型和子集的组合不在验证范围内。用 `DescribeEmbodiedEvalModels source=public` 确认可选组合；用自己的权重或镜像时改用 `source=custom` 查。
- 报卡型不支持：查 `DescribeEmbodiedEvalModel` 和 `DescribeEmbodiedEvalBenchmarks` 的 `minHardware.acceleratorTypes`，并确认队列里有这种卡。
- 其他常见 400 原因：`replicas` 不是评测集个数的整数倍；`parallelShards` 超过 `episodesPerTask`；挂载点被占用；请求体不是合法 JSON；`params` 不是对象；拿详情重建时 `imageConfig.password` 仍是 `******`。
- 部分评测集按 GPU 卡数分片（如 RoboDojo 要求 `parallelShards` 等于该评测集的 GPU 卡数，CPU、内存、共享内存不低于单分片最低配置乘以分片数）。不满足时 400 的 `message` 会给出应填的值。
- 400 不会建任务。改正后用新的 `clientToken` 重新创建。

### 7. 两种 404，以及元数据接口不可用时怎么拿配置
| 返回 | 含义 | 处理 |
|-|-|-|
| `code=NotFound`，`message` 冒号后有具体原因 | 服务端收到了请求，但资源（任务、模型、评测集、视频）不存在 | 检查 ID 或名称是否写错、任务是否已删除、是否在另一个地域创建 |
| `code=ResourceNotFound`，`message` 只有 `Resource not found.` | 请求在网关层没有对应的接口 | 先确认 `AIHC_ENDPOINT` 是任务所在地域的域名（如苏州应为 `aihc.su.baidubce.com`）。域名正确时，说明该地域暂未开放这个接口。开放状态会随发版变化，先重试一次 |

元数据接口不可用时，按下面的顺序找替代做法：
1. 已知模型时，用 `GET DescribeEmbodiedEvalBenchmarks modelName=<模型>`（不传 `benchmarkName`、`subBench`）列出全部组合；用自己的权重或镜像时加 `source=custom`。返回空列表 `"benchmarks": []` 表示组合不在验证范围内；模型名不存在时返回 404 `NotFound`。
2. 客户没指定模型时，不要替客户拍板。先从 `DescribeEmbodiedEvals` 历史任务里看用过哪些 `modelName`，再结合常见预置模型名（如 `openvla_7b`、`pi05_libero`）用上一步逐个确认，把候选和各自的推荐配置列给客户选择。
3. 元数据接口都不可用时，用 `GET DescribeEmbodiedEvals status=Succeeded` 找同地域、同模型、同评测集的成功任务，再用 `GET DescribeEmbodiedEval evalId=<evalId>` 取详情。详情里的 `modelConfig`、`benchmarkConfigs`、`dataSources` 与创建请求体结构相同，改掉 `name`（需要时再改资源池、队列、`subBench`、`episodesPerTask`）后可以直接作为请求体。自定义镜像任务要重填 `imageConfig.password`，因为详情里显示为 `******`。
4. 没有历史任务时，用 `<skill目录>/templates/` 下的模板按本技能的规则填写。组合或卡型不支持时，创建会返回 400 并在 `message` 里说明原因。

### 8. 创建返回 500 `InternalError`，或使用全托管资源池
- 队列挂在全托管（Serverless）资源池上时，如果 `resourcePoolId` 填了控制台看到的物理池 ID（形如 `aihc-xxxx`）且不传 `resourcePoolType`，创建可能连续返回 500 且没有参数提示，换 `clientToken` 也没用。
- 已验证可用的写法是请求体顶层同时写 `"resourcePoolId": "aihc-serverless"`、`"resourcePoolType": "serverless"`、`"queueId": "<队列ID>"`。普通资源池填该池 ID，不传 `resourcePoolType`。
- 最稳的确认方法：找同一队列上成功过的任务，用 `DescribeEmbodiedEval` 看它的 `resourcePoolId`、`resourcePoolType`、`queueId`，照着写。
- 创建返回 500 时，先核对这三个字段，不要只换 `clientToken` 反复重试。持续 500 时，让客户提供 `requestId` 联系技术支持。

### 9. 任务长时间 `Scheduled`
- `Scheduled` 表示已提交、在排队等资源，不是终态，任务没有挂掉。接口里没有排队原因字段，不要编造。
- 让客户在百舸控制台查看该队列的资源使用，确认所选卡型有足够空闲。同时核对卡型与队列实际的卡一致，以及资源池写法正确（见第 8 节）。
- 不想继续等时：
  1. 先停止或删除旧任务（排队中的任务可以停）：`POST StopEmbodiedEval evalId=<evalId>` 或 `POST DeleteEmbodiedEval evalId=<evalId>`。
  2. 降低 `resources`、`parallelShards`、`replicas` 后，用新的 `clientToken` 重新创建。`replicas` 仍须是评测集个数的整数倍，`parallelShards` 仍不能超过 `episodesPerTask`。

### 10. 任务 `Failed` / `CreateFailed`
- 详情里没有失败原因字段，`DescribeEmbodiedEvalResult` 的 `result` 也可能是 `null`（结果文件还没产出）。
- 用详情里的 `jobId`（对应的百舸分布式训练任务 ID）到百舸控制台找到训练任务，看日志和事件。
- 自定义镜像任务对照第 4 节逐条排查，其他任务对照第 2、3 节的配置规则排查。改好后用新的 `clientToken` 重新创建。
- `CreateFailed` 的任务不出现在列表里（按 `status=CreateFailed` 过滤也是空列表），要用 `DescribeEmbodiedEval` 按 ID 查看。

### 11. 任务 `Succeeded` 但成功率明显偏低
按以下顺序检查，并给出对应命令：
1. `GET DescribeEmbodiedEval evalId=<evalId>`：看 `benchmarkConfigs[].modelOverride.extraArgs` 是否与子集匹配，例如 OpenVLA 的 `unnorm_key` 是否等于所跑的子集名；`jpeg_roundtrip`、`center_crop` 等预处理参数是否与 `DescribeEmbodiedEvalBenchmarks` 的推荐值一致。用预置模型时，最稳的做法是原样采用推荐的 `modelOverride`。
2. `GET DescribeEmbodiedEvalResult evalId=<evalId>`：
   - `result.benchmarks[].status` 为 `partial` 表示部分分片失败，评分只统计成功跑完的分片。
   - 看 `progress.num_errors`、`num_timeouts` 是否不为 0，`completed_episodes` 是否等于 `total_episodes`。
3. 权重：用自己的权重时，确认 `ckptPath` 指向该子集微调过的权重；用预置模型时，确认 `modelOverride.ckptPath` 没有被改成别的子集或基础权重。
4. 开了 `recordVideo` 时，抽看失败 episode 的回放（见第 13 节）。
5. 改正后用新的 `clientToken` 重新创建。`episodesPerTask` 调得很小时，分数波动本身也会变大。

### 12. 结果字段位置
- `DescribeEmbodiedEvalResult` 的顶层只有 `requestId`、`evalId`、`result`，没有任务状态。任务状态看 `DescribeEmbodiedEval` 的 `status`（首字母大写）。
- `result.benchmarks[]` 每个子集一项，字段是下划线风格：
  - `status`：小写，`running`、`succeeded`、`partial`、`failed`、`stopped`。
  - 评分：`metrics.mean_success`（0～1）和 `metrics.num_tasks`。
  - 进度：`progress.completed_episodes`、`total_episodes`、`num_errors`、`num_timeouts`。
  - 实际配置：`config`，其中 `config.params.suite` 显示实际跑的子集。
  - 明细：`details.tasks[]`。
- 任务成功前，`result` 可能是 `null`，也可能是进度快照（`metrics`、`details`、`finished_at` 为 `null`）。以 `Succeeded` 之后取到的结果为准。
- 轮询单个任务用 `DescribeEmbodiedEval`，不要用大 `pageSize` 的列表接口（列表接口会刷新状态，越大越慢）。

### 13. 回放视频播放地址
1. 只有创建时该评测集 `recordVideo=true` 才产出视频。没开录像时，`result` 里没有 `video_summary`，`tasks[]` 里也没有 `videos`；之前没开的任务需要重新创建并开启录像。
2. `GET DescribeEmbodiedEvalResult evalId=<evalId>`，在 `result.benchmarks[].details.tasks[].videos[]` 里按 `episode_index`、`success` 找到目标 episode，取 `video_id`（以 `v-` 开头），并看 `state`。
3. `python3 <skill目录>/scripts/call_eval.py GET DescribeEmbodiedEvalVideo evalId=<evalId> videoId=<video_id>`，得到 `streamUrl`，同时返回 `expiresIn`、`mimeType`、`sizeBytes`。
4. 链接打不开时：
   - `streamUrl` 的有效期是 `expiresIn` 秒（当前为 900 秒），过期后重新调用 `DescribeEmbodiedEvalVideo` 即可，不要保存链接。
   - `state=failed` 的视频没有文件，调用会返回 404。
   - `streamUrl` 自带访问凭证，不要转发给无关人员。
   - 集成到自己的系统时，只保存 `evalId` 和 `videoId`，播放时实时取地址。

### 14. 停止与删除
- `StopEmbodiedEval`：可停止运行中或排队中的任务，任务先进入 `Stopping`，再变为 `ManualTermination`。停止前已写出的结果仍可查询。
  - 任务已经是 `ManualTermination` 时再停返回成功。
  - 任务已经是 `Succeeded`、`Failed`、`CreateFailed` 时返回 400。
  - 任务刚创建、还在提交时返回 503 `ResourceBusy`，等几秒重试。
- `DeleteEmbodiedEval`：运行中的任务会先被终止再删除，删除后不能再查询。重复删除返回 404，重试时可以当作已删除。删除后，该任务用过的 `clientToken` 可以再用。
- 权限：
  - 资源池管理员可以停止或删除池内任意任务；队列成员只能操作自己创建的任务；只读用户不能操作。
  - `visibleScope=1` 的任务对同队列成员可见，`visibleScope=0` 的任务只对创建人和管理员可见。
  - 同账号但没有权限返回 403；任务不存在、属于其他账号或已删除返回 404。
- 重试：`Describe*` 可以直接重试；Create 重试要带同一个 `clientToken`；Stop、Delete 重复调用安全；429、500、504 可以稍后重试。

## 任务状态
- 非终态：`Created`、`ToBeSubmitted`、`Scheduled`（排队等资源）、`Running`、`Scaling`、`Restarting`、`Preempted`（被抢占，等待重新调度）、`Stopping`、`Abnormal`、`Unknown`。
- 终态：`Succeeded`、`Failed`、`ManualTermination`、`CreateFailed`（只在详情中出现）。

## 随附文件说明
- `<skill目录>/scripts/call_eval.py`：调用任一接口的签名脚本，只依赖 Python 3 标准库。
  - 环境变量：`BCE_AK`、`BCE_SK` 必填；`AIHC_ENDPOINT` 填服务域名，不设时默认北京。
  - 用法：`python3 <skill目录>/scripts/call_eval.py METHOD ACTION [key=value ...] [--body 文件.json]`，`--help` 查看说明。
  - 输出和退出码见「调用方式」一节。
- `<skill目录>/references/api-reference.md`：10 个接口的完整请求参数表、响应参数表、返回示例，以及 ModelConfig、BenchmarkConfig、DataSource、Resource、EvalResult 等数据结构。需要核对字段类型、可选值或完整返回结构时阅读。
- `<skill目录>/templates/create-public-libero.json`：预置模型请求体模板（OpenVLA 跑 LIBERO 子集）。复制为 `create.json`，替换 `<资源池ID>`、`<队列ID>`、卡型、`subBench`、`modelOverride` 等值后，用 `--body create.json` 创建。
- `<skill目录>/templates/create-custom-ckpt.json`：自己的权重模板，包含 `dataSources` 挂载和 `/mnt/ckpt/<权重目录>` 形式的 `ckptPath`。替换 bucket、前缀、权重目录、子集和对应的 `unnorm_key`。
- `<skill目录>/templates/create-custom-image.json`：自己的推理镜像模板，包含 `customImage` 的 `image`、`command`、`envs`、`imageConfig`。替换镜像地址、启动命令中的权重路径、仓库凭据。

## 输出要求
- 用中文直接回答客户，先给结论或原因，再给按顺序可执行的步骤。
- 每个需要调用接口的步骤都写出实际命令（`call_eval.py` 形式，或客户要求的 SDK/CLI 形式）和关键参数、请求体字段。需要请求体时，指明用哪个模板，并写出要改的字段。
- 客户自己填的值用占位符标出，如 `<资源池ID>`、`<队列ID>`、`<evalId>`、`<bucket>/<prefix>`。不要在回答里写入真实 AK/SK。
- 涉及删除、停止等不可逆操作时，提醒客户先确认 `evalId`。
- 调用方要求结构化输出时，按调用方给出的格式输出。

## 常见误判
- 声称手上没有接口说明，然后自编接口名（如 `CreateEmbodiedEvalTask`、`DescribeEvalEpisodeVideo`）或字段（如 `queueName`、`model.type`、`benchmark.suite`、`numTrialsPerTask`、`unnormKey`、`gpuType`、`inferenceService`、`output.path`）。正确做法是只用本技能列出的接口和字段。
- 自己写签名脚本或手拼 curl 签名，而不是用随附的 `call_eval.py`。
- 用其他产品的接口（如 `DescribeResourcePools`）验证调通。验证调通用 `DescribeEmbodiedEvals`。
- 客户在苏州等非北京地域时，没有设置 `AIHC_ENDPOINT`，导致脚本默认打到北京，返回 `ResourceNotFound`。
- 把 `evalId` 等参数放进请求体。除 Create 外，所有参数都在 query。
- OpenVLA 的 `unnorm_key` 写成开源脚本里带 `_no_noops` 后缀的值，或者与所跑的子集不一致。
- 在 `params` 里填 `suite`、`task_name`、`tasks`。子集只由 `subBench` 决定。
- `dataSources.sourcePath` 带 `bos://` 前缀；`ckptPath` 不以挂载点开头；挂载到 `/mnt/aihc-eval`。
- 自定义镜像用 `/health` 做就绪检查、在 `command` 里写死端口、把仓库密码放进 `envs`，或者以为 `ckptPath` 会传给镜像。
- 把 400 报错里的内部字段名（`benchmarkConfs`、`episodePerTask`）当成请求体字段让客户照写；把报错里的上限数字当成所有评测集的通用上限。
- 给所有创建请求写死同一个 `clientToken`，或者在 400、500 后只换 token 不改参数就反复重试。
- 认为 `Scheduled` 就是任务挂了，或者编造排队原因字段和其他查询接口。
- 只看 `mean_success`，不检查 `partial`、`num_errors`、`num_timeouts`。
- 用预置模型时，把夹爪符号、图像旋转、prompt 格式当成排查重点。这些由平台适配，只有自定义镜像时才需要客户自己保证。
- 用论文或公开报告的分数作为验收标准写进回答。
- 保存 `streamUrl` 长期使用，或者给出平台外的下载方式。
