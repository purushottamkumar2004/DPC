"""
SiteEnv - every place the Supervisor reads site.yaml goes through here, so the rules
below hold for all of them:
  * variables()   the legacy env bridge (CONTRACTS 9, D-048, D-049)
  * mqtt_login()  the Supervisor's own broker login, mqtt.user / mqtt.password (F11)
  * render()      the rendered worker configs, <root>/config/rendered/<id>.json (6f, D-062)

The legacy env bridge:

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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("supervisor.site_env")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # .../code


class SiteEnvError(Exception):
    """site.yaml could not be turned into environment variables. The text is safe to log."""


@dataclass
class RenderResult:
    """What render() did. `changes` maps worker_id -> written | unchanged | removed.
    `error` is None on success; on an error nothing was written and the text is safe to log."""
    error: Optional[str] = None
    changes: Dict[str, str] = field(default_factory=dict)
    rendered_dir: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def ids(self, status: str) -> List[str]:
        return sorted(w for w, s in self.changes.items() if s == status)


class SiteEnv:
    def __init__(self, site_config_path: Optional[str] = None):
        self.site_config_path = site_config_path     # None = $DPC_SITE_CONFIG, then the default

    def load(self):
        """The validated SiteConfig. Raises SiteEnvError (safe text)."""
        load_site_config, _, site_config_error = self._import_loader()
        return self._safely(lambda: load_site_config(self.site_config_path), site_config_error)

    def variables(self) -> Dict[str, str]:
        """The DPC_* variables for one worker start. Raises SiteEnvError."""
        _, legacy_env_vars, _ = self._import_loader()
        config = self.load()
        return legacy_env_vars(config)

    SECRET_SUFFIXES = ("PASS", "PASSWORD", "SECRET")

    def secret_values(self) -> List[str]:
        """The secret values in site.yaml (DB, mail, MQTT passwords and secrets), so a text from
        a worker can be scrubbed before it is logged or published. [] when site.yaml is not
        readable (then there is nothing of it to leak)."""
        try:
            variables = self.variables()
        except SiteEnvError:
            return []
        return [v for name, v in variables.items() if v and name.endswith(self.SECRET_SUFFIXES)]

    def mqtt_login(self) -> Tuple[Optional[str], Optional[str]]:
        """(user, password) for the broker, (None, None) when site.yaml gives no login. Raises SiteEnvError."""
        mqtt = self.load().mqtt
        return (mqtt.user or None, mqtt.password or None) if mqtt.user else (None, None)

    def render(self) -> RenderResult:
        """Run the renderer (common.config.render). Never raises: a site.yaml problem is
        returned as `error` and the renderer has then written nothing."""
        try:
            _, _, site_config_error = self._import_loader()
            from common.config.render import render
            changes = self._safely(lambda: render(self.site_config_path), site_config_error)
        except SiteEnvError as exc:
            return RenderResult(error=str(exc), rendered_dir=self.default_rendered_dir())
        except ImportError as exc:
            return RenderResult(error=f"renderer not importable ({type(exc).__name__})",
                                rendered_dir=self.default_rendered_dir())
        folder = os.path.dirname(changes[0].path) if changes else self.default_rendered_dir()
        return RenderResult(changes={c.worker_id: c.status for c in changes}, rendered_dir=folder)

    def site_yaml_path(self) -> Optional[str]:
        if self.site_config_path:
            return os.path.abspath(os.path.expanduser(self.site_config_path))
        try:
            self._import_loader()
            from common.config.site_config import default_config_path
            return default_config_path()
        except (SiteEnvError, ImportError):
            return None

    def default_rendered_dir(self) -> Optional[str]:
        """<folder of site.yaml>/rendered: where the last good render is when site.yaml
        cannot be read now (CONTRACTS 4.1: site.yaml and rendered/ are both in <root>/config)."""
        path = self.site_yaml_path()
        return os.path.join(os.path.dirname(path), "rendered") if path else None

    @staticmethod
    def _safely(action, site_config_error):
        """Run a loader call; turn every error into a SiteEnvError whose text holds no value."""
        try:
            return action()
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
