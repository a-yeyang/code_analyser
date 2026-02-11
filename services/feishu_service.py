"""
feishu_service.py - 飞书（Feishu/Lark）知识库集成模块

核心设计思路：
  用户只需提供知识库的 URL（如 https://xxx.feishu.cn/wiki/XxxYyyZzz），
  代码自动从 URL 中提取 node_token，通过 get_node API 反查 space_id，
  无需用户手动寻找 space_id。

职责：
  - 从飞书知识库 URL 中解析 node_token
  - 通过 get_node API 获取 space_id 和节点信息
  - 遍历知识库节点，按标题关键词检索目标文档
  - 获取文档纯文本内容
  - 按审计语言做文本切片过滤

飞书 API 参考：
  - 认证:       POST /open-apis/auth/v3/tenant_access_token/internal
  - 节点信息:   GET  /open-apis/wiki/v2/spaces/get_node?token={node_token}&obj_type=wiki
  - 节点列表:   GET  /open-apis/wiki/v2/spaces/{space_id}/nodes
  - 文档纯文本: GET  /open-apis/docx/v1/documents/{document_id}/raw_content

依赖权限（在飞书开发者后台为应用开通）：
  - wiki:wiki:readonly
  - docx:document:readonly
  - drive:drive:readonly
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────

FEISHU_HOST = "https://open.feishu.cn"

_TOKEN_URL = f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal"

# 通过 node_token 获取节点信息（含 space_id）
_GET_NODE_URL = f"{FEISHU_HOST}/open-apis/wiki/v2/spaces/get_node"

# 知识库节点列表（分页）
_WIKI_NODES_URL = f"{FEISHU_HOST}/open-apis/wiki/v2/spaces/{{space_id}}/nodes"

# 文档纯文本内容
_DOC_RAW_CONTENT_URL = (
    f"{FEISHU_HOST}/open-apis/docx/v1/documents/{{document_id}}/raw_content"
)

# 从飞书 URL 中提取 node_token 的正则
# 匹配格式: https://xxx.feishu.cn/wiki/{node_token}
#            https://xxx.feishu.cn/wiki/{node_token}?xxx
_WIKI_URL_PATTERN = re.compile(
    r"feishu\.cn/wiki/([A-Za-z0-9_-]+)"
)

# 语言关键词映射，用于从规范文档中切片出相关段落
_LANGUAGE_SECTION_KEYWORDS: Dict[str, List[str]] = {
    "java": ["java", "spring", "mybatis", "maven", "gradle", "jdk"],
    "python": ["python", "django", "flask", "fastapi", "pip", "pytest"],
    "go": ["go", "golang", "gin", "goroutine", "gomod"],
    "javascript": ["javascript", "js", "node", "npm", "react", "vue", "express"],
    "typescript": ["typescript", "ts", "node", "npm", "react", "vue", "express"],
}


# ──────────────────────────────────────────────
#  数据模型
# ──────────────────────────────────────────────

@dataclass
class WikiNode:
    """知识库节点信息。"""
    node_token: str
    obj_token: str          # 对应的文档 document_id
    obj_type: str           # "doc" / "docx" / "sheet" / ...
    title: str
    space_id: str = ""
    has_child: bool = False


@dataclass
class FeishuStandard:
    """
    从飞书检索到的企业规范。

    Attributes:
        title:         文档标题
        full_text:     文档完整纯文本
        filtered_text: 按语言过滤后的片段（用于注入 Prompt）
        source:        来源说明（用于报告引用）
    """
    title: str = ""
    full_text: str = ""
    filtered_text: str = ""
    source: str = ""


# ──────────────────────────────────────────────
#  URL 解析工具
# ──────────────────────────────────────────────

def parse_node_token_from_url(wiki_url: str) -> str:
    """
    从飞书知识库 URL 中提取 node_token。

    支持的格式：
      - https://xxx.feishu.cn/wiki/CxUNwmXP7i9rflkSvBuctY3fnVd
      - https://xxx.feishu.cn/wiki/CxUNwmXP7i9rflkSvBuctY3fnVd?query=xxx
      - 直接传入 node_token 字符串（无 URL 前缀时原样返回）

    Args:
        wiki_url: 飞书知识库链接或 node_token

    Returns:
        node_token 字符串，解析失败返回空字符串
    """
    wiki_url = wiki_url.strip()
    if not wiki_url:
        return ""

    match = _WIKI_URL_PATTERN.search(wiki_url)
    if match:
        return match.group(1)

    # 如果不是 URL 格式，可能用户直接填了 node_token 或 space_id
    # 原样返回让后续 API 尝试
    if "/" not in wiki_url and len(wiki_url) > 5:
        return wiki_url

    return ""


# ──────────────────────────────────────────────
#  飞书 API 客户端
# ──────────────────────────────────────────────

class FeishuClient:
    """
    飞书开放平台 API 客户端。

    核心改进：用户只需提供知识库 URL，代码自动解析 node_token
    并通过 get_node API 反查 space_id，无需手动填写 space_id。

    Usage:
        client = FeishuClient(app_id="...", app_secret="...")
        standard = client.fetch_standard_from_url(
            wiki_url="https://xxx.feishu.cn/wiki/CxUNwmXP7i...",
            doc_title_keyword="代码规范",
            language="java",
        )
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self._token: str = ""
        self._token_expire: float = 0.0

    # ────────── Token 管理 ──────────

    def _ensure_token(self) -> None:
        """确保 tenant_access_token 有效，过期前 60 秒自动刷新。"""
        if self._token and time.time() < self._token_expire - 60:
            return
        self._refresh_token()

    def _refresh_token(self) -> None:
        """调用飞书接口获取 tenant_access_token。"""
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        try:
            resp = httpx.post(_TOKEN_URL, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(
                    f"获取 token 失败: code={data.get('code')}, "
                    f"msg={data.get('msg')}"
                )
            self._token = data["tenant_access_token"]
            self._token_expire = time.time() + data.get("expire", 7200)
            logger.info("飞书 tenant_access_token 刷新成功")
        except Exception as exc:
            logger.error("飞书 Token 获取失败: %s", exc)
            raise

    def _headers(self) -> Dict[str, str]:
        """构建带鉴权的请求头。"""
        self._ensure_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ────────── 节点信息（从 node_token 反查 space_id）──────────

    def get_node_info(self, node_token: str) -> Optional[WikiNode]:
        """
        通过 node_token 获取节点详细信息（含 space_id）。

        这是解决"用户找不到 space_id"问题的核心接口：
        用户提供知识库 URL → 解析出 node_token → 调本接口 → 拿到 space_id。

        API: GET /open-apis/wiki/v2/spaces/get_node?token={node_token}&obj_type=wiki

        Args:
            node_token: 从知识库 URL 中解析出的节点 token

        Returns:
            WikiNode（含 space_id），失败返回 None
        """
        # 依次尝试 obj_type = "wiki" 和 "docx"
        # 知识库根节点通常为 wiki，文档节点可能为 docx
        for obj_type in ("wiki", "docx", "doc"):
            try:
                resp = httpx.get(
                    _GET_NODE_URL,
                    headers=self._headers(),
                    params={"token": node_token, "obj_type": obj_type},
                    timeout=15,
                )

                # 尝试解析响应体
                try:
                    body = resp.json()
                except Exception:
                    body = {}

                if resp.status_code != 200:
                    logger.warning(
                        "get_node(obj_type=%s) HTTP %s: %s",
                        obj_type, resp.status_code,
                        body.get("msg", resp.text[:200] if resp.text else ""),
                    )
                    continue  # 换一个 obj_type 再试

                if body.get("code") != 0:
                    code = body.get("code")
                    msg = body.get("msg", "")
                    logger.warning(
                        "get_node(obj_type=%s) 业务错误: code=%s, msg=%s",
                        obj_type, code, msg,
                    )
                    # 某些错误码不需要再换 obj_type 重试
                    if code in (131001, 99991671, 99991672):
                        logger.error(
                            "→ 权限不足! 请在飞书开发者后台(open.feishu.cn/app)为应用"
                            "开通 wiki:wiki:readonly 权限，并在知识库设置中将应用添加为成员"
                        )
                        return None
                    continue

                # 解析成功
                node_data = body.get("data", {}).get("node", {})
                node = WikiNode(
                    node_token=node_data.get("node_token", ""),
                    obj_token=node_data.get("obj_token", ""),
                    obj_type=node_data.get("obj_type", ""),
                    title=node_data.get("title", ""),
                    space_id=node_data.get("space_id", ""),
                    has_child=node_data.get("has_child", False),
                )
                logger.info(
                    "get_node 成功(obj_type=%s): title='%s', space_id=%s, obj_token=%s",
                    obj_type, node.title, node.space_id, node.obj_token,
                )
                return node
            except Exception as exc:
                logger.warning("get_node(obj_type=%s) 异常: %s", obj_type, exc)
                continue

        # 所有 obj_type 都失败
        logger.error(
            "get_node 全部尝试失败 (token=%s)。"
            "常见原因: 1) 应用未开通 wiki:wiki:readonly 权限; "
            "2) 应用未发布; 3) 知识库未将应用添加为成员",
            node_token,
        )
        return None

    # ────────── 知识库节点遍历 ──────────

    def list_wiki_nodes(
        self,
        space_id: str,
        parent_node_token: Optional[str] = None,
    ) -> List[WikiNode]:
        """
        获取指定知识库下的节点列表（支持分页）。

        Args:
            space_id:           知识库 space_id
            parent_node_token:  父节点 token（为空则获取根节点）

        Returns:
            WikiNode 列表
        """
        url = _WIKI_NODES_URL.format(space_id=space_id)
        params: Dict[str, str] = {"page_size": "50"}
        if parent_node_token:
            params["parent_node_token"] = parent_node_token

        nodes: List[WikiNode] = []
        page_token: Optional[str] = None

        while True:
            if page_token:
                params["page_token"] = page_token
            try:
                resp = httpx.get(
                    url, headers=self._headers(), params=params, timeout=15
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("code") != 0:
                    logger.error("获取知识库节点失败: %s", body.get("msg"))
                    break

                items = body.get("data", {}).get("items", [])
                for item in items:
                    nodes.append(WikiNode(
                        node_token=item.get("node_token", ""),
                        obj_token=item.get("obj_token", ""),
                        obj_type=item.get("obj_type", ""),
                        title=item.get("title", ""),
                        space_id=space_id,
                        has_child=item.get("has_child", False),
                    ))

                if body.get("data", {}).get("has_more"):
                    page_token = body["data"].get("page_token")
                else:
                    break
            except Exception as exc:
                logger.error("请求知识库节点异常: %s", exc)
                break

        logger.info("知识库 %s 共获取 %d 个节点", space_id, len(nodes))
        return nodes

    def find_node_by_title(
        self,
        space_id: str,
        keyword: str,
    ) -> Optional[WikiNode]:
        """
        在知识库中按标题关键词模糊匹配文档（根节点 + 一层子目录）。

        Args:
            space_id: 知识库 ID
            keyword:  标题关键词（如 "代码规范"）

        Returns:
            匹配到的第一个 WikiNode，未找到返回 None
        """
        nodes = self.list_wiki_nodes(space_id)
        keyword_lower = keyword.lower()

        for node in nodes:
            if keyword_lower in node.title.lower():
                logger.info("匹配到文档: '%s' (obj_token=%s)", node.title, node.obj_token)
                return node

        # 递归搜索有子节点的目录（限一层深度）
        for node in nodes:
            if node.has_child:
                children = self.list_wiki_nodes(space_id, node.node_token)
                for child in children:
                    if keyword_lower in child.title.lower():
                        logger.info(
                            "匹配到文档（子目录）: '%s' (obj_token=%s)",
                            child.title, child.obj_token,
                        )
                        return child

        logger.warning("未在知识库 %s 中找到含 '%s' 的文档", space_id, keyword)
        return None

    # ────────── 文档内容 ──────────

    def get_document_raw_content(self, document_id: str) -> str:
        """
        获取文档的纯文本内容。

        Args:
            document_id: 文档 ID（即 WikiNode.obj_token）

        Returns:
            文档纯文本字符串
        """
        url = _DOC_RAW_CONTENT_URL.format(document_id=document_id)
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != 0:
                logger.error("获取文档内容失败: %s", body.get("msg"))
                return ""
            content = body.get("data", {}).get("content", "")
            logger.info(
                "获取文档内容成功, document_id=%s, 长度=%d",
                document_id, len(content),
            )
            return content
        except Exception as exc:
            logger.error("获取文档内容异常: %s", exc)
            return ""

    # ────────── 高级接口：一站式获取规范 ──────────

    def fetch_standard_from_url(
        self,
        wiki_url: str,
        doc_title_keyword: str = "代码规范",
        language: str = "",
        max_chars: int = 8000,
    ) -> FeishuStandard:
        """
        一站式接口：从飞书知识库 URL 出发，检索规范文档并提取内容。

        完整流程：
          1. 从 wiki_url 解析出 node_token
          2. 调用 get_node API 获取 space_id
          3. 在知识库中按 doc_title_keyword 搜索文档
          4. 获取文档纯文本
          5. 按语言过滤相关段落
          6. 截取到 max_chars 以内

        Args:
            wiki_url:          飞书知识库 URL（如 https://xxx.feishu.cn/wiki/XxxYyy）
            doc_title_keyword: 文档标题关键词
            language:          项目主语言（用于过滤）
            max_chars:         最大返回字符数

        Returns:
            FeishuStandard 数据实例
        """
        result = FeishuStandard()

        # 1. 解析 node_token
        node_token = parse_node_token_from_url(wiki_url)
        if not node_token:
            logger.error("无法从 URL 中解析 node_token: %s", wiki_url)
            return result

        logger.info("从 URL 解析出 node_token: %s", node_token)

        # 2. 通过 get_node 获取 space_id 和节点信息
        entry_node = self.get_node_info(node_token)

        full_text = ""

        if entry_node and entry_node.space_id:
            # ── 策略 A：标准流程（get_node 成功）──
            space_id = entry_node.space_id
            logger.info("获取到 space_id: %s", space_id)

            # 3A. 搜索目标文档
            target_node: Optional[WikiNode] = None
            if doc_title_keyword.lower() in entry_node.title.lower():
                target_node = entry_node
                logger.info("入口节点本身就是目标文档: '%s'", entry_node.title)
            else:
                target_node = self.find_node_by_title(space_id, doc_title_keyword)

            if target_node:
                full_text = self.get_document_raw_content(target_node.obj_token)
                if full_text:
                    result.title = target_node.title
                    result.source = f"飞书知识库文档《{target_node.title}》"
            else:
                logger.warning("未在知识库中找到含 '%s' 的文档", doc_title_keyword)

        if not full_text:
            # ── 策略 B：直接用 node_token 尝试读取文档（fallback）──
            # get_node 失败时的兜底方案：
            # 尝试把 node_token 当作 document_id 直接调用 docx API
            logger.info(
                "尝试 fallback 策略: 直接用 node_token=%s 作为 document_id 读取",
                node_token,
            )
            full_text = self.get_document_raw_content(node_token)

            if not full_text and entry_node and entry_node.obj_token:
                # obj_token 和 node_token 不同时，也试试 obj_token
                logger.info(
                    "再尝试 obj_token=%s 作为 document_id",
                    entry_node.obj_token,
                )
                full_text = self.get_document_raw_content(entry_node.obj_token)

            if full_text:
                result.title = doc_title_keyword
                result.source = f"飞书文档（直接获取, token={node_token}）"
                logger.info("fallback 策略成功，获取到文档内容 %d 字", len(full_text))
            else:
                logger.error(
                    "所有策略均失败，无法获取飞书文档内容。请确认:\n"
                    "  1. 已在 open.feishu.cn/app 为应用开通 wiki:wiki:readonly 和 docx:document:readonly 权限\n"
                    "  2. 已在开发者后台发布应用（版本管理与发布）\n"
                    "  3. 已在飞书知识库设置中将应用添加为成员（可阅读权限）"
                )
                return result

        result.full_text = full_text

        # 5. 按语言过滤
        filtered = _filter_by_language(full_text, language)
        if filtered:
            result.filtered_text = filtered[:max_chars]
        else:
            result.filtered_text = full_text[:max_chars]

        if len(result.filtered_text) >= max_chars:
            result.filtered_text += "\n\n... [规范文档过长，已截断] ..."

        logger.info(
            "飞书规范加载完成: '%s', 全文 %d 字, 过滤后 %d 字",
            result.title, len(full_text), len(result.filtered_text),
        )
        return result


# ──────────────────────────────────────────────
#  文本过滤工具
# ──────────────────────────────────────────────

def _filter_by_language(text: str, language: str) -> str:
    """
    从规范文档中提取与指定语言相关的段落。

    策略：按空行分段，保留包含语言关键词的段落。
    如果没有任何段落匹配，返回空字符串（调用方会 fallback 到全文）。
    """
    if not language:
        return ""

    keywords = _LANGUAGE_SECTION_KEYWORDS.get(language.lower(), [])
    if not keywords:
        return ""

    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    paragraphs = re.split(r"\n\s*\n", text)
    matched: List[str] = []

    for para in paragraphs:
        if pattern.search(para):
            matched.append(para.strip())

    return "\n\n".join(matched)
