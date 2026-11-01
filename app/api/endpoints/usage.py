# Backward-compatibility shim: implementation moved to domain modules.
from app.api.endpoints._shared import *  # noqa: F401,F403
from app.api.endpoints.usage_cumulative import *  # noqa: F401,F403
from app.api.endpoints.usage_events import *  # noqa: F401,F403
from app.api.endpoints.usage_forecast import *  # noqa: F401,F403
from app.api.endpoints.usage_history import *  # noqa: F401,F403
from app.api.endpoints.usage_overview import *  # noqa: F401,F403
from app.api.endpoints.usage_overview import router as _r  # noqa: F401
from app.api.endpoints.usage_rankings import *  # noqa: F401,F403
from app.api.endpoints.usage_sessions import *  # noqa: F401,F403

router = _r

from app.services.collector_manager import manager  # noqa: F401
from app.services.queries.sessions import query_sessions_paginated  # noqa: F401
