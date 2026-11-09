"""Machine readings for the dashboard and the System health page.

* Disk: used / (used + free) of the data folder, the same measure as the cleanup and
  dpc-check (CONTRACTS 10), graded with cleanup.disk_percent from ui.json.
* GPU: one `nvidia-smi --query-gpu` call, cached a few seconds.
* CPU / RAM: /proc (no extra package).
* Ping: cameras by IP, run by the watcher thread every PING_EVERY_S; pages only read results.
Every reading returns a dict with "ok": False and an "error" text instead of raising.
"""
import shutil
import subprocess
import threading
import time

GPU_CACHE_S = 5
GPU_FIELDS = ["name", "temperature.gpu", "clocks.sm", "utilization.gpu", "memory.used", "memory.total"]

_gpu_cache = {"at": 0.0, "value": None}
_cpu_prev = {"total": None, "idle": None, "percent": None}
_ping_results = {}            # ip -> {"ok", "checked_at", "error"}
_lock = threading.Lock()


# ------------------------------------------------------------------------------- disk
def disk(path: str, thresholds: dict) -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return {"ok": False, "error": str(e), "level": "unknown", "percent": None}
    percent = round(100.0 * usage.used / (usage.used + usage.free), 1) if usage.used + usage.free else 0.0
    return {"ok": True, "percent": percent, "level": disk_level(percent, thresholds),
            "free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1),
            "thresholds": thresholds}


def disk_level(percent: float, thresholds: dict) -> str:
    """ok below `delete`; delete (old images being removed) up to `mail`; mail at or above."""
    if percent is None:
        return "unknown"
    if percent >= float(thresholds.get("mail", 90)):
        return "mail"
    if percent >= float(thresholds.get("delete", 75)):
        return "delete"
    return "ok"


# ------------------------------------------------------------------------------- gpu
def gpu(now=None, runner=subprocess.run) -> dict:
    now = now or time.time()
    with _lock:
        if _gpu_cache["value"] is not None and now - _gpu_cache["at"] < GPU_CACHE_S:
            return _gpu_cache["value"]
    try:
        out = runner(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"],
                     capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout or "nvidia-smi failed").strip()[:200])
        value = {"ok": True, "gpus": [parse_gpu_line(line) for line in out.stdout.strip().splitlines() if line.strip()]}
    except FileNotFoundError:
        value = {"ok": False, "error": "nvidia-smi not found (no NVIDIA driver?)", "gpus": []}
    except Exception as e:
        value = {"ok": False, "error": str(e)[:200], "gpus": []}
    with _lock:
        _gpu_cache.update(at=now, value=value)
    return value


def parse_gpu_line(line: str) -> dict:
    parts = [p.strip() for p in line.split(",")]
    parts += [""] * (len(GPU_FIELDS) - len(parts))

    def num(text):
        try:
            return float(text)
        except ValueError:
            return None
    return {"name": parts[0], "temperature_c": num(parts[1]), "clock_mhz": num(parts[2]),
            "load_percent": num(parts[3]), "memory_used_mb": num(parts[4]), "memory_total_mb": num(parts[5])}


# ------------------------------------------------------------------------------- cpu / ram
def cpu_ram(proc="/proc") -> dict:
    result = {"ok": True}
    try:
        with open(f"{proc}/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        with _lock:
            if _cpu_prev["total"] is not None and total > _cpu_prev["total"]:
                busy = 1.0 - (idle - _cpu_prev["idle"]) / (total - _cpu_prev["total"])
                _cpu_prev["percent"] = round(100.0 * max(0.0, min(1.0, busy)), 1)
            _cpu_prev.update(total=total, idle=idle)
            result["cpu_percent"] = _cpu_prev["percent"]
        with open(f"{proc}/loadavg") as f:
            result["load"] = [float(x) for x in f.read().split()[:3]]
        mem = {}
        with open(f"{proc}/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                mem[key] = int(rest.split()[0])          # kB
        total_kb, avail_kb = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        result["ram_total_gb"] = round(total_kb / 1e6, 1)
        result["ram_used_percent"] = round(100.0 * (total_kb - avail_kb) / total_kb, 1) if total_kb else None
    except (OSError, ValueError, IndexError) as e:
        result = {"ok": False, "error": str(e)}
    return result


# ------------------------------------------------------------------------------- ping
def ping(ip: str, runner=subprocess.run) -> dict:
    """One ICMP ping, 1 s timeout (like dpc-check). Called by the watcher, not by pages."""
    try:
        out = runner(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True, timeout=3)
        result = {"ok": out.returncode == 0, "error": "" if out.returncode == 0 else "no answer"}
    except FileNotFoundError:
        result = {"ok": None, "error": "ping not installed"}
    except Exception as e:
        result = {"ok": False, "error": str(e)[:120]}
    result["checked_at"] = time.time()
    with _lock:
        _ping_results[ip] = result
    return result


def ping_result(ip: str):
    with _lock:
        return dict(_ping_results[ip]) if ip in _ping_results else None
