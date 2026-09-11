from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import TIMEZONE
from .updater import refresh_all
from .history import grade_pending

REFRESH_TIMES = [(9, 0), (12, 0), (15, 0), (18, 0), (20, 30), (23, 0)]

def start_scheduler():
    s = BackgroundScheduler(timezone=TIMEZONE)
    for i, (h, m) in enumerate(REFRESH_TIMES, 1):
        s.add_job(
            refresh_all,
            CronTrigger(hour=h, minute=m, timezone=TIMEZONE),
            id=f"nfl_refresh_{i}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    # nflverse box-score data can update after overnight processing, so grade daily.
    s.add_job(
        grade_pending,
        CronTrigger(hour=10, minute=15, timezone=TIMEZONE),
        id="nfl_history_grade",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    s.start()
    return s
