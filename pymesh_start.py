#!/usr/bin/env python3
"""
PyMesh Chat — Launcher
Run this from the pymesh_chat/ folder (the folder you downloaded):

    python3 pymesh_start.py --alias yourname --session test

Two terminals on the same network:
    # Terminal 1
    python3 pymesh_start.py --alias alice --session dev-team

    # Terminal 2 (connect manually if mDNS not available)
    python3 pymesh_start.py --alias bob --session dev-team --connect 192.168.1.x
"""

import sys
import os

# Add this folder to the path so Python can find the 'pymesh' package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pymesh.main import run

if __name__ == "__main__":
    run()
