"""无 Web API 的本地调度入口。

运行后会启动 APScheduler，按固定频率执行：
- quote_worker:      每 5 分钟
- guardian_scan:     每 5 分钟（错开）
- opportunity_scan:  每 5 分钟（错开）
- news_worker:       每 15 分钟
- brain_worker:      每小时
- daily_reflection:  工作日 15:10

用法：python scripts/run_scheduler.py
"""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    from core import db, guardian, reflection, opportunity
    from core.config import ALWAYS_ON
    from orchestrator import workers
except ImportError as e:
    raise RuntimeError(
        "Import failed. Please run this script from project root: python scripts/run_scheduler.py"
    ) from e


scheduler = BlockingScheduler(timezone="Asia/Shanghai")


def _safe(fn, name: str):
    try:
        result = fn()
        db.log_agent("sched", name, "summary", str(result)[:1000], raw=result)
        return result
    except Exception as exc:
        db.log_agent("sched", name, "error", f"{type(exc).__name__}: {exc}")
        return None


def _is_active_window() -> bool:
    if ALWAYS_ON:
        return True
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return 9 <= now.hour < 16


def _quote_job():
    if _is_active_window():
        _safe(workers.quote_worker, "quote_worker")


def _news_job():
    if _is_active_window():
        _safe(workers.news_worker, "news_worker")


def _brain_job():
    if _is_active_window():
        _safe(workers.brain_worker, "brain_worker")


def _guardian_job():
    if _is_active_window():
        _safe(guardian.guardian_scan, "guardian")


def _opportunity_job():
    if _is_active_window():
        _safe(opportunity.opportunity_scan, "opportunity")


def _reflection_job():
    if datetime.now().weekday() < 5:
        _safe(reflection.run_daily_reflection, "reflection")


def main():
    db.init_db()

    scheduler.add_job(
        _quote_job,
        CronTrigger(minute="*/5"),
        id="quote_5m",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guardian_job,
        CronTrigger(minute="2-59/5"),
        id="guardian_5m",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _opportunity_job,
        CronTrigger(minute="3-59/5"),
        id="opportunity_5m",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _news_job,
        CronTrigger(minute="*/15"),
        id="news_15m",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _brain_job,
        CronTrigger(minute="0"),
        id="brain_60m",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reflection_job,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=10),
        id="reflection_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        db.make_available_t1,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=25),
        id="daily_open",
        replace_existing=True,
    )

    print("[scheduler] started (timezone=Asia/Shanghai)")
    print(f"[scheduler] ALWAYS_ON={ALWAYS_ON}")
    scheduler.start()


if __name__ == "__main__":
    main()
