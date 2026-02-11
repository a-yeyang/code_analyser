"""
file_manager.py - 文件读取、遍历与清理工具
"""

import shutil
import logging
from pathlib import Path
from typing import List, Dict

logger = logging.getLogger(__name__)


def get_code_files(
    repo_path: Path,
    extensions: List[str],
    max_files: int = 200,
) -> List[Path]:
    """
    递归遍历仓库目录，收集符合扩展名的源代码文件。
    自动跳过常见的非业务目录（node_modules、.git、vendor 等）。

    Args:
        repo_path: 仓库本地路径
        extensions: 需要收集的文件扩展名列表, 如 [".py", ".js"]
        max_files: 最多返回的文件数量（Demo 限制，防止文件过多）

    Returns:
        符合条件的 Path 列表
    """
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "vendor", "dist", "build", ".next", ".nuxt", "target",
        ".idea", ".vscode", "eggs", "*.egg-info",
    }

    collected: List[Path] = []

    for item in repo_path.rglob("*"):
        # 跳过目录本身
        if item.is_dir():
            continue

        # 跳过黑名单目录下的文件
        parts = item.relative_to(repo_path).parts
        if any(part in skip_dirs for part in parts):
            continue

        if item.suffix.lower() in extensions:
            collected.append(item)

        if len(collected) >= max_files:
            logger.warning(
                "已达到最大文件数 %d，停止收集。", max_files
            )
            break

    logger.info("共收集到 %d 个代码文件。", len(collected))
    return collected


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


def build_code_context(
    repo_path: Path,
    extensions: List[str],
    max_chars_per_file: int = 10000,
) -> List[Dict[str, str]]:
    """
    构建代码上下文列表，每个元素包含文件相对路径和内容。

    Returns:
        [{"path": "src/main.py", "content": "..."}, ...]
    """
    files = get_code_files(repo_path, extensions)
    context: List[Dict[str, str]] = []

    for f in files:
        relative = f.relative_to(repo_path).as_posix()
        content = read_file_content(f, max_chars=max_chars_per_file)
        context.append({"path": relative, "content": content})

    return context


def cleanup_repo(repo_path: Path) -> None:
    """
    强制删除克隆的仓库目录。
    Windows 上 .git 目录可能存在只读文件，需要特殊处理。
    使用 onerror 以兼容 Python 3.10/3.11（onexc 仅在 3.12+ 存在）。
    """
    import stat

    def _on_rm_error(func, path, exc_info):
        """处理 Windows 下只读文件无法删除的问题：去掉只读后再删。"""
        path_obj = Path(path)
        try:
            path_obj.chmod(stat.S_IWRITE)
        except OSError:
            pass
        func(path)

    if repo_path.exists():
        try:
            shutil.rmtree(repo_path, onerror=_on_rm_error)
            logger.info("已清理临时目录: %s", repo_path)
        except Exception as exc:
            logger.error("清理目录失败 %s: %s", repo_path, exc)
