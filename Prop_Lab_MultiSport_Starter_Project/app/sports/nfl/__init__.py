# Install the strict Underdog primary-line guard before routes/schedulers import
# refresh functions. Keeping this at package initialization means every NFL
# refresh path (manual and scheduled) uses the same filtered source rows.
from . import updater as _updater
from .primary_filter import install_primary_line_filter

install_primary_line_filter(_updater)
