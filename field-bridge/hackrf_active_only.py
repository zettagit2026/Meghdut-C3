#!/usr/bin/env python3
"""Standalone active-only capture (drone already ON), no baseline re-capture."""
import sys
sys.path.insert(0, ".")
from hackrf_baseline_test import summarize

for i in range(4):
    print(f"--- pass {i+1} ---")
    summarize("active")
