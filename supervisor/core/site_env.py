"""
SiteEnv - the legacy env bridge (CONTRACTS 9, D-048, D-049).

Some programs (today only workers/cleanup/delete_script.py) still read their DB and mail
secrets from DPC_* environment variables. A worker with `env_from_site: true` gets them
from the Supervisor, which reads site.yaml with the shared loader (common/config) and
exports the values with legacy_env_vars().

Rules that keep the secrets safe:
  * site.yaml is read again at EVERY start of such a worker, so a rotated password only
    needs `restart <worker>`, not a restart of the whole Supervisor (which would also
    stop the algorithms and lose their wheel/plate state).
  * Nothing here logs, prints or publishes a value. Errors say what is wrong with the
    file (the loader already keeps values out of its messages), never what is in it.
    An unexpected exception is reported by its type name only.
  * The values go from the loader straight into the child's environment, and nowhere else.

`common` is imported only when a worker asks for it, so a Supervisor without any
env_from_site worker needs nothing outside supervisor/.
"""

import logging
import os
import sys
from typing import Dict, Optional

log = logging.getLogger("supervisor.site_env")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # .../code


class SiteEnvError(Exception):
    """site.yaml could not be turned into environment variables. The text is safe to log."""


class SiteEnv:
    def __init__(self, site_config_path: Optional[str] = None):
        self.site_config_path = site_config_path     # None = $DPC_SITE_CONFIG, then the default

    def variables(self) -> Dict[str, str]:
        """The DPC_* variables for one worker start. Raises SiteEnvError."""
        load_site_config, legacy_env_vars, site_config_error = self._import_loader()
        try:
            config = load_site_config(self.site_config_path)
            return legacy_env_vars(config)
        except site_config_error as exc:
            raise SiteEnvError(str(exc)) from None           # the loader's message holds no values
        except OSError as exc:
            raise SiteEnvError(f"cannot read site config: {exc.strerror or type(exc).__name__}") from None
        except Exception as exc:                              # a bug must not leak a value either
            raise SiteEnvError(f"unexpected error while loading site config ({type(exc).__name__})") from None

    def check(self) -> Optional[str]:
        """For `check-config`: None when site.yaml is fine, otherwise the problem text."""
        try:
            self.variables()
        except SiteEnvError as exc:
            return str(exc)
        return None

    @staticmethod
    def _import_loader():
        if REPO_ROOT not in sys.path:
            sys.path.append(REPO_ROOT)          # appended: never shadows the Supervisor's own modules
        try:
            from common.config.site_config import SiteConfigError, legacy_env_vars, load_site_config
        except ImportError as exc:
            raise SiteEnvError(f"common/config is not importable ({type(exc).__name__}); "
                               f"the Supervisor must run from the repo checkout") from None
        return load_site_config, legacy_env_vars, SiteConfigError
