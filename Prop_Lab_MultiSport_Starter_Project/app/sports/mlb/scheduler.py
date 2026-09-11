from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from .config import TIMEZONE
from .updater import refresh_all
from .history import grade_pending_history

TIMES=[(10,0),(11,30),(13,0),(14,30),(16,0),(17,30),(19,0),(20,30),(22,0),(23,30)]

def start_scheduler():
    s=BackgroundScheduler(timezone=TIMEZONE)
    for i,(h,m) in enumerate(TIMES,1):
        s.add_job(refresh_all,CronTrigger(hour=h,minute=m,timezone=TIMEZONE),
                  id=f"refresh_{i}",replace_existing=True,max_instances=1,coalesce=True)
    # Grade yesterday's saved A/B picks every morning after late games have finished.
    s.add_job(
        grade_pending_history,
        CronTrigger(hour=9, minute=15, timezone=TIMEZONE),
        id="grade_mlb_history",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    s.start()
    return s
