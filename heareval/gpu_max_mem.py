#!/usr/bin/env python3
"""
Profile GPU maximum memory usage.
"""

from typing import Optional

import torch
from pynvml import nvmlDeviceGetHandleByIndex, nvmlDeviceGetMemoryInfo, nvmlInit

nvmlInit()

max_memory_used: Optional[float] = None


def reset():
    global max_memory_used
    max_memory_used = None


def measure() -> Optional[float]:
    global max_memory_used
    if torch.cuda.is_available():
        h = nvmlDeviceGetHandleByIndex(0)
        info = nvmlDeviceGetMemoryInfo(h)
        # Convert to GB
        memory_used: float = info.used / 1024 / 1024 / 1024
        if max_memory_used is None or memory_used > max_memory_used:
            max_memory_used = memory_used
    return max_memory_used
