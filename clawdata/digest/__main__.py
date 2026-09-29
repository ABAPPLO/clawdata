"""视频文库 CLI：对订阅博主执行一轮信息整理，或对已有文库提问。

用法：
    python -m clawdata.digest                 # 全部启用订阅，每订阅默认 5 条新视频
    python -m clawdata.digest --sub 3         # 只整理订阅 3
    python -m clawdata.digest --limit 2       # 每订阅最多 2 条
    python -m clawdata.digest --ask "某某工具的下载链接是什么"
"""

from __future__ import annotations

import argparse

from clawdata.core.paths import DEFAULT_DB_PATH
from clawdata.digest import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m clawdata.digest",
        description="订阅博主最新视频信息整理（简介/字幕 -> 结构化 -> AI 总结 -> 文库）",
    )
    parser.add_argument("--sub", type=int, default=0, help="指定订阅 ID（默认全部启用订阅）")
    parser.add_argument("--limit", type=int, default=0, help="每个订阅最多处理的新视频条数（0=用配置默认 5）")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="数据库路径")
    parser.add_argument("--ask", default="", help="不整理，直接对文库提问")
    args = parser.parse_args(argv)

    if args.ask:
        result = service.ask(args.db, args.ask)
        print(result["answer"])
        for r in result.get("refs", []):
            print(f"[参考] #{r['id']} {r['title']}（{r['author']}）")
        return 0

    stats = service.run_digest(
        args.db,
        subscription_id=args.sub,
        limit=args.limit or None,
        on_progress=lambda m: print(m),
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
