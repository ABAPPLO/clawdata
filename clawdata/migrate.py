"""迁移工具：在目标机器上从源面板逐个拉取资产——一个文件就是一个资产。

在「源机器」（现有 Windows 面板）不用装任何东西，只要面板对目标机可达
（源机器用 `start_dashboard.bat` 或 `--host 0.0.0.0` 启动即可）。
在「目标机器」（部署机）执行：

    python -m clawdata.migrate pull --from http://192.168.1.10:8000

按资产逐个拉取：每个资产先经 /api/migrate/export 打成一个 zip
（视频 + 元数据），下载完成后再导入本地库；已存在的自动跳过，
因此中断后直接重跑同一命令即可续传。其他用法：

    python -m clawdata.migrate pull --from http://src:8000 --type digests --limit 10
    python -m clawdata.migrate pull --from http://src:8000 --dry-run
    python -m clawdata.migrate export --type downloads --id 391 --out download_391.zip
    python -m clawdata.migrate import-file download_391.zip
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request

from clawdata.core.paths import DEFAULT_DB_PATH
from clawdata.storage import migrate

PAGE = 50


def _http_json(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: str, timeout: float = 120.0) -> int:
    size = 0
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return size


def _pull(source: str, types: list[str], limit: int, dry_run: bool, db_path: str) -> int:
    base = source.rstrip("/")
    stats = {t: {"imported": 0, "skipped": 0, "failed": 0} for t in types}
    for t in types:
        print(f"=== 拉取 {t}（源 {base}）===")
        try:
            known = migrate.existing_ids(db_path, t)
        except Exception as exc:  # noqa: BLE001
            print(f"读取本地库失败：{exc}")
            return 1
        after_id, done = 0, 0
        while True:
            if limit and done >= limit:
                break
            try:
                data = _http_json(
                    f"{base}/api/migrate/list?type={t}&after_id={after_id}&limit={PAGE}")
            except Exception as exc:  # noqa: BLE001
                print(f"拉取列表失败：{exc}")
                return 1
            items = data.get("items") or []
            if not items:
                break
            for it in items:
                rid, key = it["id"], it.get("key") or ""
                after_id = rid
                title = (it.get("title") or "")[:36]
                if key in known:
                    stats[t]["skipped"] += 1
                    print(f"  [跳过] id={rid} {title}（本地已存在）")
                    continue
                if dry_run:
                    done += 1
                    size_h = f"{int(it.get('size') or 0) / 1e6:.1f}MB" if it.get("size") else "-"
                    print(f"  [试跑] id={rid} {title}（{size_h}，含文件: {bool(it.get('has_file'))}）")
                    continue
                tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                tmp.close()
                try:
                    _download(f"{base}/api/migrate/export?type={t}&id={rid}", tmp.name)
                    res = migrate.import_bundle(db_path, tmp.name)
                    if res.get("action") == "skipped":
                        stats[t]["skipped"] += 1
                        print(f"  [跳过] id={rid} {title}（{res.get('reason')}）")
                    else:
                        stats[t]["imported"] += 1
                        print(f"  [导入] id={rid} {title} -> 本地 #{res.get('id')}")
                except Exception as exc:  # noqa: BLE001
                    stats[t]["failed"] += 1
                    print(f"  [失败] id={rid} {title}: {str(exc)[:200]}")
                finally:
                    try:
                        os.remove(tmp.name)
                    except OSError:
                        pass
                done += 1
                if limit and done >= limit:
                    break
            if len(items) < PAGE:
                break
        s = stats[t]
        print(f"=== {t} 完成：导入 {s['imported']}，跳过 {s['skipped']}，失败 {s['failed']} ===")
    failed_total = sum(s["failed"] for s in stats.values())
    return 1 if failed_total else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m clawdata.migrate",
        description="跨机迁移：按资产逐文件传输（视频/文库文档 + 元数据）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser("pull", help="从源面板逐个拉取资产并导入本机库")
    p_pull.add_argument("--from", dest="source", required=True,
                        help="源面板地址，如 http://192.168.1.10:8000")
    p_pull.add_argument("--type", default="downloads,digests",
                        help="资产类型，逗号分隔：downloads,digests（默认两者）")
    p_pull.add_argument("--limit", type=int, default=0, help="每类最多处理条数（0=不限）")
    p_pull.add_argument("--dry-run", action="store_true", help="只列出不落库")
    p_pull.add_argument("--db", default=DEFAULT_DB_PATH, help="本机库路径")

    p_imp = sub.add_parser("import-file", help="导入一个资产包 zip")
    p_imp.add_argument("zip", help="资产包路径")
    p_imp.add_argument("--db", default=DEFAULT_DB_PATH, help="本机库路径")

    p_exp = sub.add_parser("export", help="把本机一个资产打成 zip")
    p_exp.add_argument("--type", default="downloads", help="downloads 或 digests")
    p_exp.add_argument("--id", type=int, required=True, help="资产记录 id")
    p_exp.add_argument("--out", required=True, help="输出 zip 路径")
    p_exp.add_argument("--db", default=DEFAULT_DB_PATH, help="本机库路径")

    args = parser.parse_args(argv)
    if args.cmd == "pull":
        types = [migrate._norm_type(x) for x in str(args.type).split(",") if x.strip()]
        return _pull(args.source, types, args.limit, args.dry_run, args.db)
    if args.cmd == "import-file":
        res = migrate.import_bundle(args.db, args.zip)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    info = migrate.build_bundle(args.db, args.type, args.id, args.out)
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
