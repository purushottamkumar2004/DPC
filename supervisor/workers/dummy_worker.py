"""
Dummy worker - only exists to test the supervisor.

It "works" by touching a heartbeat file every few seconds (and optionally
publishing an MQTT heartbeat). It can misbehave on purpose:

  --crash-after N   exit with an error after N seconds   (process dies)
  --hang-after N    keep running but stop heartbeats     (alive but unhealthy)

For oneshot / periodic / start_after tests (STATUS item 3):
  --exit-after N    exit after N seconds with --exit-code (default 0)   (a clean "job done")
  --log-start FILE  append one line "<worker id> <unix time>" when the worker starts
  --dump-env FILE   write every DPC_* variable as NAME=value to FILE (never to stdout, so the
                    worker log stays free of secrets); --print-env-names prints the names only
"""

import argparse
import json
import os
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=os.environ.get("DPC_WORKER_ID", "dummy"))
    p.add_argument("--heartbeat-file", help="file touched on every loop")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--crash-after", type=float, default=0)
    p.add_argument("--hang-after", type=float, default=0)
    p.add_argument("--mqtt", action="store_true", help="also publish MQTT heartbeats")
    p.add_argument("--exit-after", type=float, default=0)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--log-start", help="append a line to this file at start")
    p.add_argument("--dump-env", help="write the DPC_* environment variables to this file")
    p.add_argument("--print-env-names", action="store_true", help="print the names of the DPC_* variables")
    args = p.parse_args()

    if args.log_start:
        with open(args.log_start, "a") as f:
            f.write(f"{args.name} {time.time()}\n")
    if args.dump_env:
        with open(args.dump_env, "w") as f:
            for name in sorted(os.environ):
                if name.startswith("DPC_"):
                    f.write(f"{name}={os.environ[name]}\n")
    if args.print_env_names:
        print("env:", " ".join(sorted(n for n in os.environ if n.startswith("DPC_"))), flush=True)

    client = None
    if args.mqtt:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
        client.connect_async(os.environ.get("DPC_MQTT_HOST", "localhost"),
                             int(os.environ.get("DPC_MQTT_PORT", "1883")))
        client.loop_start()

    start = time.time()
    print(f"[{args.name}] started, pid={os.getpid()}", flush=True)

    while True:
        elapsed = time.time() - start

        if args.crash_after and elapsed >= args.crash_after:
            print(f"[{args.name}] simulating crash", flush=True)
            raise SystemExit(1)

        if args.exit_after and elapsed >= args.exit_after:
            print(f"[{args.name}] finished, exit code {args.exit_code}", flush=True)
            raise SystemExit(args.exit_code)

        hanging = args.hang_after and elapsed >= args.hang_after
        if hanging:
            print(f"[{args.name}] simulating hang (alive, but no heartbeat)", flush=True)
        else:
            if args.heartbeat_file:
                os.makedirs(os.path.dirname(args.heartbeat_file), exist_ok=True)
                with open(args.heartbeat_file, "w") as f:   # writing real content updates mtime reliably
                    f.write(f"{time.time()}\n")
            if client:
                client.publish(f"dpc/worker/{args.name}/health",
                               json.dumps({"worker_id": args.name, "healthy": True}))
            print(f"[{args.name}] working... {elapsed:.0f}s", flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
