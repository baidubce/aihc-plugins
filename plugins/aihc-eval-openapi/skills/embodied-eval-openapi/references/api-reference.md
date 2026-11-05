# 百舸具身评测 OpenAPI 参考：参数、返回和数据结构

本文是具身评测 OpenAPI 10 个接口的请求参数、响应参数、返回示例和数据结构速查。调用方式、签名、流程和排错见 SKILL.md；调用脚本是 `<skill目录>/scripts/call_eval.py`。示例使用苏州地域（`aihc.su.baidubce.com`），ID 都是占位符。

## 接口列表
|接口|方法|说明|
|-|-|-|
|CreateEmbodiedEval|POST|创建评测任务|
|DescribeEmbodiedEvals|GET|查询评测任务列表|
|DescribeEmbodiedEval|GET|查询评测任务详情|
|StopEmbodiedEval|POST|停止评测任务|
|DeleteEmbodiedEval|POST|删除评测任务|
|DescribeEmbodiedEvalResult|GET|查询评测结果|
|DescribeEmbodiedEvalVideo|GET|查询回放视频播放地址|
|DescribeEmbodiedEvalModels|GET|查询可评测的模型及其评测集|
|DescribeEmbodiedEvalModel|GET|查询模型的默认配置|
|DescribeEmbodiedEvalBenchmarks|GET|查询评测集的默认配置|

## CreateEmbodiedEval
### 请求结构
```
POST /?action=CreateEmbodiedEval&clientToken={clientToken}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
Content-Type: application/json
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`clientToken`|String|否|Query参数|幂等键，长度不超过 64 字节，规则见下文|
|`name`|String|是|RequestBody参数|任务名称，不超过 64 字节|
|`resourcePoolId`|String|是|RequestBody参数|资源池 ID|
|`queueId`|String|是|RequestBody参数|队列 ID|
|`resourcePoolType`|String|否|RequestBody参数|资源池类型。使用全托管（Serverless）资源池时填 `serverless`，否则不传|
|`priority`|String|否|RequestBody参数|任务优先级：`high` / `normal` / `low`，默认 `normal`|
|`visibleScope`|Integer|否|RequestBody参数|可见范围：`1` 队列内成员可见，`0` 仅创建人可见。默认 `1`|
|`dataSources`|DataSource[]|否|RequestBody参数|挂载到任务里的数据源，用于读取自己的模型权重。目前只支持 `bos` 类型|
|`modelConfig`|ModelConfig|是|RequestBody参数|模型服务配置|
|`benchmarkConfigs`|BenchmarkConfig[]|是|RequestBody参数|评测集配置，1～20 个|

`clientToken` 的使用规则：

* 每次创建生成一个新的值（如 UUID），只有重试**同一次**创建时才传回同一个值。不传时不做幂等，每次调用都新建任务。
* 同一账号下，同一个 `clientToken` 只会创建一个任务：重试时返回原来的 `evalId`；参数和第一次不一样时返回 403 `IdempotentParameterMismatch`。
* `clientToken` 没有有效期，只有对应任务被删除或创建失败后才能再用。不要给所有请求写死同一个值，否则从第二个任务起都会返回第一个任务的 `evalId` 或报参数冲突。

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`evalId`|String|评测任务 ID，格式为 `em-` 加 12 位字符|

### 响应示例
```json
{
  "requestId": "1f2e3d4c-5b6a-4789-8a9b-0c1d2e3f4a5b",
  "evalId": "em-0123456789ab"
}
```

### 请求体示例
预置 OpenVLA 跑 LIBERO `libero_spatial`（同 `<skill目录>/templates/create-public-libero.json`）：

```json
{
  "name": "openvla-libero-spatial",
  "resourcePoolId": "cce-xxxxxxxx",
  "queueId": "default",
  "priority": "normal",
  "modelConfig": {
    "modelType": "public",
    "modelName": "openvla_7b",
    "replicas": 1,
    "maxBatchSize": 1,
    "resources": [
      {"name": "baidu.com/rtx_4090_cgpu", "quantity": 1},
      {"name": "cpu", "quantity": 8},
      {"name": "memory", "quantity": 32}
    ]
  },
  "benchmarkConfigs": [
    {
      "name": "libero",
      "subBench": "libero_spatial",
      "resources": [
        {"name": "baidu.com/rtx_4090_cgpu", "quantity": 1},
        {"name": "cpu", "quantity": 8},
        {"name": "memory", "quantity": 32}
      ],
      "parallelShards": 1,
      "episodesPerTask": 10,
      "recordVideo": true,
      "modelOverride": {"chunkSize": 1}
    }
  ]
}
```

## DescribeEmbodiedEvals
### 请求结构
```
GET /?action=DescribeEmbodiedEvals&pageNumber=1&pageSize=10
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`pageNumber`|Integer|否|Query参数|页码，从 1 开始，默认 1|
|`pageSize`|Integer|否|Query参数|每页条数，1～100，默认 10|
|`keywordType`|String|否|Query参数|关键字匹配的字段：`name` 任务名称、`evalId` 任务 ID、`creator` 创建人。传 `keyword` 时必须传|
|`keyword`|String|否|Query参数|关键字，按 `keywordType` 指定的字段模糊匹配|
|`status`|String|否|Query参数|按状态过滤，取值见「任务状态」，多个用英文逗号分隔，如 `Running,Failed`|
|`modelName`|String|否|Query参数|按模型名称精确过滤，多个用英文逗号分隔|
|`priority`|String|否|Query参数|按优先级过滤，多个用英文逗号分隔|
|`orderBy`|String|否|Query参数|排序字段：`createdAt`、`finishedAt`、`runTimeSeconds`、`priority`，默认 `createdAt`|
|`order`|String|否|Query参数|排序方向：`ASC` / `DESC`，只接受大写，默认 `DESC`|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`totalCount`|Integer|符合条件的任务总数|
|`evals`|EvalSummary[]|当前页的任务|

### 响应示例
```json
{
  "requestId": "2a3b4c5d-6e7f-4801-9a2b-3c4d5e6f7a8b",
  "totalCount": 1,
  "evals": [
    {
      "evalId": "em-0123456789ab",
      "name": "openvla-libero-spatial",
      "status": "Running",
      "resourcePoolId": "cce-xxxxxxxx",
      "queueId": "default",
      "resourcePoolType": "",
      "priority": "normal",
      "visibleScope": 1,
      "creator": "zhangsan",
      "modelName": "openvla_7b",
      "modelDisplayName": "OpenVLA",
      "benchmarks": [{"name": "libero", "displayName": "LIBERO", "subBench": "libero_spatial"}],
      "resources": [
        {"name": "baidu.com/rtx_4090_cgpu", "quantity": 2},
        {"name": "cpu", "quantity": 16},
        {"name": "memory", "quantity": 64}
      ],
      "runTimeSeconds": 1260,
      "createdAt": "2026-11-04T02:00:00Z",
      "finishedAt": "",
      "region": "su"
    }
  ]
}
```

## DescribeEmbodiedEval
### 请求结构
```
GET /?action=DescribeEmbodiedEval&evalId={evalId}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`evalId`|String|是|Query参数|评测任务 ID|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`evalId`|String|评测任务 ID|
|`name`|String|任务名称|
|`status`|String|任务状态，见「任务状态」|
|`jobId`|String|任务对应的百舸分布式训练任务 ID，任务还没提交时为空|
|`resourcePoolId` / `queueId` / `resourcePoolType`|String|同创建请求|
|`priority`|String|优先级|
|`visibleScope`|Integer|可见范围|
|`creator`|String|创建人用户名|
|`region`|String|地域|
|`runTimeSeconds`|Integer|累计运行秒数，没开始运行时为 0|
|`createdAt` / `updatedAt` / `finishedAt`|String|创建、更新、结束时间|
|`dataSources`|DataSource[]|创建时传入的数据源，不包含平台自动挂载的数据源|
|`modelConfig`|ModelConfig|模型服务配置|
|`benchmarkConfigs`|BenchmarkConfig[]|评测集配置。`params`、`modelOverride.extraArgs` 是创建时传入的值，没传或为空对象时可能不返回该字段，按空对象处理即可|

### 响应示例
```json
{
  "requestId": "3b4c5d6e-7f80-4912-ab3c-4d5e6f7a8b9c",
  "evalId": "em-0123456789ab",
  "name": "openvla-libero-spatial",
  "jobId": "job-a1b2c3d4e5f6",
  "status": "Succeeded",
  "resourcePoolId": "cce-xxxxxxxx",
  "queueId": "default",
  "resourcePoolType": "",
  "priority": "normal",
  "visibleScope": 1,
  "creator": "zhangsan",
  "region": "su",
  "runTimeSeconds": 2410,
  "createdAt": "2026-11-04T02:00:00Z",
  "finishedAt": "2026-11-04T02:43:12Z",
  "updatedAt": "2026-11-04T02:43:20Z",
  "modelConfig": {
    "modelType": "public",
    "modelName": "openvla_7b",
    "replicas": 1,
    "maxBatchSize": 1,
    "resources": [
      {"name": "baidu.com/rtx_4090_cgpu", "quantity": 1},
      {"name": "cpu", "quantity": 8},
      {"name": "memory", "quantity": 32}
    ]
  },
  "benchmarkConfigs": [
    {
      "name": "libero",
      "subBench": "libero_spatial",
      "resources": [
        {"name": "baidu.com/rtx_4090_cgpu", "quantity": 1},
        {"name": "cpu", "quantity": 8},
        {"name": "memory", "quantity": 32}
      ],
      "evalType": "sync",
      "parallelShards": 1,
      "episodesPerTask": 10,
      "recordVideo": true,
      "modelOverride": {"chunkSize": 1}
    }
  ]
}
```

## StopEmbodiedEval
### 请求结构
```
POST /?action=StopEmbodiedEval&evalId={evalId}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
Content-Type: application/json
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`evalId`|String|是|Query参数|评测任务 ID，一次只能停止一个任务|
|`clientToken`|String|否|Query参数|幂等键，不超过 64 字节|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`evalId`|String|被停止的任务 ID|

### 响应示例
```json
{"requestId": "4c5d6e7f-8091-4a23-bc4d-5e6f7a8b9c0d", "evalId": "em-0123456789ab"}
```

## DeleteEmbodiedEval
### 请求结构
```
POST /?action=DeleteEmbodiedEval&evalId={evalId}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
Content-Type: application/json
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`evalId`|String|是|Query参数|评测任务 ID，一次只能删除一个任务|
|`clientToken`|String|否|Query参数|幂等键，不超过 64 字节|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`evalId`|String|被删除的任务 ID|

### 响应示例
```json
{"requestId": "5d6e7f80-91a2-4b34-cd5e-6f7a8b9c0d1e", "evalId": "em-0123456789ab"}
```

## DescribeEmbodiedEvalResult
### 请求结构
```
GET /?action=DescribeEmbodiedEvalResult&evalId={evalId}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`evalId`|String|是|Query参数|评测任务 ID|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`evalId`|String|评测任务 ID|
|`result`|EvalResult|评测结果。结果文件还没有产出时为 `null`|

### 响应示例
```json
{
  "requestId": "6e7f8091-a2b3-4c45-de6f-7a8b9c0d1e2f",
  "evalId": "em-0123456789ab",
  "result": {
    "version": "1",
    "job_id": "job-a1b2c3d4e5f6",
    "started_at": "2026-11-04T02:01:05Z",
    "finished_at": "2026-11-04T02:43:02Z",
    "video_summary": {"enabled": true, "total": 100, "ready": 100, "failed": 0},
    "benchmarks": [
      {
        "id": "libero-libero_spatial",
        "name": "libero",
        "sub_bench": "libero_spatial",
        "status": "succeeded",
        "progress": {"completed_episodes": 100, "total_episodes": 100, "num_errors": 0, "num_timeouts": 3},
        "config": {
          "mode": "sync", "num_shards": 1, "episodes_per_task": 10, "max_steps": 220, "max_tasks": null, "tasks": null,
          "chunk_size": 1,
          "params": {"seed": 7, "num_steps_wait": 10, "absolute_action": false, "max_steps": null, "env_seed": null, "suite": "libero_spatial"}
        },
        "metrics": {"mean_success": 0.84, "num_tasks": 10},
        "details": {
          "tasks": [
            {
              "task": "pick up the black bowl between the plate and the ramekin and place it on the plate",
              "mean_success": 0.9,
              "avg_steps": 132.4,
              "num_episodes": 10,
              "num_errors": 0,
              "num_timeouts": 0,
              "videos": [
                {"video_id": "v-ZW0tMDEyMzQ1Njc4OWFiL3ZpZGVvcy9saWJlcm8tbGliZXJvX3NwYXRpYWwvc2hhcmQwL3Rhc2swMDAwX2VwMDAwMC5tcDQ", "episode_index": 0, "success": true, "state": "ready"}
              ]
            }
          ]
        }
      }
    ]
  }
}
```
示例里只列出了一个任务和一个视频，实际返回包含全部任务。

## DescribeEmbodiedEvalVideo
### 请求结构
```
GET /?action=DescribeEmbodiedEvalVideo&evalId={evalId}&videoId={videoId}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`evalId`|String|是|Query参数|评测任务 ID|
|`videoId`|String|是|Query参数|视频 ID，以 `v-` 开头|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`videoId`|String|视频 ID|
|`streamUrl`|String|视频的临时访问地址，支持 HTTP Range 请求，可直接在浏览器或播放器里打开|
|`expiresIn`|Integer|`streamUrl` 的有效期，单位秒，目前为 900|
|`mimeType`|String|视频格式，如 `video/mp4`|
|`sizeBytes`|Integer|视频大小，单位字节|

### 响应示例
```json
{
  "requestId": "7f8091a2-b3c4-4d56-ef7a-8b9c0d1e2f3a",
  "videoId": "v-ZW0tMDEyMzQ1Njc4OWFiL3ZpZGVvcy9saWJlcm8tbGliZXJvX3NwYXRpYWwvc2hhcmQwL3Rhc2swMDAwX2VwMDAwMC5tcDQ",
  "streamUrl": "https://aihc-eval-xxxx-su.su.bcebos.com/results/...?authorization=...",
  "expiresIn": 900,
  "mimeType": "video/mp4",
  "sizeBytes": 1048576
}
```

## DescribeEmbodiedEvalModels
### 请求结构
```
GET /?action=DescribeEmbodiedEvalModels&source={source}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`source`|String|否|Query参数|`public`：平台预置模型及其经过验证的评测集组合，默认值。`custom`：使用自己的权重或镜像时用，每个模型都列出全部评测集|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`models`|ModelItem[]|模型列表|

### 响应示例
```json
{
  "requestId": "8091a2b3-c4d5-4e67-f08b-9c0d1e2f3a4b",
  "models": [
    {
      "modelName": "openvla_7b",
      "displayName": "OpenVLA",
      "benchmarks": [
        {"name": "libero", "displayName": "LIBERO", "subBenches": ["libero_spatial", "libero_object", "libero_goal", "libero_10"]}
      ]
    },
    {
      "modelName": "pi05_libero",
      "displayName": "Pi0.5",
      "benchmarks": [
        {"name": "libero", "displayName": "LIBERO", "subBenches": ["libero_spatial", "libero_object", "libero_goal", "libero_10"]}
      ]
    }
  ]
}
```

## DescribeEmbodiedEvalModel
### 请求结构
```
GET /?action=DescribeEmbodiedEvalModel&modelName={modelName}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`modelName`|String|是|Query参数|模型名称，取自 `DescribeEmbodiedEvalModels`|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`modelName` / `displayName`|String|模型名称、展示名称|
|`minHardware`|MinHardware|模型服务单副本的最低硬件要求|
|`defaultModelConfig`|ModelConfig|推荐的模型服务配置|
|`extraArgsSchema`|ParamSchema|`benchmarkConfigs[].modelOverride.extraArgs` 可以填的参数|
|`chunkSizeLimit`|Integer|`modelOverride.chunkSize` 的建议上限|
|`maxBatchSizeLimit`|Integer|`modelConfig.maxBatchSize` 的建议上限|

### 响应示例
```json
{
  "requestId": "91a2b3c4-d5e6-4f78-a19c-0d1e2f3a4b5c",
  "modelName": "openvla_7b",
  "displayName": "OpenVLA",
  "minHardware": {
    "acceleratorTypes": ["baidu.com/l20_cgpu", "baidu.com/rtx_4090_cgpu", "baidu.com/a10_24g_cgpu", "baidu.com/a800_80g_cgpu"],
    "acceleratorCount": 1,
    "cpu": 4,
    "memory": 16
  },
  "defaultModelConfig": {
    "modelType": "public",
    "modelName": "openvla_7b",
    "replicas": 1,
    "maxBatchSize": 1,
    "resources": [
      {"name": "baidu.com/l20_cgpu", "quantity": 1},
      {"name": "cpu", "quantity": 8},
      {"name": "memory", "quantity": 32}
    ]
  },
  "extraArgsSchema": {
    "requiredArgs": ["unnorm_key"],
    "optionalArgs": ["unnorm_key", "jpeg_roundtrip", "center_crop", "action_ensemble"],
    "properties": {
      "unnorm_key": {"type": "string", "default": null, "nullable": true, "enum": ["libero_spatial", "libero_object", "libero_goal", "libero_10", "bridge_orig"], "description": "动作反归一化使用哪一组训练集统计量"},
      "jpeg_roundtrip": {"type": "boolean", "default": false, "description": "模拟 OpenVLA 官方评测的 JPEG 预处理"},
      "center_crop": {"type": "boolean", "default": false, "description": "模拟 OpenVLA 官方评测的中心裁剪预处理"},
      "action_ensemble": {"type": "string", "default": "newest", "enum": ["newest", "average", "ema"], "description": "动作 chunk 跨步重叠时的融合策略"}
    }
  },
  "chunkSizeLimit": 8,
  "maxBatchSizeLimit": 1
}
```

## DescribeEmbodiedEvalBenchmarks
### 请求结构
```
GET /?action=DescribeEmbodiedEvalBenchmarks&modelName={modelName}&benchmarkName={benchmarkName}&subBench={subBench}&source={source}
Host: aihc.su.baidubce.com
Authorization: {authorization}
version: v2
```

### 请求参数
|参数名称|类型|是否必须|参数位置|说明|
|-|-|-|-|-|
|`modelName`|String|是|Query参数|模型名称|
|`benchmarkName`|String|否|Query参数|评测集名称，不传时返回该模型的全部评测集|
|`subBench`|String|否|Query参数|子集名称，不传时返回评测集的全部子集|
|`source`|String|否|Query参数|同 `DescribeEmbodiedEvalModels`，默认 `public`。用自己的权重或镜像时传 `custom`|

### 响应参数
|参数名称|类型|说明|
|-|-|-|
|`requestId`|String|请求 ID|
|`benchmarks`|BenchmarkMetadata[]|每个组合的默认配置|

### 响应示例
```json
{
  "requestId": "a2b3c4d5-e6f7-4089-b2ad-1e2f3a4b5c6d",
  "benchmarks": [
    {
      "modelName": "openvla_7b",
      "name": "libero",
      "subBench": "libero_spatial",
      "minHardware": {
        "acceleratorTypes": ["baidu.com/l20_cgpu", "baidu.com/rtx_4090_cgpu", "baidu.com/a10_24g_cgpu", "baidu.com/a800_80g_cgpu"],
        "acceleratorCount": 0,
        "cpu": 8,
        "memory": 16
      },
      "defaultBenchmarkConfig": {
        "name": "libero",
        "subBench": "libero_spatial",
        "resources": [
          {"name": "baidu.com/l20_cgpu", "quantity": 1},
          {"name": "cpu", "quantity": 8},
          {"name": "memory", "quantity": 32}
        ],
        "evalType": "sync",
        "parallelShards": 1,
        "episodesPerTask": 50,
        "params": {"seed": 7, "num_steps_wait": 10, "absolute_action": false, "max_steps": null, "env_seed": null, "suite": "libero_spatial"},
        "recordVideo": false,
        "modelOverride": {
          "ckptPath": "openvla/openvla-7b-finetuned-libero-spatial",
          "chunkSize": 1,
          "extraArgs": {"unnorm_key": "libero_spatial", "jpeg_roundtrip": true, "center_crop": true}
        }
      },
      "paramsSchema": {
        "requiredArgs": [],
        "optionalArgs": ["seed", "num_steps_wait", "send_wrist_image", "send_state", "absolute_action", "max_steps", "env_seed"],
        "properties": {
          "seed": {"type": "integer", "default": 7, "description": "环境初始化随机种子"},
          "max_steps": {"type": "integer", "default": null, "nullable": true, "description": "覆盖 suite 默认最大步数；null 时按子集默认值"}
        }
      },
      "maxEpisodesPerTask": 50
    }
  ]
}
```
示例里的 `paramsSchema.properties` 只列出了两项。

# 数据结构
## ModelConfig
模型服务配置。平台按此配置启动 `replicas` 个推理服务副本，评测集通过它们调用模型。

|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`modelType`|String|是|模型来源：`public` 平台预置模型和权重；`customCKPT` 平台预置模型 + 自己的权重；`customImage` 自己的推理服务镜像|
|`modelName`|String|条件必须|`public`、`customCKPT` 必填，取自 `DescribeEmbodiedEvalModels`；`customImage` 不能填|
|`ckptPath`|String|条件必须|模型权重路径，对所有评测集生效；某个评测集的 `modelOverride.ckptPath` 非空时以后者为准。`customCKPT` 时它和每个评测集的 `modelOverride.ckptPath` 至少要填一个。以 `/` 开头表示容器内路径，必须在 `dataSources` 的某个 `mountPath` 下；否则按 Hugging Face 仓库名处理（如 `openvla/openvla-7b`）|
|`replicas`|Integer|是|推理服务副本数，大于 0，且是 `benchmarkConfigs` 个数的整数倍|
|`maxBatchSize`|Integer|是|单副本最大批大小，大于 0|
|`resources`|Resource[]|是|单副本的资源。GPU 卡型必须在模型支持的卡型内（`DescribeEmbodiedEvalModel` 的 `minHardware.acceleratorTypes`）|
|`customImage`|CustomImage|条件必须|`modelType=customImage` 时必填，其他类型不能传|

## CustomImage
自己的推理服务镜像。镜像必须满足「自定义镜像要求」。

|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`image`|String|是|镜像地址，包含仓库和 tag，不超过 512 字节。填写 `imageConfig` 时镜像必须在 `ccr-registry.baidubce.com` 上|
|`command`|String|是|启动命令，不超过 4096 字节。按空格切分后直接执行（不经过 shell），不能写死监听端口，见「自定义镜像要求」|
|`envs`|EnvVar[]|否|环境变量，`{"name": "...", "value": "..."}`，最多 32 个，单个 `value` 不超过 1024 字节，全部合计不超过 8192 字节|
|`imageConfig`|ImageConfig|否|私有镜像仓库凭据，`{"username": "...", "password": "..."}`，两项同时填或同时不填，各不超过 256 字节|

* `envs` 的 `name` 只能包含字母、数字、`-`、`_`、`.`，不能以数字开头，不能重名。以下名称由平台使用，不能填写（不区分大小写）：`EVAL_SPEC`、`EVAL_SPEC_VERSION`、`EVAL_RESULT_FILE`、`EVAL_PLATFORM_MOUNT`、`EVAL_SRC_MOUNT`、`EVAL_RESULT_MOUNT`、`CUDA_VISIBLE_DEVICES`、`PYTHONPATH`、`MS_PORT`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`ALL_PROXY`，以及以 `RAY_` 开头的名称。
* `envs` 的值在详情里原样返回，**不要在 `envs` 里放密码、密钥**。镜像仓库密码只放在 `imageConfig.password`，详情中会显示为 `******`。
* `imageConfig.password` 不能是 `******`。

## BenchmarkConfig
一个评测集（子集）的配置。

|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`name`|String|是|评测集名称，如 `libero`|
|`subBench`|String|是|子集名称，如 `libero_spatial`，必须属于该评测集，且与模型兼容（`source=public` 时以 `DescribeEmbodiedEvalModels` 为准）|
|`resources`|Resource[]|是|评测集仿真环境的资源，所有分片共用。GPU 卡型必须在评测集支持的卡型内；GPU 为 0 只适用于 `minHardware.acceleratorCount` 为 0 的评测集|
|`parallelShards`|Integer|是|并行分片数，大于 0，不超过 `episodesPerTask`。分片越多跑得越快，需要的 CPU、内存也越多|
|`episodesPerTask`|Integer|是|每个任务（task）跑的 episode 数，大于 0，不超过 `maxEpisodesPerTask`|
|`evalType`|String|否|评测方式：`sync` 同步（默认）、`realtime` 实时|
|`params`|Object|否|评测集参数，可填的键见 `DescribeEmbodiedEvalBenchmarks` 返回的 `paramsSchema`。没填的键使用默认值。不要填 `suite`、`task_name`、`tasks` 等由子集决定的键|
|`recordVideo`|Boolean|否|是否录制每个 episode 的回放视频，默认 `false`。开启后可用 `DescribeEmbodiedEvalVideo` 播放|
|`modelOverride`|ModelOverride|是|本评测集对模型的参数|

## ModelOverride
评测集级的模型参数。

|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`chunkSize`|Integer|是|模型每次推理输出的动作步数，大于 0，建议不超过 `chunkSizeLimit`|
|`ckptPath`|String|否|本评测集使用的权重路径，规则同 `ModelConfig.ckptPath`。不填时依次使用 `modelConfig.ckptPath`、平台推荐权重|
|`extraArgs`|Object|否|模型推理参数，可填的键见 `DescribeEmbodiedEvalModel` 的 `extraArgsSchema`。没填的键使用平台推荐值|

## DataSource
挂载到评测任务里的数据源。目前只支持 BOS。

|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`type`|String|是|数据源类型，目前只能是 `bos`|
|`sourcePath`|String|是|BOS 路径，格式 `<bucket>/<prefix>`，如 `my-bucket/ckpt/openvla-ft`|
|`mountPath`|String|是|容器内的挂载路径，以 `/` 开头，每一段只能包含字母、数字、`_`、`-`，如 `/mnt/ckpt`|
|`name`|String|否|数据源名称|
|`id`|String|否|数据源 ID|
|`options`|Object|否|挂载选项。BOS 可以填 `{"readOnly": true}`|

* 当前账号需要对该 BOS 路径有读权限，建议桶与任务在同一地域。
* `/mnt/aihc-eval` 及其下的子路径、`/mnt/aihc-eval-result` 由平台使用，不能作为 `mountPath`。

## Resource
|参数名称|类型|是否必须|说明|
|-|-|-|-|
|`name`|String|是|资源名称：`cpu`（核）、`memory`（GB）、`sharedMemory`（共享内存，GB，不填时为 10）、GPU 卡型（如 `baidu.com/a800_80g_cgpu`、`baidu.com/l20_cgpu`）|
|`quantity`|Number|是|数量，大于 0。`cpu`、`memory`、`sharedMemory` 保留一位小数|

GPU 卡型需要与队列里实际的卡一致。各模型、评测集支持的卡型见元数据接口返回的 `minHardware.acceleratorTypes`，一次评测里模型和评测集使用同一种卡。

## EvalSummary
`DescribeEmbodiedEvals` 返回的任务摘要。

|参数名称|类型|说明|
|-|-|-|
|`evalId`|String|评测任务 ID|
|`name`|String|任务名称|
|`status`|String|任务状态，见「任务状态」|
|`resourcePoolId` / `queueId` / `resourcePoolType`|String|资源池、队列、资源池类型|
|`priority`|String|优先级|
|`visibleScope`|Integer|可见范围|
|`creator`|String|创建人用户名|
|`modelName` / `modelDisplayName`|String|模型名称和展示名称；自定义镜像任务为 `custom`|
|`benchmarks`|Object[]|评测集和子集，每项 `{name, displayName, subBench}`|
|`resources`|Resource[]|整个任务申请的资源合计|
|`runTimeSeconds`|Integer|累计运行秒数|
|`createdAt` / `finishedAt`|String|创建时间、结束时间|
|`region`|String|地域|

## 任务状态
|状态|是否终态|说明|
|-|-|-|
|`Created`|否|已创建，正在提交|
|`ToBeSubmitted`|否|等待提交|
|`Scheduled`|否|已提交，排队等待资源|
|`Running`|否|运行中|
|`Scaling`|否|扩缩容中|
|`Restarting`|否|重启中|
|`Preempted`|否|被高优先级任务抢占，等待重新调度|
|`Stopping`|否|停止中|
|`Abnormal`|否|运行异常，或底层任务已不存在|
|`Unknown`|否|状态未知，稍后再查|
|`Succeeded`|是|运行成功，可以查询结果|
|`Failed`|是|运行失败|
|`ManualTermination`|是|已手动停止|
|`CreateFailed`|是|创建失败，只在详情中出现，不出现在列表中|

## 自定义镜像要求
使用 `modelType=customImage` 时，平台在每个副本里用 `command` 启动你的推理服务，评测集通过 WebSocket 与它交互。镜像必须同时满足下面的要求，平台在创建时无法检查这些项，不满足时任务会失败，或者跑完但分数不可信。

|#|要求|不满足时的表现|
|-|-|-|
|1|从环境变量 `MS_PORT` 读取端口，监听 `0.0.0.0:$MS_PORT`。`command` 不经过 shell 执行，写在命令里的 `$MS_PORT` 不会被替换，端口只能由程序自己读环境变量|健康检查连不上，900 秒后失败。命令里写了 `--port 8000`、`0.0.0.0:8000` 这类固定端口时创建直接返回 400|
|2|在同一端口提供 `GET /config`，返回 200 作为唯一的就绪信号|同上|
|3|`GET /config` 必须在模型加载完、能推理之后才返回 200|前几个 episode 全部出错，可能得到错误分数|
|4|同一端口同时提供 WebSocket 服务，协议与开源 [vla-evaluation-harness](https://github.com/allenai/vla-evaluation-harness) 的 model server 一致（可以基于其 `vla_eval/model_servers/` 下的实现改造）|健康检查通过但评测阶段连接失败|
|5|启动到就绪不超过 900 秒，期间进程不能退出|超时或立即失败，不重试|
|6|收到 `SIGTERM` 后 30 秒内退出|停止任务时被强制结束|
|7|镜像内 Ray 版本为 2.49.2、Python 版本为 3.13.12|Ray 集群连接失败|
|8|不依赖外网，不依赖 `HF_ENDPOINT`，不假设 `HF_HOME` 可写|下载模型失败或写文件报只读错误|
|9|权重放在 BOS，通过 `dataSources` 挂载，并在 `command` 里写上权重路径。`ckptPath` 不会传给自定义镜像|镜像用内置的默认权重启动，分数与预期不符|
|10|容器的 hostname 能被解析|600 秒后失败|

带 `imageConfig` 的私有镜像必须在 `ccr-registry.baidubce.com` 上。

## EvalResult
`DescribeEmbodiedEvalResult` 返回的 `result` 对象。字段名为下划线风格。

|参数名称|类型|说明|
|-|-|-|
|`version`|String|结果格式版本，目前为 `"1"`|
|`job_id`|String|底层任务 ID|
|`started_at`|String|开始时间，UTC|
|`finished_at`|String|结束时间，UTC；运行中为 `null`|
|`video_summary`|Object|视频统计：`enabled` 是否开启录像，`total` 视频总数，`ready` 可播放数，`failed` 不可播放数|
|`benchmarks`|Object[]|每个评测集（子集）一项，见下表|

`benchmarks[]` 每项：

|参数名称|类型|说明|
|-|-|-|
|`id`|String|评测项 ID，格式 `<评测集>-<子集>`|
|`name`|String|评测集名称|
|`sub_bench`|String|子集名称|
|`status`|String|`running` 运行中、`succeeded` 全部分片成功、`partial` 部分分片失败、`failed` 失败、`stopped` 已停止|
|`progress`|Object|`completed_episodes` 已完成 episode 数、`total_episodes` 总数、`num_errors` 出错数、`num_timeouts` 达到最大步数仍未成功的数量|
|`config`|Object|本评测项实际使用的配置：`mode`、`num_shards`、`episodes_per_task`、`max_steps`、`chunk_size`、`params` 等|
|`metrics`|Object|评分：`mean_success` 平均成功率（0～1）、`num_tasks` 任务数。运行中为 `null`|
|`details`|Object|明细：`tasks[]` 每个任务的 `task` 任务描述、`mean_success`、`avg_steps`、`num_episodes`、`num_errors`、`num_timeouts`、`videos[]`。运行中为 `null`|

`videos[]` 每项：`video_id` 视频 ID（传给 `DescribeEmbodiedEvalVideo`）、`episode_index` episode 序号、`success` 该 episode 是否成功、`state` 为 `ready`（可播放）或 `failed`。

## ModelItem
|参数名称|类型|说明|
|-|-|-|
|`modelName`|String|模型名称，用于创建请求的 `modelConfig.modelName`|
|`displayName`|String|展示名称|
|`benchmarks`|Object[]|可以跑的评测集，每项 `{name, displayName, subBenches}`，`subBenches` 是子集名称列表|

## MinHardware
|参数名称|类型|说明|
|-|-|-|
|`acceleratorTypes`|String[]|支持的 GPU 卡型|
|`acceleratorCount`|Integer|最少 GPU 卡数，0 表示可以不用 GPU|
|`cpu`|Integer|最少 CPU 核数|
|`memory`|Integer|最少内存，单位 GB|

## ParamSchema
描述 `params` 或 `extraArgs` 可以填的参数。

|参数名称|类型|说明|
|-|-|-|
|`requiredArgs`|String[]|必须有值的参数。平台推荐配置里已经给了值时可以不填|
|`optionalArgs`|String[]|可以填的参数|
|`properties`|Object|每个参数的说明，键是参数名，值包含 `type`（`string`、`integer`、`number`、`boolean` 等）、`default` 默认值、`description` 说明，以及可选的 `enum` 取值范围、`nullable` 是否可为 `null`|

## BenchmarkMetadata
|参数名称|类型|说明|
|-|-|-|
|`modelName` / `name` / `subBench`|String|模型、评测集、子集|
|`minHardware`|MinHardware|评测集仿真环境单分片的最低硬件要求|
|`defaultBenchmarkConfig`|BenchmarkConfig|推荐配置|
|`paramsSchema`|ParamSchema|`params` 可以填的参数|
|`maxEpisodesPerTask`|Integer|`episodesPerTask` 的上限，0 表示不限|
