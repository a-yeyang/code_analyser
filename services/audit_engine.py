"""
audit_engine.py - 自适应多专家代码安全审计引擎

工作流程：
  1. identify_project_context()  → 生成项目画像
  2. PromptLibrary.compose()     → 动态拼接专家 Prompt
  3. build_code_context()        → 智能筛选核心文件 + 构建上下文
  4. LangChain LCEL Chain        → 调用 LLM 输出审计报告
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from services.project_profiler import ProjectProfile, identify_project_context
from services.prompt_library import PromptLibrary, USER_PROMPT_TEMPLATE
from utils.file_manager import build_code_context, inject_cross_file_context

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  审计结果数据模型
# ──────────────────────────────────────────────

@dataclass
class AuditResult:
    """
    审计引擎返回的完整结果。

    Attributes:
        report:        LLM 生成的审计报告文本
        profile:       项目画像
        experts_used:  本次激活的专家列表
        files_audited: 实际进入审计的文件数
        success:       是否成功完成审计
        error:         失败时的错误信息
    """
    report: str = ""
    profile: ProjectProfile | None = None
    experts_used: List[str] | None = None
    files_audited: int = 0
    success: bool = True
    error: str = ""


# ──────────────────────────────────────────────
#  内部工具函数
# ──────────────────────────────────────────────

def _normalize_base_url(url: str) -> str:
    """
    规范化 base_url：去除首尾空格，并确保以 /v1 结尾。
    阿里云 DashScope 等 OpenAI 兼容接口要求完整路径含 /v1，否则会 404。
    """
    url = (url or "").strip().rstrip("/")
    if url and not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _format_code_blocks(context: List[Dict[str, str]]) -> str:
    """
    将代码上下文格式化为 Prompt 中可读的文本块。

    支持两种类型的上下文项：
      - 普通文件（type 为空或 "main"）：标记为"文件 N"
      - 关联依赖（type == "dependency"）：标记为"关联引用"，
        并提示 LLM 这是跨文件调用链的补充上下文
    """
    blocks: List[str] = []
    file_idx = 0
    dep_idx = 0

    for item in context:
        if item.get("type") == "dependency":
            dep_idx += 1
            blocks.append(
                f"### 关联引用 {dep_idx}: `{item['path']}`\n"
                f"> ⚠️ 以下代码为核心文件的跨文件依赖定义，"
                f"供你理解调用链上下文，请结合主文件一起审计。\n\n"
                f"```\n{item['content']}\n```"
            )
        else:
            file_idx += 1
            blocks.append(
                f"### 文件 {file_idx}: `{item['path']}`\n"
                f"```\n{item['content']}\n```"
            )

    return "\n\n".join(blocks)


def _build_chain(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
):
    """
    构建 LangChain LCEL 审计链。

    Args:
        api_key:       API 密钥
        base_url:      API 基地址
        model:         模型名称
        system_prompt: 由 PromptLibrary 组合后的动态 SYSTEM_PROMPT
    """
    base_url = _normalize_base_url(base_url)
    logger.info("LLM 配置 → base_url=%s, model=%s", base_url, model)

    llm = ChatOpenAI(
        api_key=api_key.strip(),
        base_url=base_url,
        model=model.strip(),
        temperature=0.2,
        max_tokens=4096,
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", USER_PROMPT_TEMPLATE),
    ])

    return prompt | llm | StrOutputParser()


def _make_error_hint(err_msg: str) -> str:
    """根据错误信息生成排查提示。"""
    if "404" in err_msg:
        return "（通常为 base_url 缺少 /v1 或模型名错误，请检查 .env）"
    if "401" in err_msg or "403" in err_msg:
        return "（请检查 .env 中 OPENAI_API_KEY 是否正确）"
    if "429" in err_msg:
        return "（请求过于频繁或配额不足）"
    return ""


# ──────────────────────────────────────────────
#  核心对外接口
# ──────────────────────────────────────────────

async def run_audit(
    repo_path: Path,
    audit_prompt: str,
    extensions: List[str],
    max_chars_per_file: int,
    api_key: str,
    base_url: str,
    model: str,
    max_context_files: int = 80,
    mode: str = "full",
) -> AuditResult:
    """
    执行完整的自适应多专家审计流程。

    Steps:
      1. 扫描仓库 → 生成项目画像 (ProjectProfile)
      2. 根据画像 + 审计模式 → 动态组合专家 SYSTEM_PROMPT
      3. 智能筛选核心文件 → 构建代码上下文
      4. 调用 LLM → 生成审计报告

    Args:
        repo_path:         克隆后的仓库本地路径
        audit_prompt:      用户自定义审计需求
        extensions:        需要审计的文件扩展名列表
        max_chars_per_file: 单文件最大字符数
        api_key:           OpenAI API Key
        base_url:          OpenAI API Base URL
        model:             模型名称
        max_context_files: 进入 LLM 上下文的最大文件数
        mode:              审计模式 ("full" 详细 | "brief" 简要)

    Returns:
        AuditResult 数据实例
    """
    # ── Step 1: 项目画像 ──
    logger.info("Step 1/4 · 正在识别项目特征…")
    profile = identify_project_context(repo_path)

    # ── Step 2: 动态组合 Prompt（含审计模式）──
    logger.info("Step 2/4 · 正在组合专家提示词（模式: %s）…", mode)
    library = PromptLibrary()
    composed = library.compose(profile, mode=mode)

    # ── Step 3: 智能文件筛选 + 上下文构建 ──
    logger.info("Step 3/4 · 正在收集并筛选核心代码文件…")
    context = build_code_context(
        repo_path=repo_path,
        extensions=extensions,
        max_chars_per_file=max_chars_per_file,
        profile=profile,
        max_context_files=max_context_files,
    )

    if not context:
        return AuditResult(
            report="未在仓库中找到符合条件的代码文件，无法执行审计。",
            profile=profile,
            experts_used=composed.experts_used,
            success=False,
            error="no_code_files",
        )

    logger.info(
        "准备审计 %d 个文件（总文件 %d）",
        len(context), profile.total_code_files,
    )

    # ── Step 3.5: 跨文件依赖上下文增强 ──
    logger.info("Step 3.5 · 正在分析跨文件依赖关系，扩充调用链上下文…")
    try:
        original_count = len(context)
        context = inject_cross_file_context(
            context=context,
            repo_path=repo_path,
            profile=profile,
            max_snippet_chars=max_chars_per_file // 2,
            max_deps_per_file=5,
        )
        dep_count = len(context) - original_count
        if dep_count > 0:
            logger.info(
                "跨文件增强完成: 注入 %d 个关联依赖片段，上下文总条目 %d",
                dep_count, len(context),
            )
            # 将依赖映射信息回写到 profile，供后续使用
            from utils.static_analyzer import build_dependency_map
            from utils.file_manager import get_code_files
            all_files = get_code_files(repo_path, extensions)
            profile.dependency_map = build_dependency_map(
                all_files, repo_path, profile.primary_language,
            )
        else:
            logger.info("未发现可解析的跨文件依赖（可能为外部依赖或不支持的语言）")
    except Exception as exc:
        logger.warning(
            "跨文件依赖分析失败，降级为原始上下文（不影响主流程）: %s", exc,
        )

    code_blocks = _format_code_blocks(context)

    # ── Step 4: 调用 LLM ──
    logger.info("Step 4/4 · 正在调用 LLM 进行安全审计（可能需要 1~3 分钟）…")
    chain = _build_chain(api_key, base_url, model, composed.system_prompt)

    try:
        report = await chain.ainvoke({
            "project_summary": profile.summary(),
            "audit_prompt": audit_prompt,
            "file_count": len(context),
            "code_blocks": code_blocks,
        })
        return AuditResult(
            report=report,
            profile=profile,
            experts_used=composed.experts_used,
            files_audited=len(context),
            success=True,
        )
    except Exception as exc:
        err_msg = str(exc)
        logger.error("LLM 调用失败: %s", exc, exc_info=True)
        hint = _make_error_hint(err_msg)
        return AuditResult(
            report=f"审计过程中 LLM 调用失败: {exc}{hint}",
            profile=profile,
            experts_used=composed.experts_used,
            files_audited=len(context),
            success=False,
            error=err_msg,
        )
