"""
git_service.py - GitHub 链接验证与仓库克隆服务
"""

import re
import uuid
import logging
import subprocess
from pathlib import Path
from typing import Tuple

from git import Repo

logger = logging.getLogger(__name__)

# 支持的 GitHub URL 格式
_GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/[\w.\-]+/[\w.\-]+(\.git)?/?$"
)


def validate_github_url(url: str) -> bool:
    """
    基础格式校验：是否为合法的 GitHub 仓库 URL。
    """
    return bool(_GITHUB_URL_PATTERN.match(url.strip()))


def verify_repo_accessible(url: str) -> Tuple[bool, str]:
    """
    使用 git ls-remote 验证远端仓库是否可访问（公开）。

    Returns:
        (is_accessible, error_message)
    """
    url = url.strip()

    if not validate_github_url(url):
        return False, "链接格式不合法，请提供有效的 GitHub 仓库链接"

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        else:
            logger.warning("git ls-remote 失败: %s", result.stderr)
            return False, "链接错误或仓库为闭源/私有，无法访问"
    except subprocess.TimeoutExpired:
        return False, "验证超时，请检查网络连接"
    except FileNotFoundError:
        return False, "系统未安装 Git，请先安装 Git"
    except Exception as exc:
        logger.error("验证仓库时发生异常: %s", exc)
        return False, f"验证仓库时发生异常: {exc}"


def clone_repo(url: str, base_dir: Path) -> Tuple[Path, str]:
    """
    将仓库克隆到 base_dir 下的唯一子目录中。

    Args:
        url: GitHub 仓库链接
        base_dir: 临时目录根路径（如 temp_repos/）

    Returns:
        (repo_local_path, error_message)
        成功时 error_message 为空字符串。
    """
    # 确保基础目录存在
    base_dir.mkdir(parents=True, exist_ok=True)

    # 生成唯一目录名，避免并发冲突
    unique_name = uuid.uuid4().hex[:12]
    repo_dir = base_dir / unique_name

    try:
        logger.info("开始克隆仓库 %s -> %s", url, repo_dir)
        Repo.clone_from(
            url.strip(),
            str(repo_dir),
            depth=1,  # 浅克隆，节省时间和空间
            single_branch=True,
        )
        logger.info("克隆完成: %s", repo_dir)
        return repo_dir, ""
    except Exception as exc:
        logger.error("克隆仓库失败: %s", exc)
        return repo_dir, f"克隆仓库失败: {exc}"
