#!/usr/bin/env python3
"""Entry point of the `ui` worker (started by the Supervisor, D-095, F59).

  1. load ui.json (DPC_WORKER_CONFIG; safe defaults + one WARNING per missing key)
  2. create <root>/data/state/ui and apply the UI's own SQLite migrations
  3. start the MQTT state cache (one subscriber on dpc/#, also the UI heartbeat),
     the frame meter and the background checks
  4. serve with waitress on 127.0.0.1:<ui.port> only, until SIGTERM / SIGINT

The UI never touches the site MySQL schema and never writes outside data/state/ui.
"""
import logging
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dpc_ui.settings")

LISTEN_HOST = "127.0.0.1"     # D-095: local only; never 0.0.0.0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("dpc.ui")

    from dpc_ui import ui_config
    cfg = ui_config.load()
    ui_config.set_config(cfg)
    os.makedirs(cfg.paths["state"], exist_ok=True)

    import django
    django.setup()
    from django.core.management import call_command
    call_command("migrate", interactive=False, verbosity=0)

    from monitor.sources import files
    from monitor.state import set_cache
    from monitor.state.cache import StateCache
    from monitor.state.event_store import EventStore
    from monitor.state.subscriber import Subscriber
    from monitor.state.watchers import Watchers

    store = EventStore()
    cache = StateCache(recorder=store, topic_prefix=cfg["mqtt"].get("topic_prefix") or "dpc")
    set_cache(cache)
    subscriber = Subscriber(cache, cfg["mqtt"], worker_id=cfg.get("worker_id") or "ui")
    subscriber.start()
    meter = files.FrameMeter(cfg.paths["frames"], [c["id"] for c in cfg["cameras"]])
    files.set_meter(meter)
    meter.start()
    watchers = Watchers(cfg, store)
    watchers.start()

    from waitress.server import create_server
    from dpc_ui.wsgi import application
    port = int(cfg["ui"].get("port") or 8080)
    server = create_server(application, host=LISTEN_HOST, port=port, threads=8, ident="dpc-ui")

    def stop(signum, _frame):
        log.info("signal %s: stopping", signum)
        server.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("DPC UI on http://%s:%d (site %s)", LISTEN_HOST, port, cfg["site"].get("name"))
    try:
        server.run()
    except (OSError, ValueError) as e:
        if not getattr(server, "_closed", False) and "closed" not in str(e).lower() and "Bad file" not in str(e):
            log.error("web server stopped: %s", e)
            return 1
    finally:
        watchers.stop()
        meter.stop()
        subscriber.stop()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
