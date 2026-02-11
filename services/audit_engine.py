"""
audit_engine.py - 基于 LangChain + ChatOpenAI 的代码安全审计引擎
"""

import logging
from pathlib import Path
from typing import List, Dict

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from utils.file_manager import build_code_context

logger = logging.getLogger(__name__)

# ────────────────────────── Prompt 模板 ──────────────────────────

SYSTEM_PROMPT = """\
你是一名资深的代码安全审计专家。你的任务是根据用户的审计需求，对提供的源代码进行全面的安全性分析。

审计规则：
1. 逐文件分析，指出潜在的安全漏洞（如 SQL 注入、XSS、硬编码密钥、不安全的反序列化、路径遍历等）。
2. 对每个发现给出：
   - 风险等级（高/中/低）
   - 漏洞类型
   - 所在文件和大致位置
   - 修复建议
3. 最后给出一个整体安全评分（0-100）和总结。
4. 如果代码量过少或不存在明显漏洞，也要明确说明。
5. 使用中文回复。
"""

USER_PROMPT_TEMPLATE = """\
## 用户审计需求
{audit_prompt}

## 代码文件列表
共 {file_count} 个文件。

{code_blocks}
"""


def _format_code_blocks(context: List[Dict[str, str]]) -> str:
    """将代码上下文格式化为 Prompt 中可读的文本块。"""
    blocks = []
    for i, item in enumerate(context, 1):
        blocks.append(
            f"### 文件 {i}: `{item['path']}`\n"
            f"```\n{item['content']}\n```"
        )
    return "\n\n".join(blocks)


def _normalize_base_url(url: str) -> str:
    """
    规范化 base_url：去除首尾空格，并确保以 /v1 结尾。
    阿里云 DashScope 等 OpenAI 兼容接口要求完整路径含 /v1，否则会 404。
    """
    url = (url or "").strip().rstrip("/")
    if url and not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _build_chain(
    api_key: str,
    base_url: str,
    model: str,
):
    """构建 LangChain LCEL 审计链。"""
    base_url = _normalize_base_url(base_url)
    logger.info("LLM base_url=%s, model=%s", base_url, model)

    llm = ChatOpenAI(
        api_key=api_key.strip(),
        base_url=base_url,
        model=model.strip(),
        temperature=0.2,
        max_tokens=4096,
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", USER_PROMPT_TEMPLATE),
    ])

    chain = prompt | llm | StrOutputParser()
    return chain


async def run_audit(
    repo_path: Path,
    audit_prompt: str,
    extensions: List[str],
    max_chars_per_file: int,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """
    执行完整的审计流程：
      1. 收集代码文件
      2. 构建上下文
      3. 调用 LLM 分析
      4. 返回审计报告

    Args:
        repo_path: 克隆后的仓库本地路径
        audit_prompt: 用户自定义审计需求
        extensions: 需要审计的文件扩展名列表
        max_chars_per_file: 单文件最大字符数
        api_key: OpenAI API Key
        base_url: OpenAI API Base URL
        model: 模型名称

    Returns:
        LLM 生成的审计报告文本
    """
    # 1. 收集代码上下文
    context = build_code_context(repo_path, extensions, max_chars_per_file)

    if not context:
        return "未在仓库中找到符合条件的代码文件，无法执行审计。"

    logger.info(
        "准备审计 %d 个文件，仓库路径: %s", len(context), repo_path
    )

    # 2. 格式化代码块
    code_blocks = _format_code_blocks(context)

    # 3. 构建并调用链
    chain = _build_chain(api_key, base_url, model)

    try:
        report = await chain.ainvoke({
            "audit_prompt": audit_prompt,
            "file_count": len(context),
            "code_blocks": code_blocks,
        })
        return report
    except Exception as exc:
        err_msg = str(exc)
        logger.error("LLM 调用失败: %s", exc, exc_info=True)
        # 常见错误提示，便于排查配置问题
        if "404" in err_msg:
            hint = "（通常为 base_url 缺少 /v1 或模型名错误，请检查 .env 中 OPENAI_BASE_URL 与 OPENAI_MODEL）"
        elif "401" in err_msg or "403" in err_msg:
            hint = "（请检查 .env 中 OPENAI_API_KEY 是否正确）"
        elif "429" in err_msg:
            hint = "（请求过于频繁或配额不足）"
        else:
            hint = ""
        return f"审计过程中 LLM 调用失败: {exc}{hint}"
