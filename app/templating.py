import time

from fastapi.templating import Jinja2Templates

from app.formatting import format_duration, format_size
from app.images import track_cover
from app.version import APP_VERSION

# The single Jinja environment; every router must render through it so filters are available.
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size
templates.env.filters["cover"] = track_cover
# A global, not per-context: _base.html stamps it on every page, login included, and the
# client compares it with what /updates reports to notice it is running yesterday's document.
templates.env.globals["app_version"] = APP_VERSION
# Called per render (a global value would freeze at import). Epoch milliseconds so the client
# can subtract it from Date.now() without parsing; see _base.html and fragments.js.
templates.env.globals["rendered_at_ms"] = lambda: int(time.time() * 1000)
