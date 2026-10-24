"""Pick the right Rivet provider for a config.

New installs enroll with an `enrollment_token` (per-device credential, scoped
server side). Installs that still carry the legacy shared `api_key` keep working
on the old provider until they are re-deployed.
"""

from .panel import PanelProvider
from .rivet import RivetProvider


def make_provider(config):
    legacy = config.get("api_key") and not config.get("enrollment_token")
    if legacy:
        return RivetProvider(config)
    return PanelProvider(config)
