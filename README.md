# LilBot Agent

LilBot 是一个原创的本地 Agent 实验框架，用来学习和复刻现代 coding agent 的核心形态：Agent loop、工具调用、权限审批、沙箱、记忆、技能、子 Agent 和 MCP 风格外部工具接入。

这个项目刻意不复制任何专有实现。代码是干净实现，架构上参考的是通用 Agent 设计模式和本目录里玩具实现暴露出来的公开概念。

## 快速开始

```powershell
pip install -r requirements.txt
python -m lilbot
```

无 API Key 时会使用内置的规则模型，方便测试 TUI、工具、记忆和技能。接入 OpenAI-compatible 模型：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
python -m lilbot --provider deepseek --model deepseek-v4-flash

# or any OpenAI-compatible endpoint:
$env:LILBOT_API_KEY="sk-..."
$env:LILBOT_BASE_URL="https://api.openai.com/v1"
$env:LILBOT_MODEL="gpt-4o-mini"
python -m lilbot --provider openai
```

## 核心架构

```text
User / CLI / TUI
  -> AgentLoop
      -> Provider(OpenAI-compatible or local rule model)
      -> ToolRegistry
          -> Sandbox + PermissionManager
          -> File/Bash/Search tools
          -> Memory tools
          -> Skill tools
          -> Subagent tools
          -> MCP adapter tools
      -> Session transcript + compaction
```

## 常用命令

- `/help` 查看命令
- `/tools` 查看工具
- `/skills` 查看技能
- `/skill review <目标>` 执行技能模板
- `/memory list|search|save|delete` 管理记忆
- `/agents` 查看子 Agent 类型和任务
- `/mcp` 查看 MCP 风格 server 配置
- `/permissions ask|accept-all|deny-all` 切换权限模式
- `/exit` 退出

## 已实现模块

- 原创 Agent loop：支持多轮工具调用、事件流、轻量上下文压缩
- Tool registry：工具 schema、执行器、统一结果模型
- Sandbox：文件路径限制在 workspace 内，shell 在 workspace 下执行
- Permission：写文件和 shell 默认审批，可按会话记住 allow/deny
- Memory：项目级 JSONL 记忆，支持关键词评分搜索
- Skills：Markdown 技能模板，支持 `{{args}}` 替换
- Subagents：轻量后台任务模型，支持 coder/reviewer/researcher/planner
- MCP adapter：读取 `.lilbot/mcp.json`，提供实验性 JSON-RPC stdio 调用入口
- TUI：Rich 彩色 logo、状态条、工具过程和命令面板

## 安全边界

LilBot 默认把工作空间作为根目录。文件工具不能读写根目录外文件；bash 命令需要权限审批。它是学习用 agent，不应该直接用于高风险生产环境。
