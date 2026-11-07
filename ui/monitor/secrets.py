"""Second guard against showing a secret: every text the UI displays from a log, a traceback
or an event payload passes through mask(). The first guard is that workers never write
secrets (CONTRACTS 2, D-079)."""
from dpc_ui.ui_config import get_config

MASK = "********"


def mask(text: str) -> str:
    if not text:
        return text
    for secret in get_config().secret_values():
        text = text.replace(secret, MASK)
    return text
