"""CLI: python -m ink2reel_driver run ..."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ink2reel_driver", description="Ink2Reel 无人值守驱动器")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="跑文本链 / 媒体链")
    run_p.add_argument("novel", type=str, help="小说 txt 路径")
    run_p.add_argument("--project", default="midnight", help="项目名")
    run_p.add_argument(
        "--until",
        choices=["script", "assets", "storyboard", "video", "deliverables", "media"],
        default="script",
        help="media 等价 deliverables",
    )
    run_p.add_argument("--gate", choices=["auto", "manual", "threshold"], default="auto")
    run_p.add_argument(
        "--threshold",
        type=float,
        default=50.0,
        help="threshold 门总分下限（六维 0–60，默认 50）",
    )
    run_p.add_argument("--episode", type=int, default=1)
    run_p.add_argument("--title", default=None)
    run_p.add_argument("--no-export", action="store_true")
    run_p.add_argument("--limit-segments", type=int, default=None, help="媒体阶段只处理前 N 段")
    run_p.add_argument("--budget-usd", type=float, default=5.0, help="媒体预算上限（USD）")
    run_p.add_argument(
        "--no-worker",
        action="store_true",
        help="不启动进程内 GenerationWorker（假定外部 worker 已在跑）",
    )

    exp_p = sub.add_parser("export", help="仅导出交付物")
    exp_p.add_argument("--project", default="midnight")
    exp_p.add_argument("--episode", type=int, default=1)
    exp_p.add_argument("--limit-segments", type=int, default=None)

    ex_p = sub.add_parser("exceptions", help="异常队列")
    ex_sub = ex_p.add_subparsers(dest="ex_cmd", required=True)
    list_p = ex_sub.add_parser("list", help="列出 open 异常")
    list_p.add_argument("--project", required=True)
    list_p.add_argument("--all", action="store_true", help="含已 resolved")
    res_p = ex_sub.add_parser("resolve", help="处理异常")
    res_p.add_argument("exception_id", type=str)
    res_p.add_argument("--project", required=True)
    res_p.add_argument("--action", choices=["approve", "regenerate", "skip"], required=True)

    watch_p = sub.add_parser("watch", help="消费 queue/inbox 投喂闭环")
    watch_p.add_argument(
        "--once",
        action="store_true",
        help="只扫描消费一次（适合计划任务）",
    )
    watch_p.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="常驻循环间隔秒（默认 300；与 --once 互斥时以 --once 为准）",
    )
    watch_p.add_argument(
        "--queue-root",
        type=str,
        default=None,
        help="queue 根目录（默认 ink2reel/queue）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "export":
        from ink2reel_driver.exporter import export_episode

        out = export_episode(
            project_name=args.project,
            episode=args.episode,
            limit_segments=args.limit_segments,
        )
        print(f"exported: {out}")
        return 0

    if args.command == "exceptions":
        from ink2reel_driver.exceptions import list_exceptions, resolve_exception
        from ink2reel_driver.journal import DriverJournal
        from lib.project_manager import get_project_manager

        pm = get_project_manager()
        if args.ex_cmd == "list":
            status = None if args.all else "open"
            rows = list_exceptions(project_name=args.project, status=status, pm=pm)
            if not rows:
                print("(empty)")
                return 0
            for r in rows:
                print(f"{r.id}\t{r.status}\t{r.stage}\t{r.target_id}\t{';'.join(r.reasons)[:80]}")
            return 0
        if args.ex_cmd == "resolve":
            rec = resolve_exception(
                args.exception_id,
                project_name=args.project,
                action=args.action,
                pm=pm,
            )
            # regenerate/skip 落 journal 待消费
            jpath = pm.get_project_path(args.project) / "driver_journal.jsonl"
            journal = DriverJournal(jpath)
            journal.info(
                "exception_resolve",
                exception_id=rec.id,
                action=args.action,
                target_id=rec.target_id,
                gate_stage=rec.stage,
            )
            print(f"resolved {rec.id} action={args.action}")
            return 0
        return 2

    if args.command == "watch":
        from ink2reel_driver.watcher import default_queue_root, run_forever, scan_once

        root = Path(args.queue_root) if args.queue_root else default_queue_root()
        if args.once:
            r = scan_once(root)
            print(json_dumps_safe(r.as_dict()))
            return 0 if r.status in ("consumed", "empty", "skipped") else 1
        run_forever(interval_s=args.interval, queue_root=root)
        return 0

    if args.command == "run":
        from ink2reel_driver.pipeline import PipelineError, run

        novel = Path(args.novel)
        try:
            summary = asyncio.run(
                run(
                    novel,
                    project_name=args.project,
                    until=args.until,
                    gate_mode=args.gate,
                    episode=args.episode,
                    title=args.title,
                    export=not args.no_export,
                    limit_segments=args.limit_segments,
                    budget_usd=args.budget_usd,
                    start_worker=not args.no_worker,
                    threshold=args.threshold,
                )
            )
        except PipelineError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return e.exit_code
        except NotImplementedError as e:
            print(f"NOT_IMPLEMENTED: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

        print("OK")
        for k, v in summary.items():
            if k.startswith("_"):
                continue
            print(f"  {k}: {v}")
        return 0

    return 2


def json_dumps_safe(obj: object) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
