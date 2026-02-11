"""
file_manager.py - 文件读取、遍历、智能筛选与清理工具

职责：
  - 递归收集仓库中的代码文件
  - 根据项目画像对文件做优先级排序（核心业务代码优先）
  - 安全读取文件内容（截断超长文件）
  - 审计完成后清理临时克隆目录
"""

from __future__ import annotations

import re
import shutil
import stat
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

from services.project_profiler import Language, ProjectProfile

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────

# 遍历时跳过的目录
SKIP_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build", ".next", ".nuxt", "target",
    ".idea", ".vscode", "eggs", ".gradle", ".mvn",
}

# ────────── 文件优先级规则 ──────────
# 数字越小优先级越高（0 = 最高优先级）

# Java 项目：Controller > Service > Mapper/DAO > Security/Config > 其他
_JAVA_PRIORITY_PATTERNS: List[tuple[re.Pattern, int]] = [
    (re.compile(r"Controller\.java$", re.IGNORECASE), 0),
    (re.compile(r"Resource\.java$", re.IGNORECASE), 0),      # JAX-RS
    (re.compile(r"Service(Impl)?\.java$", re.IGNORECASE), 1),
    (re.compile(r"Mapper\.java$", re.IGNORECASE), 2),
    (re.compile(r"(Dao|Repository)\.java$", re.IGNORECASE), 2),
    (re.compile(r"Mapper\.xml$", re.IGNORECASE), 2),          # MyBatis XML
    (re.compile(r"(Security|Auth|Filter|Interceptor).*\.java$", re.IGNORECASE), 3),
    (re.compile(r"Config(uration)?\.java$", re.IGNORECASE), 4),
    (re.compile(r"(Entity|Model|DTO|VO)\.java$", re.IGNORECASE), 5),
]

# Python 项目：views/routes > models/auth > utils
_PYTHON_PRIORITY_PATTERNS: List[tuple[re.Pattern, int]] = [
    (re.compile(r"(views|routes|api|endpoints)\.py$", re.IGNORECASE), 0),
    (re.compile(r"(main|app)\.py$", re.IGNORECASE), 0),
    (re.compile(r"(auth|security|permissions|middleware)\.py$", re.IGNORECASE), 1),
    (re.compile(r"(models|schemas)\.py$", re.IGNORECASE), 2),
    (re.compile(r"(settings|config)\.py$", re.IGNORECASE), 3),
    (re.compile(r"(utils|helpers|common)\.py$", re.IGNORECASE), 4),
]

# Go 项目：handler > service/middleware > model
_GO_PRIORITY_PATTERNS: List[tuple[re.Pattern, int]] = [
    (re.compile(r"(handler|controller|router).*\.go$", re.IGNORECASE), 0),
    (re.compile(r"main\.go$", re.IGNORECASE), 0),
    (re.compile(r"(service|middleware|auth).*\.go$", re.IGNORECASE), 1),
    (re.compile(r"(model|entity|dao|repository).*\.go$", re.IGNORECASE), 2),
    (re.compile(r"(config|util|helper).*\.go$", re.IGNORECASE), 3),
]

# JS/TS 项目
_JS_PRIORITY_PATTERNS: List[tuple[re.Pattern, int]] = [
    (re.compile(r"(route|controller|api)\.(js|ts|jsx|tsx)$", re.IGNORECASE), 0),
    (re.compile(r"(middleware|auth|security)\.(js|ts)$", re.IGNORECASE), 1),
    (re.compile(r"(model|schema|service)\.(js|ts)$", re.IGNORECASE), 2),
    (re.compile(r"(config|env)\.(js|ts)$", re.IGNORECASE), 3),
]

# 语言 → 优先级规则映射
_LANGUAGE_PRIORITY_MAP: Dict[Language, List[tuple[re.Pattern, int]]] = {
    Language.JAVA: _JAVA_PRIORITY_PATTERNS,
    Language.PYTHON: _PYTHON_PRIORITY_PATTERNS,
    Language.GO: _GO_PRIORITY_PATTERNS,
    Language.JAVASCRIPT: _JS_PRIORITY_PATTERNS,
    Language.TYPESCRIPT: _JS_PRIORITY_PATTERNS,
}

# 未命中任何优先级规则的默认优先级
_DEFAULT_PRIORITY = 99


# ──────────────────────────────────────────────
#  文件收集
# ──────────────────────────────────────────────

def get_code_files(
    repo_path: Path,
    extensions: List[str],
    max_files: int = 200,
) -> List[Path]:
    """
    递归遍历仓库目录，收集符合扩展名的源代码文件。
    自动跳过常见的非业务目录。

    Args:
        repo_path:  仓库本地路径
        extensions: 需要收集的文件扩展名列表, 如 [".py", ".js"]
        max_files:  最多返回的文件数量

    Returns:
        符合条件的 Path 列表（未排序）
    """
    collected: List[Path] = []

    for item in repo_path.rglob("*"):
        if item.is_dir():
            continue

        parts = item.relative_to(repo_path).parts
        if any(part in SKIP_DIRS for part in parts):
            continue

        if item.suffix.lower() in extensions:
            collected.append(item)

        if len(collected) >= max_files:
            logger.warning("已达到最大文件数 %d，停止收集。", max_files)
            break

    logger.info("共收集到 %d 个代码文件。", len(collected))
    return collected


# ──────────────────────────────────────────────
#  智能优先级排序与筛选
# ──────────────────────────────────────────────

def _get_file_priority(
    file_path: Path,
    repo_path: Path,
    patterns: List[tuple[re.Pattern, int]],
) -> int:
    """计算单个文件的优先级数值（越小越优先）。"""
    rel = file_path.relative_to(repo_path).as_posix()
    for pattern, priority in patterns:
        if pattern.search(rel):
            return priority
    return _DEFAULT_PRIORITY


def prioritize_files(
    files: List[Path],
    repo_path: Path,
    profile: ProjectProfile,
    max_files: int = 80,
) -> List[Path]:
    """
    根据项目画像对文件列表做优先级排序并截取。

    大型项目（>150 文件）时，优先保留核心业务代码（Controller / Service /
    Mapper 等），丢弃低优先级文件以节省 LLM 上下文窗口。

    小型项目直接返回全部文件。

    Args:
        files:     get_code_files 返回的原始文件列表
        repo_path: 仓库根路径
        profile:   项目画像
        max_files: 最终保留的最大文件数

    Returns:
        排序并截取后的 Path 列表
    """
    patterns = _LANGUAGE_PRIORITY_MAP.get(profile.primary_language, [])

    if not patterns:
        # 没有对应语言的优先级规则，直接截取
        return files[:max_files]

    # 计算每个文件的优先级
    scored = [
        (_get_file_priority(f, repo_path, patterns), f) for f in files
    ]
    scored.sort(key=lambda x: x[0])

    result = [f for _, f in scored[:max_files]]
    dropped = len(files) - len(result)

    if dropped > 0:
        logger.info(
            "智能筛选：保留 %d 个核心文件，跳过 %d 个低优先级文件。",
            len(result), dropped,
        )

    return result


# ──────────────────────────────────────────────
#  文件读取
# ──────────────────────────────────────────────

def read_file_content(file_path: Path, max_chars: int = 10000) -> str:
    """
    安全读取单个文件内容，自动截断超长文件。

    Args:
        file_path: 文件绝对路径
        max_chars: 最大字符数

    Returns:
        文件文本内容（可能被截断）
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n... [文件过长，已截断] ..."
        return text
    except Exception as exc:
        logger.error("读取文件失败 %s: %s", file_path, exc)
        return f"[读取失败: {exc}]"


# ──────────────────────────────────────────────
#  上下文构建
# ──────────────────────────────────────────────

def build_code_context(
    repo_path: Path,
    extensions: List[str],
    max_chars_per_file: int = 10000,
    profile: Optional[ProjectProfile] = None,
    max_context_files: int = 80,
) -> List[Dict[str, str]]:
    """
    构建代码上下文列表，每个元素包含文件相对路径和内容。

    当提供 profile 时，会启用智能优先级排序，优先纳入核心业务文件。

    Args:
        repo_path:         仓库本地路径
        extensions:        文件扩展名白名单
        max_chars_per_file: 单文件最大字符数
        profile:           项目画像（可选，传入则启用智能筛选）
        max_context_files: 最终进入 LLM 上下文的最大文件数

    Returns:
        [{"path": "src/main.py", "content": "..."}, ...]
    """
    files = get_code_files(repo_path, extensions)

    # 如果有画像，启用智能筛选
    if profile is not None:
        files = prioritize_files(files, repo_path, profile, max_context_files)

    context: List[Dict[str, str]] = []
    for f in files:
        relative = f.relative_to(repo_path).as_posix()
        content = read_file_content(f, max_chars=max_chars_per_file)
        context.append({"path": relative, "content": content})

    return context


# ──────────────────────────────────────────────
#  目录清理
# ──────────────────────────────────────────────

def cleanup_repo(repo_path: Path) -> None:
    """
    强制删除克隆的仓库目录。
    Windows 上 .git 目录可能存在只读文件，需要特殊处理。
    使用 onerror 以兼容 Python 3.10/3.11（onexc 仅在 3.12+ 存在）。
    """

    def _on_rm_error(func, path, exc_info):  # noqa: ANN001
        """去掉只读属性后重试删除。"""
        try:
            Path(path).chmod(stat.S_IWRITE)
        except OSError:
            pass
        func(path)

    if repo_path.exists():
        try:
            shutil.rmtree(repo_path, onerror=_on_rm_error)
            logger.info("已清理临时目录: %s", repo_path)
        except Exception as exc:
            logger.error("清理目录失败 %s: %s", repo_path, exc)
