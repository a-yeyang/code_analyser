"""
main.py - LocalCodeAudit FastAPI 入口
"""

import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os

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
        ".py,.js,.ts,.go,.java,.c,.cpp,.rs,.rb,.php,.sol",
    ).split(",")
]
MAX_FILE_CHARS = int(os.getenv("MAX_FILE_CHARS", "10000"))

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
    logger.info("LocalCodeAudit 服务已启动")
    yield
    # 可选：关闭时清理所有残留的临时仓库
    from utils.file_manager import cleanup_repo
    if TEMP_DIR.exists():
        for child in TEMP_DIR.iterdir():
            if child.is_dir():
                cleanup_repo(child)
    logger.info("LocalCodeAudit 服务已关闭，临时文件已清理")


app = FastAPI(
    title="LocalCodeAudit",
    description="本地自动化代码安全审计工具 - 基于 LangChain + ChatOpenAI",
    version="0.1.0",
    lifespan=lifespan,
)


# ────────────────── 请求 / 响应模型 ──────────────────
class AuditRequest(BaseModel):
    github_url: str = Field(
        ...,
        description="GitHub 公开仓库链接",
        examples=["https://github.com/pallets/flask"],
    )
    audit_prompt: str = Field(
        default="请对该仓库进行全面的代码安全审计",
        description="自定义审计需求提示词",
    )


class AuditResponse(BaseModel):
    success: bool
    message: str
    report: str = ""


# ────────────────── API 端点 ──────────────────
@app.post("/api/audit", response_model=AuditResponse, summary="代码审计")
async def audit_code(request: AuditRequest):
    """
    接收 GitHub 仓库链接和审计需求，执行自动化代码安全审计。

    流程：
    1. 验证链接有效性（git ls-remote）
    2. 浅克隆仓库到本地
    3. 提取代码文件，调用 LLM 分析
    4. 返回审计报告
    5. 自动清理临时文件
    """
    from services.git_service import verify_repo_accessible, clone_repo
    from services.audit_engine import run_audit
    from utils.file_manager import cleanup_repo

    # 0. 检查 API Key 配置
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

        # 3. 执行审计
        logger.info("开始审计，审计需求: %s", request.audit_prompt[:100])
        report = await run_audit(
            repo_path=repo_path,
            audit_prompt=request.audit_prompt,
            extensions=AUDIT_FILE_EXTENSIONS,
            max_chars_per_file=MAX_FILE_CHARS,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            model=OPENAI_MODEL,
        )

        return AuditResponse(
            success=True,
            message="审计完成",
            report=report,
        )

    finally:
        # 4. 无论成功或失败，都清理临时目录
        cleanup_repo(repo_path)


# ────────────────── 健康检查 ──────────────────
@app.get("/health", summary="健康检查")
async def health_check():
    return {
        "status": "ok",
        "service": "LocalCodeAudit",
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
