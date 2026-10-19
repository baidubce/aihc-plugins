# aihc-plugins

百度智能云百舸（AIHC）AI 编程助手插件中心。仓库把百舸相关的 skills、MCP 服务等能力打包成插件，同一套插件可以安装到以下 AI 编程助手：

| 客户端 | 市场清单 | 插件清单 |
|---|---|---|
| Claude Code | `.claude-plugin/marketplace.json` | `.claude-plugin/plugin.json` |
| OpenAI Codex | `.agents/plugins/marketplace.json` | `plugin.json`（Agent Plugins 1.0） |
| GitHub Copilot CLI | `.claude-plugin/marketplace.json` | `plugin.json`（Agent Plugins 1.0） |
| Cursor | `.cursor-plugin/marketplace.json` | `plugin.json`（Agent Plugins 1.0） |

- **插件（plugin）**：`plugins/` 下的一个子目录，包含 skills、MCP 配置等组件和插件清单。
- **市场（marketplace）**：仓库根目录的市场清单，登记收录了哪些插件。各客户端的市场名统一为 `aihc-plugins`。
- **Agent Plugins 1.0**：[agent-plugins.org](https://agent-plugins.org/plugin-authors) 定义的开放插件格式，Codex、Copilot CLI、Cursor 都支持。Claude Code 仍使用自己的 `.claude-plugin/` 格式，因此每个插件同时提供两份清单。

## 插件列表

| 插件 | 版本 | 功能 | 支持的客户端 |
|---|---|---|---|
| [aihc-eval](plugins/aihc-eval) | 0.2.1 | 百舸模型评测工具集：把自定义推理服务改造成具身评测可用的 Model Server（用户自定义镜像接入）等评测场景能力。 | Claude Code、Codex、Copilot CLI、Cursor |

## ⚠️ 使用须知

**本仓库的插件会以您的百度智能云账号身份调用云服务 API，可能创建、修改或删除云上资源，并产生实际费用。**

- **费用责任**：插件发起的资源创建、作业提交等操作按百度智能云实际计价规则计费，费用由您自行承担。建议首次使用时先用小规模资源验证，再逐步放量。
- **操作前请确认**：涉及创建、修改、删除资源的操作，请在 AI 编程助手请求执行前核对目标资源和参数。建议保留客户端的工具调用确认机制，不要对插件开启自动批准。
- **结果仅供参考**：插件输出的诊断结论、配置建议、资源规格建议等内容由 AI 结合规则生成，可能不准确或不完整，不构成对资源规格、执行时长、作业成功率或服务等级（SLA）的任何承诺。
- **产出需自行验证**：插件辅助生成或修改的代码、配置、数据，其正确性、完整性和业务适用性须由您独立验证后再用于生产环境。
- **凭证安全**：AK/SK 等访问凭证请通过环境变量或客户端的安全配置注入，不要写入代码或提交到仓库，并按最小权限原则授权。
- **建议先在非生产环境试用**，确认行为符合预期后再接入正式流程。

本仓库按"现状"（AS IS）提供，不附带任何明示或默示担保，详见 [LICENSE](./LICENSE) 第 7、8 条。

## 安装与使用

下文的 `<插件名>` 替换为 [插件列表](#插件列表) 中的插件名。

### Claude Code

```text
/plugin marketplace add baidubce/aihc-plugins
/plugin install <插件名>@aihc-plugins
```

- 获取更新：`/plugin marketplace update aihc-plugins`，然后重新安装插件。
- 卸载：`/plugin uninstall <插件名>@aihc-plugins`。

### OpenAI Codex

```bash
codex plugin marketplace add baidubce/aihc-plugins
```

然后在 Codex 会话中执行 `/plugins`，切到 `aihc-plugins` 市场安装插件。安装后需要新开会话才能使用插件中的 skills。

- 获取更新：`codex plugin marketplace upgrade aihc-plugins`。
- 移除市场：`codex plugin marketplace remove aihc-plugins`。

### GitHub Copilot CLI

```bash
copilot plugin marketplace add baidubce/aihc-plugins
copilot plugin install <插件名>@aihc-plugins
```

- 查看已安装插件：`copilot plugin list`。
- 卸载：`copilot plugin uninstall <插件名>`。

### Cursor

- **团队市场**（Teams / Enterprise 版）：管理员在 Dashboard → Plugins & MCPs → Team Marketplaces 中选择 Add Marketplace → Import from Repo，填入 `https://github.com/baidubce/aihc-plugins`。成员在侧边栏 Customize 中安装插件。
- **本地安装**：把 `plugins/<插件名>` 目录复制到 `~/.cursor/plugins/local/<插件名>`，然后重启 Cursor 或执行 Developer: Reload Window。

## 目录结构

```text
aihc-plugins/
├── .claude-plugin/
│   └── marketplace.json            # Claude Code 市场清单（Copilot CLI 也读取）
├── .agents/plugins/
│   └── marketplace.json            # Codex 市场清单
├── .cursor-plugin/
│   └── marketplace.json            # Cursor 市场清单
├── plugins/                        # 一个插件一个子目录
│   └── <插件名>/
│       ├── plugin.json             # Agent Plugins 1.0 清单（Codex / Copilot CLI / Cursor）
│       ├── .claude-plugin/
│       │   └── plugin.json         # Claude Code 清单
│       ├── skills/
│       │   └── <技能名>/SKILL.md    # 四个客户端通用
│       ├── mcp.json                # MCP 配置，Agent Plugins 格式（可选）
│       └── .mcp.json               # MCP 配置，Claude Code 格式（可选）
├── scripts/                        # 仓库维护脚本
├── LICENSE
├── NOTICE
├── THIRD-PARTY-LICENSES
└── README.md
```

插件的核心能力以 skills 和 MCP 服务提供，四个客户端都能使用。commands、agents、hooks 等组件各客户端格式不同，只在插件说明中注明支持的客户端里提供。

## 问题反馈

使用中遇到问题或有改进建议，请在 [GitHub Issues](https://github.com/baidubce/aihc-plugins/issues) 中提交。涉及账号、资源或计费的问题，请通过百度智能云工单联系技术支持。

## 许可证 / License

本项目采用 [Apache License 2.0](./LICENSE) 开源许可。

Copyright 2026 Baidu, Inc. All Rights Reserved.

| 文件 | 说明 |
|------|------|
| [LICENSE](./LICENSE) | Apache License 2.0 完整条款 |
| [NOTICE](./NOTICE) | 版权归属与商标声明 |
| [THIRD-PARTY-LICENSES](./THIRD-PARTY-LICENSES) | 第三方依赖组件及其许可证清单 |

### 商标说明

"百度"、"百度智能云"、"百舸"、"AIHC" 及相关名称、标识、产品与服务名称均为百度公司的商标。Apache License 2.0 不授予上述商标、商号、服务标记或产品名称的使用许可，但在描述本作品来源时的合理及惯常使用不受此限（详见许可证第 6 条）。

基于本项目的衍生作品，不得以暗示由百度公司开发、认可或支持的方式使用上述商标进行宣传或分发。
