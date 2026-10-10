"""
RuntimeManager - creates the folders a worker says it needs.

It knows nothing about cameras. A worker just lists folder names:

    runtime_dir: CAM1
    directories: [raw, inference, defects, logs]

-> <runtime root>/CAM1/raw, <runtime root>/CAM1/inference, ...

A directory given as an absolute path (e.g. /media/disk3/DPC_02/...) is
created exactly where it is, so existing site layouts keep working.
"""

import logging
import os
from typing import List

log = logging.getLogger("supervisor.runtime")


class RuntimeManager:
    def __init__(self, runtime_root: str):
        self.runtime_root = runtime_root

    def prepare(self, worker_config) -> List[str]:
        """Create all directories for one worker. Returns the list of paths."""
        base = os.path.join(self.runtime_root, worker_config.runtime_dir or worker_config.id)
        created = []
        for name in worker_config.directories:
            path = name if os.path.isabs(name) else os.path.join(base, name)
            try:
                os.makedirs(path, exist_ok=True)
                created.append(path)
            except OSError as e:
                # Not fatal for the supervisor - the worker may still run.
                log.error("[%s] cannot create directory %s: %s", worker_config.id, path, e)
        return created
