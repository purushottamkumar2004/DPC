"""
Dummy worker - only exists to test the supervisor.

It "works" by touching a heartbeat file every few seconds (and optionally
publishing an MQTT heartbeat). It can misbehave on purpose:

  --crash-after N   exit with an error after N seconds   (process dies)
  --hang-after N    keep running but stop heartbeats     (alive but unhealthy)
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
    args = p.parse_args()

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
