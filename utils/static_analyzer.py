"""
static_analyzer.py - 跨文件静态依赖分析模块

基于静态分析（Python ast / Java & Go 正则）提取文件间的导入依赖，
并解析被引用符号（函数、类）的源码定义，为 LLM 审计提供跨文件上下文补全。

设计原则：
  - 语言中立：通过 LanguageAnalyzer 抽象基类预留多语言扩展接口
  - 优雅降级：任何解析失败都不影响主流程，仅返回空结果
  - 轻量级：只提取直接依赖（一跳），不做全量调用图分析
  - 解耦：独立于审计逻辑，可单独测试和使用

扩展方式：
  若需支持新语言（如 Rust / PHP），继承 LanguageAnalyzer 并注册到 _ANALYZER_MAP 即可。
"""

from __future__ import annotations

import ast
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from services.project_profiler import Language

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
#  数据模型
# ══════════════════════════════════════════════════

@dataclass
class ImportReference:
    """
    表示源文件中的一条导入引用。

    Attributes:
        module_path: 模块路径，如 "services.project_profiler"
        symbols:     导入的符号名列表，如 ["ProjectProfile", "Language"]
        is_relative: 是否为相对导入（Python 专属）
    """
    module_path: str
    symbols: List[str] = field(default_factory=list)
    is_relative: bool = False


@dataclass
class CodeSnippet:
    """
    从依赖文件中提取的代码片段。

    Attributes:
        file_path:   相对于 repo_root 的文件路径
        symbol_name: 符号名（函数名 / 类名）
        content:     源码文本
        start_line:  起始行号（1-based）
        end_line:    结束行号（1-based）
    """
    file_path: str
    symbol_name: str
    content: str
    start_line: int = 0
    end_line: int = 0


@dataclass
class FileDependency:
    """
    单个文件的依赖分析结果。

    Attributes:
        source_file:       被分析的源文件（相对路径）
        imports:           提取到的导入引用列表
        resolved_snippets: 已解析的项目内部依赖代码片段
    """
    source_file: str
    imports: List[ImportReference] = field(default_factory=list)
    resolved_snippets: List[CodeSnippet] = field(default_factory=list)


# ══════════════════════════════════════════════════
#  抽象基类 —— 语言分析器接口
# ══════════════════════════════════════════════════

class LanguageAnalyzer(ABC):
    """
    语言分析器抽象基类。

    每种语言实现三个核心能力：
      1. 提取导入语句
      2. 判断模块是否为项目内部
      3. 从目标文件中提取指定符号的定义源码
    """

    @abstractmethod
    def extract_imports(
        self, file_path: Path, repo_root: Path,
    ) -> List[ImportReference]:
        """提取文件中的所有导入引用。"""
        ...

    @abstractmethod
    def resolve_module_to_file(
        self, module_path: str, repo_root: Path,
    ) -> Optional[Path]:
        """
        将模块路径解析为项目内的实际文件路径。
        如果该模块是外部依赖（标准库、第三方包），返回 None。
        """
        ...

    @abstractmethod
    def extract_symbol_definition(
        self, file_path: Path, symbol_name: str, max_chars: int = 3000,
    ) -> Optional[CodeSnippet]:
        """
        从文件中提取指定符号（函数 / 类）的定义源码。
        如果找不到，返回 None。
        """
        ...


# ══════════════════════════════════════════════════
#  Python 分析器 —— 基于 ast 模块
# ══════════════════════════════════════════════════

class PythonAnalyzer(LanguageAnalyzer):
    """
    Python 语言静态分析器。

    使用标准库 ast 进行精确解析，支持：
      - import xxx / from xxx import yyy
      - 相对导入
      - 函数定义（含 async）、类定义的源码提取
    """

    def extract_imports(
        self, file_path: Path, repo_root: Path,
    ) -> List[ImportReference]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_path))
        except (SyntaxError, Exception) as exc:
            logger.debug("Python AST 解析失败 %s: %s", file_path, exc)
            return []

        imports: List[ImportReference] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(ImportReference(
                        module_path=alias.name,
                        symbols=[alias.asname or alias.name.split(".")[-1]],
                    ))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    symbols = [
                        alias.name
                        for alias in (node.names or [])
                        if alias.name != "*"
                    ]
                    if symbols:
                        imports.append(ImportReference(
                            module_path=node.module,
                            symbols=symbols,
                            is_relative=node.level > 0,
                        ))

        return imports

    def resolve_module_to_file(
        self, module_path: str, repo_root: Path,
    ) -> Optional[Path]:
        parts = module_path.split(".")

        # 尝试作为模块文件：services.audit_engine → services/audit_engine.py
        candidate = repo_root / Path(*parts).with_suffix(".py")
        if candidate.exists():
            return candidate

        # 尝试作为包：services → services/__init__.py
        candidate = repo_root / Path(*parts) / "__init__.py"
        if candidate.exists():
            return candidate

        return None

    def extract_symbol_definition(
        self, file_path: Path, symbol_name: str, max_chars: int = 3000,
    ) -> Optional[CodeSnippet]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(file_path))
        except (SyntaxError, Exception) as exc:
            logger.debug("Python AST 解析失败 %s: %s", file_path, exc)
            return None

        lines = source.splitlines(keepends=True)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol_name:
                    start = node.lineno  # 1-based
                    end = (
                        node.end_lineno
                        if hasattr(node, "end_lineno") and node.end_lineno
                        else start
                    )
                    snippet_text = "".join(lines[start - 1 : end])
                    if len(snippet_text) > max_chars:
                        snippet_text = (
                            snippet_text[:max_chars]
                            + "\n# ... [定义过长，已截断] ..."
                        )
                    return CodeSnippet(
                        file_path="",  # 由调用方填充
                        symbol_name=symbol_name,
                        content=snippet_text,
                        start_line=start,
                        end_line=end,
                    )

        return None


# ══════════════════════════════════════════════════
#  Java 分析器 —— 基于正则（轻量级）
# ══════════════════════════════════════════════════

class JavaAnalyzer(LanguageAnalyzer):
    """
    Java 语言静态分析器（正则实现）。

    支持：
      - import 语句提取（含 static import）
      - 基于包路径的项目内文件定位（搜索 src/main/java 等常见源码目录）
      - 类 / 接口定义的粗粒度提取
    """

    _IMPORT_PATTERN = re.compile(
        r"^import\s+(?:static\s+)?([a-zA-Z_][\w.]*)\s*;", re.MULTILINE,
    )

    # Java 项目常见的源码根目录
    _JAVA_SRC_ROOTS = ["src/main/java", "src", ""]

    def extract_imports(
        self, file_path: Path, repo_root: Path,
    ) -> List[ImportReference]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("读取失败 %s: %s", file_path, exc)
            return []

        imports: List[ImportReference] = []
        for match in self._IMPORT_PATTERN.finditer(source):
            full_path = match.group(1)
            parts = full_path.rsplit(".", 1)
            if len(parts) == 2:
                module_path, symbol = parts
                imports.append(ImportReference(
                    module_path=module_path,
                    symbols=[symbol],
                ))

        return imports

    def resolve_module_to_file(
        self, module_path: str, repo_root: Path,
    ) -> Optional[Path]:
        rel_dir = module_path.replace(".", "/")

        for src_root in self._JAVA_SRC_ROOTS:
            base = repo_root / src_root if src_root else repo_root
            # 尝试找到同包下的 Java 文件
            candidate_dir = base / rel_dir
            parent_dir = candidate_dir.parent
            class_name = candidate_dir.name
            candidate_file = parent_dir / f"{class_name}.java"
            if candidate_file.exists():
                return candidate_file

            # 尝试完整路径
            candidate_file = base / (rel_dir + ".java")
            if candidate_file.exists():
                return candidate_file

        return None

    def extract_symbol_definition(
        self, file_path: Path, symbol_name: str, max_chars: int = 3000,
    ) -> Optional[CodeSnippet]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        # 匹配类 / 接口 / 枚举定义
        pattern = re.compile(
            rf"((?:public|private|protected|abstract|final)\s+"
            rf"(?:static\s+)?(?:class|interface|enum)\s+"
            rf"{re.escape(symbol_name)}"
            rf"\s*(?:<[^>]*>)?"
            rf"\s*(?:extends\s+[\w.<>,\s]+)?"
            rf"\s*(?:implements\s+[\w.<>,\s]+)?"
            rf"\s*\{{)",
            re.MULTILINE,
        )
        match = pattern.search(source)
        if match:
            start_pos = match.start()
            # 找到匹配的右花括号（简化：取 max_chars 长度）
            snippet_text = source[start_pos : start_pos + max_chars]
            start_line = source[:start_pos].count("\n") + 1
            return CodeSnippet(
                file_path="",
                symbol_name=symbol_name,
                content=snippet_text,
                start_line=start_line,
                end_line=start_line + snippet_text.count("\n"),
            )

        # 尝试匹配方法定义
        method_pattern = re.compile(
            rf"((?:public|private|protected)\s+"
            rf"(?:static\s+)?[\w<>\[\],\s]+\s+"
            rf"{re.escape(symbol_name)}"
            rf"\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{{)",
            re.MULTILINE,
        )
        match = method_pattern.search(source)
        if match:
            start_pos = match.start()
            snippet_text = source[start_pos : start_pos + max_chars]
            start_line = source[:start_pos].count("\n") + 1
            return CodeSnippet(
                file_path="",
                symbol_name=symbol_name,
                content=snippet_text,
                start_line=start_line,
                end_line=start_line + snippet_text.count("\n"),
            )

        return None


# ══════════════════════════════════════════════════
#  Go 分析器 —— 基于正则（轻量级）
# ══════════════════════════════════════════════════

class GoAnalyzer(LanguageAnalyzer):
    """
    Go 语言静态分析器（正则实现）。

    支持：
      - 单行和块导入语句
      - 基于 go.mod 的项目内模块判断
      - func / type struct 定义的提取
    """

    _IMPORT_SINGLE = re.compile(r'import\s+"([^"]+)"')
    _IMPORT_BLOCK = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
    _IMPORT_LINE = re.compile(r'"([^"]+)"')

    def extract_imports(
        self, file_path: Path, repo_root: Path,
    ) -> List[ImportReference]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        imports: List[ImportReference] = []

        # 单行 import
        for match in self._IMPORT_SINGLE.finditer(source):
            imports.append(ImportReference(
                module_path=match.group(1),
                symbols=[match.group(1).split("/")[-1]],
            ))

        # 块 import
        for block_match in self._IMPORT_BLOCK.finditer(source):
            block = block_match.group(1)
            for line_match in self._IMPORT_LINE.finditer(block):
                imports.append(ImportReference(
                    module_path=line_match.group(1),
                    symbols=[line_match.group(1).split("/")[-1]],
                ))

        return imports

    def resolve_module_to_file(
        self, module_path: str, repo_root: Path,
    ) -> Optional[Path]:
        go_mod = repo_root / "go.mod"
        if not go_mod.exists():
            return None

        try:
            mod_content = go_mod.read_text(encoding="utf-8", errors="replace")
            module_line = re.search(r"^module\s+(\S+)", mod_content, re.MULTILINE)
            if module_line:
                module_name = module_line.group(1)
                if module_path.startswith(module_name):
                    rel = module_path[len(module_name):].lstrip("/")
                    candidate = repo_root / rel
                    if candidate.is_dir():
                        # Go 包是目录，返回目录内的第一个 .go 文件
                        go_files = list(candidate.glob("*.go"))
                        if go_files:
                            return go_files[0]
        except Exception:
            pass

        return None

    def extract_symbol_definition(
        self, file_path: Path, symbol_name: str, max_chars: int = 3000,
    ) -> Optional[CodeSnippet]:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        # 匹配 func 定义（含方法接收器）
        func_pattern = re.compile(
            rf"^(func\s+(?:\([^)]*\)\s+)?{re.escape(symbol_name)}\s*\()",
            re.MULTILINE,
        )
        match = func_pattern.search(source)

        if not match:
            # 尝试匹配 type struct 定义
            struct_pattern = re.compile(
                rf"^(type\s+{re.escape(symbol_name)}\s+struct\s*\{{)",
                re.MULTILINE,
            )
            match = struct_pattern.search(source)

        if match:
            start_pos = match.start()
            snippet_text = source[start_pos : start_pos + max_chars]
            start_line = source[:start_pos].count("\n") + 1
            return CodeSnippet(
                file_path="",
                symbol_name=symbol_name,
                content=snippet_text,
                start_line=start_line,
                end_line=start_line + snippet_text.count("\n"),
            )

        return None


# ══════════════════════════════════════════════════
#  分析器工厂
# ══════════════════════════════════════════════════

_ANALYZER_MAP: Dict[Language, type] = {
    Language.PYTHON: PythonAnalyzer,
    Language.JAVA: JavaAnalyzer,
    Language.GO: GoAnalyzer,
    # 扩展：未来添加 Language.RUST: RustAnalyzer 等
}


def get_analyzer(language: Language) -> Optional[LanguageAnalyzer]:
    """
    根据语言枚举获取对应的分析器实例。

    Args:
        language: 项目主语言

    Returns:
        对应的 LanguageAnalyzer 实例，不支持的语言返回 None
    """
    cls = _ANALYZER_MAP.get(language)
    return cls() if cls else None


# ══════════════════════════════════════════════════
#  核心对外接口
# ══════════════════════════════════════════════════

def analyze_file_dependencies(
    file_path: Path,
    repo_root: Path,
    language: Language,
    max_snippet_chars: int = 3000,
) -> FileDependency:
    """
    分析单个文件的跨文件依赖，返回所有可解析的项目内关联代码片段。

    流程：
      1. 提取文件中的导入语句
      2. 过滤出项目内部模块（排除标准库和第三方包）
      3. 对每个内部导入，提取被引用符号的定义源码

    Args:
        file_path:         要分析的文件绝对路径
        repo_root:         项目仓库根路径
        language:          项目主语言
        max_snippet_chars: 每个代码片段的最大字符数

    Returns:
        FileDependency 实例，包含导入信息和已解析的代码片段
    """
    rel_path = file_path.relative_to(repo_root).as_posix()
    result = FileDependency(source_file=rel_path)

    analyzer = get_analyzer(language)
    if not analyzer:
        logger.debug("语言 %s 暂无静态分析器，跳过依赖分析", language.value)
        return result

    try:
        # Step 1: 提取导入
        imports = analyzer.extract_imports(file_path, repo_root)
        result.imports = imports

        # Step 2 & 3: 解析项目内部依赖
        seen_files: Set[str] = set()
        for imp in imports:
            resolved_path = analyzer.resolve_module_to_file(
                imp.module_path, repo_root,
            )
            if not resolved_path:
                continue  # 外部依赖，跳过

            dep_rel_path = resolved_path.relative_to(repo_root).as_posix()
            if dep_rel_path in seen_files:
                continue

            # 提取引用的每个符号定义
            for symbol in imp.symbols:
                snippet = analyzer.extract_symbol_definition(
                    resolved_path, symbol, max_snippet_chars,
                )
                if snippet:
                    snippet.file_path = dep_rel_path
                    result.resolved_snippets.append(snippet)

            seen_files.add(dep_rel_path)

        if result.resolved_snippets:
            logger.info(
                "文件 %s: 提取到 %d 个导入, 解析到 %d 个内部依赖片段",
                rel_path, len(imports), len(result.resolved_snippets),
            )

    except Exception as exc:
        logger.warning(
            "静态分析文件 %s 失败（降级为无依赖）: %s",
            rel_path, exc,
        )

    return result


def build_dependency_map(
    files: List[Path],
    repo_root: Path,
    language: Language,
) -> Dict[str, List[str]]:
    """
    批量构建文件间的依赖映射表。

    遍历文件列表，为每个文件提取其项目内部的导入依赖路径。
    此映射表可存入 ProjectProfile.dependency_map 供后续使用。

    Args:
        files:     文件绝对路径列表
        repo_root: 项目根路径
        language:  项目主语言

    Returns:
        {文件相对路径: [依赖文件相对路径列表]}
    """
    dep_map: Dict[str, List[str]] = {}

    analyzer = get_analyzer(language)
    if not analyzer:
        return dep_map

    for f in files:
        try:
            imports = analyzer.extract_imports(f, repo_root)
            deps: List[str] = []
            for imp in imports:
                resolved = analyzer.resolve_module_to_file(
                    imp.module_path, repo_root,
                )
                if resolved:
                    deps.append(resolved.relative_to(repo_root).as_posix())
            if deps:
                rel = f.relative_to(repo_root).as_posix()
                dep_map[rel] = sorted(set(deps))
        except Exception:
            continue

    logger.info(
        "依赖映射表构建完成: %d 个文件含有项目内依赖",
        len(dep_map),
    )
    return dep_map
