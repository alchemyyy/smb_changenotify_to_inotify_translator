#!/usr/bin/env python3
"""
Verify that inotify events are firing on the watched paths.

Reads local_path entries from the translator config and watches them
with inotifywait.  Any events that appear confirm the kernel module
and translator are working end-to-end.

Usage: python3 verify_inotify.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    config_path = Path(__file__).resolve().parent / "smb_changenotify_to_inotify_translator_config.json"
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    paths = [w["local_path"] for w in config["watches"] if os.path.exists(w["local_path"])]
    if not paths:
        print("No valid local_path entries found in config.")
        sys.exit(1)

    print("Watching for inotify events on:")
    for p in paths:
        print(f"  {p}")
    print()
    print("Make a change on the SMB server and see if events appear here.")
    print("Ctrl+C to stop.\n")

    try:
        subprocess.run(
            ["inotifywait", "-m", "--format", "%T %e %w%f", "--timefmt", "%H:%M:%S"] + paths,
        )
    except FileNotFoundError:
        print("inotifywait not found. Install it: apt-get install inotify-tools")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
