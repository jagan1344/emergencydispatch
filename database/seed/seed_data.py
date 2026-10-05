"""Seed synthetic demo data (wrapper around backend/app/seed.py).

    python database/seed/seed_data.py --reset --seed 42
"""
import os
import runpy
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))
os.chdir(os.path.abspath(BACKEND))
runpy.run_module("app.seed", run_name="__main__")
