# Backward-compatibility shim: implementation moved to domain modules.
#
# Only the symbols the modules own are re-exported. The service and
# third-party names the old module happened to import (``token_cache``,
# ``Depends``, ``Session``, …) are deliberately left out: a stale
# ``patch("app.api.endpoints.fleet.token_cache")`` then fails loudly with an
# AttributeError instead of silently patching a binding nothing reads.
import logging

from app.api.endpoints.fleet_credentials import *  # noqa: F401,F403
from app.api.endpoints.fleet_ingest import *  # noqa: F401,F403
from app.api.endpoints.fleet_pairing import *  # noqa: F401,F403
from app.api.endpoints.fleet_pending_events import *  # noqa: F401,F403
from app.api.endpoints.fleet_shared import *  # noqa: F401,F403
from app.api.endpoints.fleet_sidecars import *  # noqa: F401,F403

# Kept for one release so ``from app.api.endpoints.fleet import logger`` still
# resolves; the code that logs now uses each module's own logger.
logger = logging.getLogger(__name__)
