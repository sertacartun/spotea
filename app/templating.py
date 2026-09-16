from fastapi.templating import Jinja2Templates

from app.formatting import format_duration, format_size
from app.images import track_cover

# The single Jinja environment; every router must render through it so filters are available.
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size
templates.env.filters["cover"] = track_cover
