# 知行— 市场级通用个人智能体底座 (Personal AI Agent Runtime)

> **第十一届华为 ICT 大赛（中国区创新赛 · 赛题 2：基于华为云 AgentArts 智能体开发平台的 Agent 设计和应用）核心工程实现**

知行是一款摒弃一切写死表单与虚假话术模板、对标主流商业智能体底座（Claude Desktop / Manus / Dify）的**通用个人自主智能体运行时**。基于 Python 3 标准库 + SQLite WAL/FTS5 零外部依赖构建，开箱即用。

---

## 一、核心运行时与学业决策能力

1. **逐 Token 真实 SSE 流式推理引擎 (`backend/runtime/llm_client.py`)**
   - 原生 `stream=True` 无缓冲解析 OpenAI 兼容 SSE 流；
   - 自动重组分片 `tool_calls`，对话只交付最终答复与画布产物，支持前端一键中止生成；
   - 内置首包延迟实测探针（`/api/settings/llm/test`），未配置密钥时诚实提示并直出本地 FTS5 索引结果，绝不伪造对话。
2. **零算术幻觉「学业决策驾驶舱」(`backend/runtime/academic_engine.py`)**
   - **160 学分与博约通识毕业体检**：基于 Python `Decimal` 确定性核算百分制加权均分、4.0 制 GPA、8 大培养方案模块完成度环形图，精准揪出博约经典/核心/百花隐形漏修红灯风险；一键同步至长期记忆或推送交互式 HTML 体检看板至画布；
   - **What-If 综测与推免 ROI 沙盘**：严格执行 `S = G×0.75 + M×0.10 + min(15, I×0.15)` 及推免重修 `0.3×初修 + 0.7×重修` 折算规则，滑动滑块实时对比重修刷分与第十一届华为 ICT 大赛 / HCIP 认证 / 软著的每 10 小时精力 ROI；
   - **校园机会资格雷达与避课 `.ics` 导出**：自动核验校内奖助基金与 A 类赛事硬性门槛（100% 达标 / 差 1 项预警 / 暂未满足），一键导出标准 RFC 5545 `.ics` 手机日历。
3. **自主规划多轮 ReAct 核心循环 (`backend/runtime/agent_loop.py`)**
   - 普通问题最多执行 2 次搜索、3 次网页抓取，明确要求深度调研时扩大检索范围，达到限制后根据已有证据收尾；
   - 内置 9 大通用 Agent 原语工具，并自动合并外部挂载的第三方 MCP 工具。
4. **受控 Python 3 代码解释器沙箱 (`backend/runtime/sandbox.py`)**
   - AST 静态安全检查拦截危险系统破坏指令；
   - 物理挂载 `workspace/files/`，预置零依赖 Office 读取函数 `load_xlsx_rows()` 与 `load_docx_text()`，让智能体针对用户上传的真实成绩单、财务表或数据集编写 Python 脚本精确计算。
5. **免 Key 全网搜索引擎与动态流守护器 (`backend/runtime/web_tools.py`)**
   - 默认免 Key 实时检索公开网页与高校通知，支持任意公开网页正文去噪提取与附件嗅探（`web_fetch`）；
   - 后台守护线程并发巡检 5 类实时信息源（`web_list` / `rss` / `github` / `arxiv` / `search_query`），右侧支持分类筛选与关键词检索。
6. **自进化长期记忆 + FTS5 检索实验台 + MCP Inspector + AgentArts 拓扑 (`storage.py` / `skills.py` / `mcp_bridge.py`)**
   - `memories` 表持久化个人背景、阶段目标、偏好与关键事实，每次变更实时驱动右侧信息流个性化相关度重算；
   - 内置 **SQLite FTS5 BM25 实时检索实验台**、**JSON-RPC 2.0 MCP Inspector 协议调试台**、**华为云 AgentArts 4-Agent 架构拓扑视图**；
   - 现代化象牙翡翠浅色 / 夜间墨玉深色双主题、`Cmd+K` 全局命令面板、`/` 快捷指令菜单、可拖拽调整宽度的全屏交互画布与会话 Markdown 一键导出。

---

## 二、快速启动

无需安装任何第三方 `pip` 依赖（兼容 macOS / Linux 自带 Python 3.9+）：

```bash
cd /Users/xiaoma/Documents/Project/02华为ict大赛/zhixing_agent
python3 run.py
```

浏览器访问 `http://127.0.0.1:8765`，点击左下角「未配置推理引擎」填入华为云 ModelArts MaaS / DeepSeek / SiliconFlow / 阿里云百炼 API Key（或指向本机免 Key Ollama `http://127.0.0.1:11434/v1/chat/completions`），点击「测试连接」验证延迟后即可开始使用。

---

## 三、华为云 AgentArts 平台对接入口

- **MCP Server 挂载地址 (`streamableHttp`)**：`http://<部署IP>:8765/mcp`
- **REST OpenAPI 3.1 插件规范**：`http://<部署IP>:8765/openapi.json`
- **多智能体编排手册**：详见 `agentarts_configs/AgentArts_多智能体编排与Prompt配置手册.md`

---

## 四、运行端到端硬核基准校验

```bash
python3 tests/run_benchmark.py
```

## 五、组内真实问答试用

将代码放进私有仓库后，可在 Render 中用 `render.yaml` 建立免费 Web Service。`team_server.py` 使用邀请码进入问答，每个浏览器使用独立的会话工作区。组员打开托管平台分配的 HTTPS 地址并输入邀请码即可使用。

在托管平台配置 `DEEPSEEK_API_KEY` 和至少 12 位的 `ZHIXING_TEAM_CODE`。Render 会自动生成独立的 `ZHIXING_TEAM_COOKIE_KEY`。试用支持真实模型问答和公开网页搜索，聊天记录存在免费实例的临时磁盘，实例重启后可能清空。

## 六、个人注册登录版（托管推荐入口）

`app.py` 是面向公网托管的入口：**开放自助注册，每位用户在自己的账号里填写自己的模型 API Key**，服务端不需要预置任何密钥。

```bash
PORT=8000 python3 app.py
```

与 `python3 run.py --product` 的差别：

- 模型配置按账号存储（`llm_config_override=None`），每个账号的 Key / 接口地址 / 模型名互不可见；
- 不要求提前知道公网域名：`ZHIXING_PUBLIC_ORIGIN` 留空时 CSRF 校验退化为同 Host 校验；
- 注册默认开放（可用 `ZHIXING_REGISTRATION_CODE` 加邀请码、`ZHIXING_ALLOW_REGISTRATION=0` 关闭注册）；
- 账号库与各账号工作区默认落在 `.account_data/`，可用 `ZHIXING_ACCOUNT_DATA_DIR` 改到持久磁盘。

登录后点右上角「未配置推理引擎」→ 选择服务商预设 → 填 API Key → 「测试连接」（成功会自动保存）。未配置模型时，资料上传、FTS5 知识检索与「学业决策驾驶舱」的确定性精算仍可正常使用。

## 七、本地学业工作台

在 `zhixing_agent` 目录运行：

```bash
python3 run.py --product
```

打开 [本地学业工作台](http://127.0.0.1:8766)，创建账号后可以查询公开通知、上传个人资料提问和核算加权平均成绩。产品模式为每个账号创建独立工作区，默认数据目录为 `workspace/product`。原有个人工作区可用 `python3 run.py` 打开。两种入口可以同时运行。

本地工作台默认使用本机已配置的模型，也支持现有模型环境变量。模型仍未配置时，成绩核算和资料索引可以正常使用。成绩核算接受两列数据，依次为学分、百分制成绩，计算公式为加权总分除以总学分。

### 对外部署配置参考

新版配置使用 `ZHIXING_` 前缀，已有部署的旧版变量继续有效。两者同时设置时，优先使用新版变量。

| 配置 | 用途 |
| --- | --- |
| `ZHIXING_PUBLIC_ORIGIN` | 真实 HTTPS 域名，例如 `https://study.example.com`。由反向代理终止 HTTPS。 |
| `ZHIXING_PRODUCT_DATA_DIR` | 账号库和私人工作区所在的持久磁盘目录。 |
| `DEEPSEEK_API_KEY` | 服务端统一模型密钥，仅 `run.py --product` / `team_server.py` 入口需要；`app.py` 由每位用户自行填写。 |
| `ZHIXING_REGISTRATION_CODE` | 配置后使用邀请码注册。留空时开放注册。 |
| `ZHIXING_ALLOW_REGISTRATION` | 设置为 `0` 时关闭新账号注册，已有账号仍可登录。 |
| `ZHIXING_DAILY_CHAT_LIMIT` | 每账号连续 24 小时问答额度，默认 50。`0` 表示由外部网关控制额度。 |

当前内置 HTTP 服务用于本地体验和受控试用。正式上线前应使用生产 HTTP 服务和 HTTPS 网关，配置持久磁盘与模型配额，补齐账号找回流程，并在目标环境验证双账号隔离、问答和备份恢复。

## 八、自动上线（Git 推送即部署）

需要「改完代码自动上线」时，用 Git + Render 这条链路，`render.yaml` 已按 `app.py` 配好：

1. 在 GitHub 建一个私有仓库，把 `zhixing_agent` 目录推上去（仓库根目录就是本目录，`render.yaml` 必须在根）。
2. Render 控制台选择 **New → Blueprint**，绑定该仓库，它会读取 `render.yaml` 并创建 `zhixing-personal`（个人注册登录版）与 `zhixing-team-preview`（组内邀请码版，不需要可删掉该段）。
3. 之后任何一次 `git push` 到默认分支，Render 自动重新构建并部署；队友或 AI 提交代码同样生效。

几点需要注意：

- 两个发布通道互不影响：Git 推送到 Render 不会更新 WorkBuddy 那边生成的分享链接，反之亦然。
- 免费实例 15 分钟无访问会休眠，首次访问需等 30–60 秒冷启动。
- 免费实例是临时磁盘，每次重新部署都会清空账号与聊天记录；要保留数据需挂载 Render Disk，并把 `ZHIXING_ACCOUNT_DATA_DIR` 指到挂载点（例如 `/var/data`）。
- 不想依赖外部平台时，也可以在本地用 `git` 暂存改动，再让 WorkBuddy 重新发布当前代码。

