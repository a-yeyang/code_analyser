"""
main.py - LocalCodeAudit FastAPI 入口

自适应多专家代码安全审计工具：
  POST /api/audit   → 提交审计任务
  GET  /health      → 健康检查
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ────────────────── 加载环境变量 ──────────────────
load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o").strip()
TEMP_REPO_DIR = os.getenv("TEMP_REPO_DIR", "temp_repos")
AUDIT_FILE_EXTENSIONS = [
    ext.strip()
    for ext in os.getenv(
        "AUDIT_FILE_EXTENSIONS",
        ".py,.js,.ts,.go,.java,.c,.cpp,.rs,.rb,.php,.sol,.xml",
    ).split(",")
]
MAX_FILE_CHARS = int(os.getenv("MAX_FILE_CHARS", "10000"))
MAX_CONTEXT_FILES = int(os.getenv("MAX_CONTEXT_FILES", "80"))

# ────────────────── 飞书 RAG 配置 ──────────────────
FEISHU_APP_ID = (os.getenv("FEISHU_APP_ID") or "").strip()
FEISHU_APP_SECRET = (os.getenv("FEISHU_APP_SECRET") or "").strip()
FEISHU_WIKI_URL = (os.getenv("FEISHU_WIKI_URL") or "").strip()
FEISHU_DOC_TITLE_KEYWORD = (
    os.getenv("FEISHU_DOC_TITLE_KEYWORD") or "代码规范"
).strip()

# ────────────────── 日志配置 ──────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("LocalCodeAudit")

# ────────────────── 路径 ──────────────────
BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / TEMP_REPO_DIR


# ────────────────── FastAPI 生命周期 ──────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时确保临时目录存在，关闭时清理残留。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("LocalCodeAudit 服务已启动 (model=%s)", OPENAI_MODEL)
    yield
    from utils.file_manager import cleanup_repo
    if TEMP_DIR.exists():
        for child in TEMP_DIR.iterdir():
            if child.is_dir():
                cleanup_repo(child)
    logger.info("LocalCodeAudit 服务已关闭，临时文件已清理")


app = FastAPI(
    title="LocalCodeAudit",
    description="自适应多专家代码安全审计工具 - 基于 LangChain + ChatOpenAI",
    version="0.2.0",
    lifespan=lifespan,
)


# ────────────────── 请求 / 响应模型 ──────────────────

class AuditRequest(BaseModel):
    """审计请求参数。"""
    github_url: str = Field(
        ...,
        description="GitHub 公开仓库链接",
        examples=["https://github.com/pallets/flask"],
    )
    audit_prompt: str = Field(
        default="请对该仓库进行全面的代码安全审计",
        description="自定义审计需求提示词",
    )
    mode: str = Field(
        default="full",
        description="审计模式: full (详细报告) 或 brief (简要报告，仅位置+风险+修复)",
    )
    use_feishu_standard: bool = Field(
        default=False,
        description="是否从飞书知识库检索企业代码规范并作为 RAG 上下文注入审计",
    )


class ProjectProfileResponse(BaseModel):
    """项目画像摘要（嵌入审计响应中）。"""
    primary_language: str = ""
    language_stats: dict = {}
    frameworks: List[str] = []
    has_mybatis: bool = False
    total_code_files: int = 0
    scale: str = ""


class AuditResponse(BaseModel):
    """审计响应结果。"""
    success: bool
    message: str
    mode: str = "full"
    report: str = ""
    project_profile: Optional[ProjectProfileResponse] = None
    experts_used: Optional[List[str]] = None
    files_audited: int = 0
    feishu_standard_used: str = ""


# ────────────────── API 端点 ──────────────────

@app.post("/api/audit", response_model=AuditResponse, summary="代码审计")
async def audit_code(request: AuditRequest):
    """
    接收 GitHub 仓库链接和审计需求，执行自适应多专家代码安全审计。

    流程：
    1. 验证链接有效性（git ls-remote）
    2. 浅克隆仓库到本地
    3. 自动识别项目特征 → 选择专家 Prompt
    4. 智能筛选核心文件 → 调用 LLM 分析
    5. 返回审计报告 + 项目画像
    6. 自动清理临时文件
    """
    from services.audit_engine import FeishuConfig, run_audit
    from services.git_service import clone_repo, verify_repo_accessible
    from utils.file_manager import cleanup_repo

    # 0. 检查 API Key
    if not OPENAI_API_KEY or OPENAI_API_KEY == "sk-your-api-key-here":
        raise HTTPException(
            status_code=500,
            detail="未配置有效的 OPENAI_API_KEY，请在 .env 文件中设置。",
        )

    # 1. 验证仓库可访问性
    logger.info("正在验证仓库: %s", request.github_url)
    accessible, err_msg = verify_repo_accessible(request.github_url)
    if not accessible:
        return AuditResponse(success=False, message=err_msg)

    # 2. 克隆仓库
    repo_path, clone_err = clone_repo(request.github_url, TEMP_DIR)

    try:
        if clone_err:
            return AuditResponse(success=False, message=clone_err)

        # 3 + 4. 构建飞书配置 + 执行自适应审计
        feishu_cfg = FeishuConfig(
            enabled=request.use_feishu_standard,
            app_id=FEISHU_APP_ID,
            app_secret=FEISHU_APP_SECRET,
            wiki_url=FEISHU_WIKI_URL,
            doc_title_keyword=FEISHU_DOC_TITLE_KEYWORD,
        )

        logger.info(
            "开始审计 (mode=%s, feishu_rag=%s)，审计需求: %s",
            request.mode, request.use_feishu_standard,
            request.audit_prompt[:100],
        )
        result = await run_audit(
            repo_path=repo_path,
            audit_prompt=request.audit_prompt,
            extensions=AUDIT_FILE_EXTENSIONS,
            max_chars_per_file=MAX_FILE_CHARS,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            model=OPENAI_MODEL,
            max_context_files=MAX_CONTEXT_FILES,
            mode=request.mode,
            feishu_config=feishu_cfg,
        )

        # 5. 构建响应
        profile_resp = None
        if result.profile:
            profile_resp = ProjectProfileResponse(
                primary_language=result.profile.primary_language.value,
                language_stats=result.profile.language_stats,
                frameworks=result.profile.frameworks,
                has_mybatis=result.profile.has_mybatis,
                total_code_files=result.profile.total_code_files,
                scale=result.profile.scale.value,
            )

        return AuditResponse(
            success=result.success,
            message="审计完成" if result.success else "审计失败",
            mode=request.mode,
            report=result.report,
            project_profile=profile_resp,
            experts_used=result.experts_used,
            files_audited=result.files_audited,
            feishu_standard_used=result.feishu_standard_used,
        )

    finally:
        # 6. 无论成功或失败，都清理临时目录
        cleanup_repo(repo_path)


# ────────────────── 健康检查 ──────────────────

@app.get("/health", summary="健康检查")
async def health_check():
    return {
        "status": "ok",
        "service": "LocalCodeAudit",
        "version": "0.2.0",
        "model": OPENAI_MODEL,
    }


# ────────────────── 入口 ──────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
