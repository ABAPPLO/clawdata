"""
抖音 Web 签名（a_bogus）封装，基于 F2/Johnserf-Seed 的纯 Python 实现（已 vendor）。

用法：
    from clawdata.signing.sign import sign_query
    param_str = "device_platform=webapp&aid=6383&...&keyword=..."
    signed = sign_query(param_str, user_agent)   # 追加 &a_bogus=...
"""

from __future__ import annotations

import urllib.parse

from clawdata.signing.abogus_impl import ABogus, BrowserFingerprintGenerator


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


def make_abogus(param_str: str, user_agent: str = DEFAULT_UA, request: str = "") -> str:
    """给一段 query string 生成 a_bogus，返回追加 a_bogus 后的完整 query。"""
    fp = BrowserFingerprintGenerator.generate_fingerprint("Edge")
    result = ABogus(fp=fp, user_agent=user_agent).generate_abogus(param_str, request)
    # result = (params_and_abogus, abogus_value, ua)
    return result[0]


def sign_url(base: str, params: dict[str, object], user_agent: str = DEFAULT_UA) -> str:
    """把 params 生成 query、签名 a_bogus，拼成完整 URL。"""
    param_str = "&".join(
        [f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()]
    )
    param_str = make_abogus(param_str, user_agent)
    return base + "?" + param_str
