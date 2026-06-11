"""WC26 data layer.

Note: `build` is the CLI module run via `python -m datalayer.build`, so it is
intentionally NOT imported here — importing it would make runpy load it twice
and emit a RuntimeWarning. Import it explicitly when needed:
    from datalayer.build import build_feed, build_match
"""

from .providers import ApiFootballProvider, FileCache, Provider
from .normalize import build_player_profile, team_lambdas, per90, infer_role
