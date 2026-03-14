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
import threading
from pathlib import Path


def main():
    config_path = Path(__file__).resolve().parent / "smb_changenotify_to_inotify_translator_config.json"
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    # Map each local_path back to its server + username for display
    path_to_server = {}
    paths = []
    for srv in config["servers"]:
        server = srv["smb_server"]
        username = srv["smb_username"]
        for w in srv["watches"]:
            lp = w["local_path"]
            if os.path.exists(lp):
                paths.append(lp)
                path_to_server[lp] = (server, username, w["share"], w["remote_path"])

    if not paths:
        print("No valid local_path entries found in config.")
        sys.exit(1)

    print("Watching for inotify events on:")
    for p in paths:
        server, username, share, remote = path_to_server[p]
        print(f"  {p}  <-  {username}@{server}\\{share}\\{remote}")
    print()
    print("Make a change on an SMB server and see if events appear here.")
    print("Ctrl+C to stop.\n")

    def watch_path(local_path, server, username, share):
        """Run inotifywait on a single path, prefixing output with server info."""
        tag = f"[{username}@{server}\\{share}]"
        try:
            proc = subprocess.Popen(
                ["inotifywait", "-m", "--format", "%T %e %w%f", "--timefmt", "%H:%M:%S", local_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    print(f"{tag} {line}", flush=True)
        except FileNotFoundError:
            print("inotifywait not found. Install it: apt-get install inotify-tools")
            sys.exit(1)

    threads = []
    for p in paths:
        server, username, share, remote = path_to_server[p]
        t = threading.Thread(target=watch_path, args=(p, server, username, share), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            pass
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
