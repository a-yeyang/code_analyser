# LocalCodeAudit

> 自适应多专家代码安全审计工具 · 基于 LangChain + OpenAI 兼容 API

[![Python](https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

输入 **GitHub 仓库链接** 与审计需求，自动克隆、识别项目特征、选择专家策略并生成结构化安全审计报告。

---

## ✨ 特性

| 能力 | 说明 |
|------|------|
| **多专家策略** | 根据项目语言与框架自动组合「通用安全基座 + 语言专家 + 框架专家」提示词（Java / Python / Go / JS·TS / Solidity 等） |
| **项目画像** | 自动识别主要语言、构建工具（Maven / Gradle / pip / npm / Cargo…）、框架（Spring / Django / Flask / Express…）及是否使用 MyBatis |
| **智能筛选** | 大仓库下自动筛选核心代码文件，控制上下文规模，支持 `.py/.js/.ts/.go/.java/.c/.cpp/.rs/.rb/.php/.sol/.xml` 等 |
| **双模式输出** | **full**：完整结构化报告（CWE、风险等级、修复建议、安全评分）；**brief**：仅位置 + 风险 + 最小修复 |
| **API 友好** | FastAPI 提供 `POST /api/audit`，便于 CI / 飞书 / 其他系统集成 |

---

## 🛠 技术栈

- **API**：FastAPI + Uvicorn  
- **LLM**：LangChain + LangChain-OpenAI（兼容 OpenAI API，支持阿里云 DashScope 等）  
- **仓库**：GitPython 浅克隆、`git ls-remote` 校验  
- **配置**：python-dotenv + Pydantic  

---

## 📦 安装与运行

### 环境要求

- Python 3.10+
- 已配置的 OpenAI API Key（或兼容接口，如阿里云 DashScope）

### 1. 克隆并安装依赖

```bash
cd local_audit_tool
pip install -r requirements.txt
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件（可复制 `.env.example` 后修改）：

```env
# 必填：OpenAI 或兼容 API
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o

# 可选：阿里云 DashScope 示例（需带 /v1）
# OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# OPENAI_MODEL=qwen-plus

# 可选：审计与上下文控制
TEMP_REPO_DIR=temp_repos
AUDIT_FILE_EXTENSIONS=.py,.js,.ts,.go,.java,.c,.cpp,.rs,.rb,.php,.sol,.xml
MAX_FILE_CHARS=10000
MAX_CONTEXT_FILES=80
```

### 3. 启动服务

```bash
python main.py
```

默认地址：**http://0.0.0.0:8000**  
- 健康检查：`GET http://localhost:8000/health`  
- API 文档：`http://localhost:8000/docs`

---

## 📡 API 使用

### 提交审计任务

```http
POST /api/audit
Content-Type: application/json
```

**请求体示例：**

```json
{
  "github_url": "https://github.com/pallets/flask",
  "audit_prompt": "请对该仓库进行全面的代码安全审计，重点关注依赖与配置安全",
  "mode": "full"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `github_url` | string | ✅ | 公开的 GitHub 仓库 URL |
| `audit_prompt` | string | 否 | 自定义审计需求，默认「全面代码安全审计」 |
| `mode` | string | 否 | `full`（详细报告）或 `brief`（简要报告），默认 `full` |

**响应示例（成功）：**

```json
{
  "success": true,
  "message": "审计完成",
  "mode": "full",
  "report": "## 审计报告\n\n### 1. 漏洞与风险…",
  "project_profile": {
    "primary_language": "python",
    "language_stats": { "python": 120 },
    "frameworks": ["flask-or-fastapi", "pip"],
    "has_mybatis": false,
    "total_code_files": 120,
    "scale": "medium"
  },
  "experts_used": ["BASE", "PYTHON", "FLASK"],
  "files_audited": 45
}
```

### cURL 示例

```bash
curl -X POST "http://localhost:8000/api/audit" \
  -H "Content-Type: application/json" \
  -d '{"github_url":"https://github.com/pallets/flask","mode":"full"}'
```

---

## 📁 项目结构

```
local_audit_tool/
├── main.py              # FastAPI 入口、/api/audit、/health
├── requirements.txt
├── .env                 # 本地配置（勿提交密钥）
├── services/
│   ├── audit_engine.py  # 审计引擎：画像 → Prompt 组合 → 上下文构建 → LLM 调用
│   ├── project_profiler.py  # 项目特征识别（语言 / 框架 / 规模）
│   ├── prompt_library.py    # 多专家 Prompt 管理与组合
│   └── git_service.py       # 仓库校验与浅克隆
└── utils/
    ├── file_manager.py  # 克隆目录管理、代码上下文构建与清理
    └── static_analyzer.py   # 文件筛选与跨文件引用注入
```

---

## 🔄 审计流程概览

1. **校验**：`git ls-remote` 验证仓库可访问  
2. **克隆**：浅克隆到本地临时目录  
3. **画像**：识别语言、框架、规模 → 选择专家 Prompt  
4. **上下文**：按扩展名与重要性筛选文件，注入跨文件引用（可选）  
5. **LLM**：LangChain LCEL 调用模型生成报告  
6. **清理**：删除临时仓库  

---

## 📄 License

本项目采用 [MIT License](LICENSE) 开源协议。

---

**LocalCodeAudit** — 让每一次代码审计都更有针对性。
