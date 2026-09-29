"""从视频简介/字幕文本中提取结构化信息：链接分类、网盘提取码。

纯正则实现，不依赖网络与 AI。规则表内置常见平台，可通过
config/digest.json 的 link_rules 追加自定义规则，例如：
    [{"type": "公司官网", "pattern": "example.com"}]
pattern 是域名/URL 子串，忽略大小写。
"""

from __future__ import annotations

import re
from typing import Any


# (类型, 域名子串列表)——按顺序匹配，先命中先归类
BUILTIN_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("GitHub", ("github.com",)),
    ("Gitee", ("gitee.com",)),
    ("百度网盘", ("pan.baidu.com",)),
    ("夸克网盘", ("pan.quark.cn", "quark.cn")),
    ("阿里云盘", ("aliyundrive", "alipan.com")),
    ("天翼云盘", ("cloud.189.cn",)),
    ("移动云盘", ("caiyun.139.com",)),
    ("蓝奏云", ("lanzou",)),
    ("123云盘", ("123pan.com",)),
    ("飞书文档", ("feishu.cn", "larksuite.com")),
    ("Notion", ("notion.site", "notion.so")),
    ("语雀", ("yuque.com",)),
    ("腾讯文档", ("docs.qq.com",)),
    ("石墨文档", ("shimo.im",)),
    ("Google Drive", ("drive.google", "docs.google")),
    ("OneDrive", ("1drv.ms", "onedrive.live", "sharepoint.com")),
    ("B站专栏", ("bilibili.com/read",)),
    ("CSDN", ("csdn.net",)),
    ("知乎", ("zhihu.com",)),
]

_URL_RE = re.compile(
    r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"[提密]?取?码[:：\s=]*(?:pwd=)?([A-Za-z0-9]{4,6})")

_TRAILING_JUNK = "。，,；;）)】」》!！?？\"'“”"


def _clean_url(raw: str) -> str:
    url = raw.rstrip(_TRAILING_JUNK)
    # 去掉中文标点及之后被吞进来的尾巴
    for sep in "），。；！？":
        if sep in url:
            url = url.split(sep)[0]
    return url


def classify(url: str, custom_rules: list[dict[str, Any]] | None = None) -> str:
    low = url.lower()
    for rule in custom_rules or []:
        pattern = str(rule.get("pattern", "")).lower()
        if pattern and pattern in low:
            return str(rule.get("type", "自定义"))
    for type_name, keys in BUILTIN_RULES:
        if any(k in low for k in keys):
            return type_name
    return "其他链接"


def extract_links(
    text: str, custom_rules: list[dict[str, Any]] | None = None
) -> list[dict[str, str]]:
    """提取文本中的所有链接并分类，附带就近的提取码说明。"""
    text = text or ""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text):
        url = _clean_url(m.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        # 提取码一般写在链接后的 0~40 个字符内
        tail = text[m.end(): m.end() + 40]
        code = ""
        cm = _CODE_RE.search(tail)
        if cm and ("提取" in tail or "pwd" in tail.lower()):
            code = cm.group(1)
        item = {
            "type": classify(url, custom_rules),
            "value": url,
            "note": f"提取码 {code}" if code else "",
        }
        out.append(item)
    return out


if __name__ == "__main__":
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else (
        "项目地址 https://github.com/demo/repo 已经开源，文档在飞书 "
        "https://xx.feishu.cn/docs/a 提取码：ab12，资料包 "
        "https://pan.baidu.com/s/xxxx 提取码 6688"
    )
    for link in extract_links(sample):
        print(link)
