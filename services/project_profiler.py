"""
project_profiler.py - 项目特征识别模块

通过扫描仓库的文件结构、配置文件和文件后缀，自动识别：
  - 主要编程语言
  - 使用的框架（Spring / Flask / Gin / Express …）
  - 是否使用 MyBatis（检测 XML mapper 文件）
  - 项目规模等级
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  常量定义
# ──────────────────────────────────────────────

class Language(str, Enum):
    """支持识别的编程语言枚举。"""
    JAVA = "java"
    PYTHON = "python"
    GO = "go"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    C = "c"
    CPP = "cpp"
    RUST = "rust"
    RUBY = "ruby"
    PHP = "php"
    SOLIDITY = "solidity"
    UNKNOWN = "unknown"


# 后缀 → 语言映射
_EXTENSION_LANGUAGE_MAP: Dict[str, Language] = {
    ".java": Language.JAVA,
    ".py": Language.PYTHON,
    ".go": Language.GO,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".c": Language.C,
    ".h": Language.C,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".cxx": Language.CPP,
    ".hpp": Language.CPP,
    ".rs": Language.RUST,
    ".rb": Language.RUBY,
    ".php": Language.PHP,
    ".sol": Language.SOLIDITY,
}

# 配置文件 → 框架 / 工具链映射
_CONFIG_FRAMEWORK_MAP: Dict[str, str] = {
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "go.mod": "go-module",
    "requirements.txt": "pip",
    "pyproject.toml": "python-project",
    "setup.py": "setuptools",
    "Pipfile": "pipenv",
    "package.json": "npm",
    "yarn.lock": "yarn",
    "Cargo.toml": "cargo",
    "Gemfile": "bundler",
    "composer.json": "composer",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
}

# 特征文件名 → 框架标签（更精细的框架识别）
_FRAMEWORK_INDICATORS: Dict[str, str] = {
    "application.yml": "spring",
    "application.yaml": "spring",
    "application.properties": "spring",
    "bootstrap.yml": "spring-cloud",
    "bootstrap.yaml": "spring-cloud",
    "manage.py": "django",
    "settings.py": "django",
    "wsgi.py": "django",
    "app.py": "flask-or-fastapi",
    "main.go": "go",
    "next.config.js": "nextjs",
    "nuxt.config.js": "nuxtjs",
    "nuxt.config.ts": "nuxtjs",
    "angular.json": "angular",
    "vue.config.js": "vue",
    "vite.config.ts": "vite",
}

# MyBatis mapper 标签匹配
_MYBATIS_MAPPER_PATTERN = re.compile(
    r"<mapper\s+namespace\s*=", re.IGNORECASE
)

# 跳过的目录（与 file_manager 保持一致）
_SKIP_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "vendor", "dist", "build", ".next", ".nuxt", "target",
    ".idea", ".vscode", "eggs",
}


# ──────────────────────────────────────────────
#  数据模型
# ──────────────────────────────────────────────

class ProjectScale(str, Enum):
    """项目规模等级，用于决定审计策略。"""
    SMALL = "small"      # < 30 文件
    MEDIUM = "medium"    # 30 ~ 150 文件
    LARGE = "large"      # > 150 文件


@dataclass
class ProjectProfile:
    """
    项目画像数据类，承载识别结果。

    Attributes:
        primary_language:   主要编程语言
        language_stats:     各语言文件计数
        frameworks:         检测到的框架 / 工具链列表
        has_mybatis:        是否包含 MyBatis mapper
        mybatis_files:      MyBatis XML mapper 文件路径列表
        config_files:       检测到的配置文件名列表
        total_code_files:   代码文件总数
        scale:              项目规模等级
        dependency_map:     文件间依赖映射 {文件路径: [依赖文件路径列表]}
                            由静态分析模块在审计流程中填充
    """
    primary_language: Language = Language.UNKNOWN
    language_stats: Dict[str, int] = field(default_factory=dict)
    frameworks: List[str] = field(default_factory=list)
    has_mybatis: bool = False
    mybatis_files: List[str] = field(default_factory=list)
    config_files: List[str] = field(default_factory=list)
    total_code_files: int = 0
    scale: ProjectScale = ProjectScale.SMALL
    dependency_map: Dict[str, List[str]] = field(default_factory=dict)

    def summary(self) -> str:
        """返回人类可读的项目画像摘要（用于日志和 Prompt 拼接）。"""
        langs = ", ".join(
            f"{lang}({count})" for lang, count in
            sorted(self.language_stats.items(), key=lambda x: -x[1])
        )
        parts = [
            f"主语言: {self.primary_language.value}",
            f"语言分布: [{langs}]",
            f"框架: {self.frameworks or '未识别'}",
            f"MyBatis: {'是' if self.has_mybatis else '否'}",
            f"代码文件数: {self.total_code_files}",
            f"项目规模: {self.scale.value}",
        ]
        return " | ".join(parts)


# ──────────────────────────────────────────────
#  核心识别函数
# ──────────────────────────────────────────────

def _detect_mybatis(file_path: Path) -> bool:
    """检测单个 XML 文件是否为 MyBatis mapper。"""
    if file_path.suffix.lower() != ".xml":
        return False
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return bool(_MYBATIS_MAPPER_PATTERN.search(content))
    except Exception:
        return False


def _determine_scale(file_count: int) -> ProjectScale:
    """根据文件数判定项目规模。"""
    if file_count < 30:
        return ProjectScale.SMALL
    if file_count <= 150:
        return ProjectScale.MEDIUM
    return ProjectScale.LARGE


def identify_project_context(repo_path: Path) -> ProjectProfile:
    """
    扫描仓库目录，构建项目画像。

    核心逻辑：
      1. 遍历所有文件，统计各语言文件数量。
      2. 检测配置文件，推断框架。
      3. 针对 XML 文件检测 MyBatis mapper。
      4. 确定主语言和项目规模。

    Args:
        repo_path: 仓库本地根路径

    Returns:
        ProjectProfile 数据实例
    """
    language_counts: Dict[str, int] = {}
    frameworks: List[str] = []
    config_files: List[str] = []
    mybatis_files: List[str] = []
    seen_frameworks: Set[str] = set()
    total_code = 0

    for item in repo_path.rglob("*"):
        if item.is_dir():
            continue

        # 跳过黑名单目录
        rel_parts = item.relative_to(repo_path).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue

        file_name = item.name.lower()
        suffix = item.suffix.lower()

        # ── 1. 语言统计 ──
        lang = _EXTENSION_LANGUAGE_MAP.get(suffix)
        if lang is not None:
            language_counts[lang.value] = language_counts.get(lang.value, 0) + 1
            total_code += 1

        # ── 2. 配置文件 / 框架识别 ──
        if item.name in _CONFIG_FRAMEWORK_MAP:
            fw = _CONFIG_FRAMEWORK_MAP[item.name]
            config_files.append(item.name)
            if fw not in seen_frameworks:
                frameworks.append(fw)
                seen_frameworks.add(fw)

        if item.name in _FRAMEWORK_INDICATORS:
            fw = _FRAMEWORK_INDICATORS[item.name]
            if fw not in seen_frameworks:
                frameworks.append(fw)
                seen_frameworks.add(fw)

        # ── 3. MyBatis 检测 ──
        if suffix == ".xml" and _detect_mybatis(item):
            rel_posix = item.relative_to(repo_path).as_posix()
            mybatis_files.append(rel_posix)

    # ── 4. 确定主语言 ──
    primary = Language.UNKNOWN
    if language_counts:
        top_lang = max(language_counts, key=language_counts.get)  # type: ignore[arg-type]
        try:
            primary = Language(top_lang)
        except ValueError:
            primary = Language.UNKNOWN

    profile = ProjectProfile(
        primary_language=primary,
        language_stats=language_counts,
        frameworks=frameworks,
        has_mybatis=len(mybatis_files) > 0,
        mybatis_files=mybatis_files,
        config_files=config_files,
        total_code_files=total_code,
        scale=_determine_scale(total_code),
    )

    logger.info("项目画像: %s", profile.summary())
    return profile
