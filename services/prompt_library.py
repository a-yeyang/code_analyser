"""
prompt_library.py - 动态提示词管理器

根据 ProjectProfile 自动组合「通用安全基座 + 语言专家 + 框架专家」三层提示词，
使 LLM 输出更具针对性的审计报告。

支持审计模式：
  - full:  详细模式（默认），输出完整的结构化审计报告
  - brief: 简要模式，仅输出位置 + 风险 + 最小修复方案

设计原则：
  - 所有 Prompt 均为纯文本常量，便于版本管理和快速迭代。
  - PromptLibrary 作为唯一对外接口，隐藏选择逻辑。
  - 每条专家 Prompt 末尾都强制要求输出 CWE 编号和修复方案。
  - brief 模式的格式指令放在 system_prompt 最末尾，利用 LLM
    "指令跟随"特性覆盖前面基座中的详细输出要求。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from services.project_profiler import Language, ProjectProfile

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
#  Prompt 常量区 —— 按「基座 / 语言专家 / 框架专家」分层
# ═══════════════════════════════════════════════

# ────────── 1. 通用安全基座 ──────────

BASE_SYSTEM_PROMPT = """\
你是一名资深代码安全审计专家，精通 OWASP Top 10、CWE 漏洞分类体系和多种编程语言的安全最佳实践。

## 通用审计规则
1. 逐文件分析代码，重点检查以下类别：
   - 注入类漏洞（SQL 注入、OS 命令注入、LDAP 注入等）
   - 跨站脚本（XSS）与跨站请求伪造（CSRF）
   - 硬编码凭证与密钥泄露
   - 不安全的反序列化
   - 路径遍历与任意文件读写
   - 不当的访问控制与权限校验
   - 敏感数据明文传输 / 存储
   - 安全配置缺陷（如 DEBUG 开关、CORS 放行 *）

2. 对每个发现，必须包含：
   - **CWE 编号**（如 CWE-89: SQL Injection）
   - **风险等级**：高 / 中 / 低
   - **漏洞类型**
   - **所在文件与代码位置**（行号或函数名）
   - **影响范围**：该漏洞在实际部署中可能导致的后果
   - **修复方案**：给出具体的、针对当前语言和框架的代码级修复建议

3. 报告末尾给出：
   - 整体安全评分（0-100）
   - 按风险等级汇总的漏洞统计表
   - 优先修复建议（Top 3）

4. 如果代码量过少或未发现明显漏洞，也要明确说明并给出加固建议。
5. 全程使用中文回复。
"""

# ────────── 2. 语言专家 Prompt ──────────

JAVA_EXPERT_PROMPT = """\

## Java 安全专家补充规则
除通用规则外，请额外重点审计以下 Java 特有的安全与稳定性风险：
1. **并发安全**：检查 Thread / Runnable / ExecutorService 使用，是否存在竞态条件、死锁；
   synchronized / ReentrantLock 的锁粒度是否合理。
2. **内存与 GC 风险**：大对象（如大数组/大集合）在堆内分配是否可能触发 Full GC；
   是否存在内存泄漏（如静态集合持续增长、未关闭的 Stream/Connection）。
3. **分布式幂等性**：对外暴露的 REST/RPC 接口是否做了幂等校验（如唯一请求 ID、数据库唯一约束）。
4. **反序列化**：是否使用了不安全的 ObjectInputStream，是否有 Fastjson/Jackson 的 autoType 风险。
5. **依赖安全**：关注常见漏洞组件（如 Log4j、Commons-Collections）。

对以上每个发现同样给出 CWE 编号、影响范围和具体修复方案。
"""

PYTHON_EXPERT_PROMPT = """\

## Python 安全专家补充规则
除通用规则外，请额外重点审计以下 Python 特有的安全风险：
1. **反序列化**：pickle / shelve / yaml.load（非 safe_load）是否接受不可信输入。
2. **代码注入**：eval() / exec() / compile() 是否拼接了用户输入。
3. **子进程调用**：subprocess / os.system / os.popen 是否存在命令注入，shell=True 是否必要。
4. **路径遍历**：open() / pathlib 操作中是否对用户传入路径做了规范化和白名单校验。
5. **Django/Flask 特有**：DEBUG=True 是否残留、SECRET_KEY 是否硬编码、CSRF 中间件是否启用、
   模板渲染是否使用 |safe 或 Markup() 导致 XSS。
6. **依赖安全**：requirements.txt / pyproject.toml 中是否锁定版本，是否有已知 CVE 组件。

对以上每个发现同样给出 CWE 编号、影响范围和具体修复方案。
"""

GO_EXPERT_PROMPT = """\

## Go 安全专家补充规则
除通用规则外，请额外重点审计以下 Go 特有的安全风险：
1. **Goroutine 泄露**：goroutine 是否有退出机制（context / done channel），是否存在阻塞泄露。
2. **竞态条件**：共享变量是否通过 sync.Mutex / sync.RWMutex / atomic 保护，是否应使用 channel 代替。
3. **错误处理**：error 返回值是否被忽略（_ = someFunc()），是否有 panic 被 recover 吞掉。
4. **SQL 操作**：database/sql 查询是否使用参数化占位符，是否存在字符串拼接 SQL。
5. **HTTP 安全**：net/http 中间件是否配置了超时（ReadTimeout / WriteTimeout），TLS 配置是否安全。
6. **CGo / unsafe**：是否使用了 unsafe.Pointer 或 CGo，是否存在内存安全隐患。

对以上每个发现同样给出 CWE 编号、影响范围和具体修复方案。
"""

JAVASCRIPT_EXPERT_PROMPT = """\

## JavaScript / TypeScript 安全专家补充规则
除通用规则外，请额外重点审计以下 JS/TS 特有的安全风险：
1. **原型链污染**：是否存在 Object.assign / 深拷贝函数对用户输入的不安全处理。
2. **XSS**：前端框架（React/Vue/Angular）中是否使用了 dangerouslySetInnerHTML / v-html / innerHTML。
3. **依赖安全**：package.json 中是否有已知漏洞的依赖，是否锁定版本。
4. **服务端（Node.js）**：Express/Koa 中间件顺序是否正确，是否缺少 helmet / rate-limit / CORS 配置。
5. **认证与会话**：JWT secret 是否硬编码，token 是否在 URL 中传递，cookie 是否设置 httpOnly/secure。
6. **eval / Function 构造器**：是否动态执行了不可信代码。

对以上每个发现同样给出 CWE 编号、影响范围和具体修复方案。
"""

# ────────── 3. 框架专家 Prompt ──────────

MYBATIS_EXPERT_PROMPT = """\

## MyBatis SQL 安全专家补充规则
本项目使用了 MyBatis 框架，请对 XML mapper 文件进行深度扫描：
1. **${} vs #{}**：逐条检查 SQL 语句中的参数引用方式：
   - `${}` 为字符串直接拼接，是 **SQL 注入高危点**，必须逐一标记。
   - `#{}` 为预编译参数，是安全写法。
   - 对每个 `${}` 给出是否可被用户控制的判断和替换为 `#{}` 的具体建议。
2. **动态 SQL 风险**：<if> / <choose> / <foreach> 中的条件拼接是否可能产生意外 SQL。
3. **长 SQL 性能**：超过 50 行的 SQL 语句标记为性能风险，建议拆分或优化索引。
4. **批量操作**：<foreach> 拼接的 IN 子句是否有数量上限保护，防止超长 SQL 导致数据库拒绝服务。

对以上每个发现给出 CWE 编号（如 CWE-89）、影响范围和修复方案。
"""

SPRING_EXPERT_PROMPT = """\

## Spring 框架安全专家补充规则
本项目使用了 Spring 框架，请额外关注：
1. **Spring Security 配置**：是否正确配置了认证与授权，是否存在 permitAll 过度放开的路径。
2. **CSRF 保护**：REST API 是否合理地禁用/启用了 CSRF。
3. **SpEL 注入**：@Value / @PreAuthorize 中是否存在动态拼接 SpEL 表达式的风险。
4. **Actuator 暴露**：/actuator/** 端点是否在生产环境中被保护。
5. **参数绑定**：@RequestParam / @RequestBody 是否对输入做了校验（@Valid / @Validated）。

对以上每个发现同样给出 CWE 编号、影响范围和具体修复方案。
"""

# ────────── 4. 审计模式格式指令 ──────────

# 合法的审计模式值
AUDIT_MODE_FULL = "full"
AUDIT_MODE_BRIEF = "brief"
_VALID_MODES = {AUDIT_MODE_FULL, AUDIT_MODE_BRIEF}

BRIEF_FORMAT_PROMPT = """\

## 简要审计指令（核心优先级）
本次审计采用简要模式。严禁输出任何背景介绍、原理说明、安全评分或汇总表。
仅针对每个发现的风险点，严格按以下格式输出：

---
**位置**：[目录名]/[文件名] (第 X 行 - 第 Y 行)
**风险**：[一句话描述漏洞类型及危害]
**修复**：[给出可直接替换的最小代码修改方案，使用 diff 或代码块格式]
---

如果没有发现风险，仅回复：'未发现明显安全风险。'
"""

# ────────── 5. 用户请求模板 ──────────

USER_PROMPT_TEMPLATE = """\
## 项目画像
{project_summary}

## 用户审计需求
{audit_prompt}

## 代码文件列表
共 {file_count} 个文件。

{code_blocks}
"""


# ═══════════════════════════════════════════════
#  PromptLibrary —— 对外统一接口
# ═══════════════════════════════════════════════

# 语言 → 专家 Prompt 映射
_LANGUAGE_EXPERT_MAP = {
    Language.JAVA: JAVA_EXPERT_PROMPT,
    Language.PYTHON: PYTHON_EXPERT_PROMPT,
    Language.GO: GO_EXPERT_PROMPT,
    Language.JAVASCRIPT: JAVASCRIPT_EXPERT_PROMPT,
    Language.TYPESCRIPT: JAVASCRIPT_EXPERT_PROMPT,  # TS 复用 JS 专家
}

# 框架关键字 → 专家 Prompt 映射
_FRAMEWORK_EXPERT_MAP = {
    "spring": SPRING_EXPERT_PROMPT,
    "spring-cloud": SPRING_EXPERT_PROMPT,
}


@dataclass
class ComposedPrompt:
    """
    组合后的提示词结果。

    Attributes:
        system_prompt: 拼接完成的 SYSTEM_PROMPT（基座 + 语言 + 框架）
        experts_used:  本次使用的专家标签列表，用于日志和报告
    """
    system_prompt: str
    experts_used: List[str]


class PromptLibrary:
    """
    动态提示词管理器。

    根据 ProjectProfile 自动选择并拼接多层专家 Prompt。
    组合策略：  BASE + 语言专家 + 框架专家(们) + MyBatis 专家(可选) + [模式格式]

    Usage:
        library = PromptLibrary()
        composed = library.compose(profile, mode="brief")
        # composed.system_prompt 即可直接用于 ChatPromptTemplate
    """

    def compose(
        self,
        profile: ProjectProfile,
        mode: str = AUDIT_MODE_FULL,
    ) -> ComposedPrompt:
        """
        根据项目画像和审计模式组合 SYSTEM_PROMPT。

        Args:
            profile: identify_project_context 返回的项目画像
            mode:    审计模式 ("full" | "brief")

        Returns:
            ComposedPrompt（包含完整 system_prompt 和使用的专家列表）
        """
        # 校验 mode 合法性，非法值回退到 full
        if mode not in _VALID_MODES:
            logger.warning("未知审计模式 '%s'，回退为 full", mode)
            mode = AUDIT_MODE_FULL

        parts: List[str] = [BASE_SYSTEM_PROMPT]
        experts: List[str] = ["通用安全基座"]

        # ── 语言专家 ──
        lang_prompt = _LANGUAGE_EXPERT_MAP.get(profile.primary_language)
        if lang_prompt:
            parts.append(lang_prompt)
            experts.append(f"{profile.primary_language.value} 语言专家")

        # ── 框架专家 ──
        for fw in profile.frameworks:
            fw_lower = fw.lower()
            fw_prompt = _FRAMEWORK_EXPERT_MAP.get(fw_lower)
            if fw_prompt and fw_prompt not in parts:
                parts.append(fw_prompt)
                experts.append(f"{fw} 框架专家")

        # ── MyBatis 专家（独立判断，优先级高于框架映射）──
        if profile.has_mybatis and MYBATIS_EXPERT_PROMPT not in parts:
            parts.append(MYBATIS_EXPERT_PROMPT)
            experts.append("MyBatis SQL 专家")

        # ── 审计模式格式指令（放在最末尾，优先级最高）──
        if mode == AUDIT_MODE_BRIEF:
            parts.append(BRIEF_FORMAT_PROMPT)
            experts.append("简要模式")

        system_prompt = "\n".join(parts)

        logger.info(
            "已激活审计专家: %s | 模式: %s",
            " → ".join(experts), mode,
        )
        return ComposedPrompt(
            system_prompt=system_prompt,
            experts_used=experts,
        )
