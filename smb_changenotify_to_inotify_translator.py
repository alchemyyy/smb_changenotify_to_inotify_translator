#!/usr/bin/env python3
"""
SMB ChangeNotify to inotify Translator

Connects directly to an SMB/Samba server and subscribes to native SMB2
CHANGE_NOTIFY on specified directories. When changes are detected, injects
inotify events via the inotify_trigger kernel module so that local
applications (Navidrome, Jellyfin, etc.) pick them up.

Zero filesystem operations.  The kernel module calls fsnotify() directly
on the resolved inode — no touches, no writes, no feedback loops.

Requires:
  - pip install smbprotocol
  - inotify_trigger kernel module (see inotify_trigger/ directory)

Neat bonus: there is no reason you can't use NFS as the actual data connection.
since this is a totally independent thing.

Single script, runs on the client side only. Nothing to install on the server.
"""

import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

def _ensure_pip():
    """Make sure pip is available, installing it via apt if needed."""
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("pip not found — installing via apt...")
        subprocess.check_call(["apt-get", "update", "-qq"])
        subprocess.check_call(["apt-get", "install", "-y", "-qq", "pip"])


def _pip_install(package):
    """Install a pip package, using --break-system-packages for Debian compatibility."""
    _ensure_pip()
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", package],
    )


def _ensure_smbprotocol():
    """Import smbprotocol, auto-installing it if missing."""
    try:
        import smbprotocol  # noqa: F401
    except ImportError:
        print("'smbprotocol' not found — installing automatically...")
        try:
            _pip_install("smbprotocol")
        except Exception as e:
            print(f"ERROR: Failed to install smbprotocol: {e}")
            print("Try manually: pip install smbprotocol")
            print("  On Debian/Ubuntu: pip install --break-system-packages smbprotocol")
            sys.exit(1)
        # Verify the install worked
        try:
            import smbprotocol  # noqa: F401
        except ImportError:
            print("ERROR: smbprotocol installed but still cannot be imported.")
            sys.exit(1)
        print("'smbprotocol' installed successfully.")


_ensure_smbprotocol()

from smbprotocol.change_notify import (
    ChangeNotifyFlags,
    CompletionFilter,
    FileSystemWatcher,
)
from smbprotocol.connection import Connection
from smbprotocol.open import (
    CreateDisposition,
    CreateOptions,
    FileAttributes,
    ImpersonationLevel,
    Open,
    ShareAccess,
)
try:
    from smbprotocol.open import DirectoryAccessMask
    FILE_LIST_DIRECTORY = DirectoryAccessMask.FILE_LIST_DIRECTORY
except ImportError:
    try:
        from smbprotocol.open import FilePipePrinterAccessMask
        FILE_LIST_DIRECTORY = FilePipePrinterAccessMask.FILE_LIST_DIRECTORY
    except AttributeError:
        # Raw value from MS-SMB2 spec
        FILE_LIST_DIRECTORY = 0x00000001
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smb_changenotify_to_inotify_translator")

# Silence the noisy smbprotocol library logging
logging.getLogger("smbprotocol").setLevel(logging.WARNING)

# SMB2 FILE_ACTION constants (from MS-FSCC 2.4.42)
FILE_ACTION_ADDED = 0x00000001
FILE_ACTION_REMOVED = 0x00000002
FILE_ACTION_MODIFIED = 0x00000003
FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

ACTION_NAMES = {
    FILE_ACTION_ADDED: "CREATED",
    FILE_ACTION_REMOVED: "DELETED",
    FILE_ACTION_MODIFIED: "MODIFIED",
    FILE_ACTION_RENAMED_OLD_NAME: "RENAME_FROM",
    FILE_ACTION_RENAMED_NEW_NAME: "RENAME_TO",
}

# inotify/fsnotify event masks (identical values at both layers)
IN_MODIFY     = 0x00000002
IN_ATTRIB     = 0x00000004
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO   = 0x00000080
IN_CREATE     = 0x00000100
IN_DELETE     = 0x00000200

# SMB file action -> correct inotify event type
_SMB_TO_INOTIFY = {
    FILE_ACTION_ADDED:            IN_CREATE,
    FILE_ACTION_REMOVED:          IN_DELETE,
    FILE_ACTION_MODIFIED:         IN_MODIFY,
    FILE_ACTION_RENAMED_OLD_NAME: IN_MOVED_FROM,
    FILE_ACTION_RENAMED_NEW_NAME: IN_MOVED_TO,
}

INOTIFY_TRIGGER = "/proc/inotify_trigger"

RECONNECT_DELAY = 5
EVENT_COOLDOWN = 2  # seconds — suppress duplicate events from SMB burst-firing

DEFAULT_CONFIG = {
    "smb_server": "192.168.1.50",
    "smb_port": 445,
    "smb_username": "user",
    "smb_password": "password",
    "watches": [
        {
            "share": "example",
            "remote_path": "Video",
            "local_path": "/media/video",
        },
        {
            "share": "example",
            "remote_path": "Music",
            "local_path": "/media/music",
        },
    ],
}


# ---------------------------------------------------------------------------
# Kernel module interface
# ---------------------------------------------------------------------------

def _inject_inotify(path, mask):
    """Inject an inotify event via the kernel module.

    Writes to /proc/inotify_trigger which calls fsnotify() directly
    on the resolved inode.  Zero filesystem I/O.  Returns True on
    success, False if the path doesn't exist or the write fails.
    """
    try:
        with open(INOTIFY_TRIGGER, "w") as f:
            f.write(f"0x{mask:x} {path}")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Config / SMB helpers
# ---------------------------------------------------------------------------

def load_config():
    """Load smb_changenotify_to_inotify_translator_config.json from the same directory as this script, generating it if missing."""
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "smb_changenotify_to_inotify_translator_config.json"
    if not config_path.exists():
        with open(config_path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        log.info("Generated default config at: %s", config_path)
        log.info("Edit it with your settings, then run again.")
        sys.exit(0)
    with open(config_path, "r") as f:
        return json.load(f)


def parse_notify_buffer(data):
    """
    Parse raw FILE_NOTIFY_INFORMATION structures from SMB2 CHANGE_NOTIFY response.

    Each entry:
      ULONG NextEntryOffset   (4 bytes)
      ULONG Action            (4 bytes)
      ULONG FileNameLength    (4 bytes, in bytes)
      WCHAR FileName[]        (variable, UTF-16LE)
    """
    results = []
    offset = 0
    while offset < len(data):
        if offset + 12 > len(data):
            break
        next_offset, action, name_len = struct.unpack_from("<III", data, offset)
        name_start = offset + 12
        name_end = name_start + name_len
        if name_end > len(data):
            break
        filename = data[name_start:name_end].decode("utf-16-le")
        results.append((action, filename))
        if next_offset == 0:
            break
        offset += next_offset
    return results


def connect_to_share(server, port, username, password, share_name):
    """Establish an SMB connection and connect to a share. Returns (connection, session, tree)."""
    conn = Connection(uuid.uuid4(), server, port)
    conn.connect()
    log.info("SMB connected to %s:%d", server, port)

    session = Session(conn, username, password)
    session.connect()
    log.info("SMB authenticated as %s", username)

    tree = TreeConnect(session, r"\\%s\%s" % (server, share_name))
    tree.connect()
    log.info("SMB connected to share: %s", share_name)

    return conn, session, tree


def open_directory(tree, path):
    """Open a directory handle suitable for CHANGE_NOTIFY."""
    dir_open = Open(tree, path)
    dir_open.create(
        impersonation_level=ImpersonationLevel.Impersonation,
        desired_access=FILE_LIST_DIRECTORY,
        file_attributes=FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
        share_access=ShareAccess.FILE_SHARE_READ
        | ShareAccess.FILE_SHARE_WRITE
        | ShareAccess.FILE_SHARE_DELETE,
        create_disposition=CreateDisposition.FILE_OPEN,
        create_options=CreateOptions.FILE_DIRECTORY_FILE,
    )
    return dir_open


# ---------------------------------------------------------------------------
# Event replay — pure kernel fsnotify injection
# ---------------------------------------------------------------------------

def replay_event(action, relative_path, local_root):
    """Inject an inotify event for a remote change via the kernel module.

    Writes to /proc/inotify_trigger which calls fsnotify() directly on
    the resolved inode.  Zero filesystem operations, correct event types
    (CREATE/DELETE/MODIFY — not just ATTRIB from utime), no feedback loop.
    """
    relative_path = relative_path.replace("\\", "/")
    local_path = os.path.join(local_root, relative_path)
    action_name = ACTION_NAMES.get(action, f"UNKNOWN({action})")

    mask = _SMB_TO_INOTIFY.get(action)
    if mask is None:
        log.debug("%-12s %s (no inotify mapping)", action_name, local_path)
        return

    # Try the exact path first
    if _inject_inotify(local_path, mask):
        log.info("%-12s %s", action_name, local_path)
        return

    # Path doesn't exist (deleted/moved) — walk up to nearest existing
    # ancestor and poke it so directory watchers rescan
    target = os.path.dirname(local_path)
    while target and target != local_root:
        if _inject_inotify(target, IN_ATTRIB):
            log.info("%-12s %s (poked %s)", action_name, local_path, target)
            return
        target = os.path.dirname(target)

    # Last resort: poke the root
    if _inject_inotify(local_root, IN_ATTRIB):
        log.info("%-12s %s (poked root)", action_name, local_path)
    else:
        log.error("%-12s %s (inject failed)", action_name, local_path)


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def watch_loop(server, port, username, password, share_name, remote_path, local_path):
    """
    Main loop for one watched directory.
    Connects to the share, subscribes to CHANGE_NOTIFY, and replays events.
    Reconnects automatically on failure.
    """
    completion_filter = (
        CompletionFilter.FILE_NOTIFY_CHANGE_FILE_NAME
        | CompletionFilter.FILE_NOTIFY_CHANGE_DIR_NAME
        | CompletionFilter.FILE_NOTIFY_CHANGE_SIZE
        | CompletionFilter.FILE_NOTIFY_CHANGE_LAST_WRITE
        | CompletionFilter.FILE_NOTIFY_CHANGE_CREATION
    )

    last_seen = {}  # (action, filename) -> monotonic timestamp for cooldown

    while True:
        conn = None
        try:
            conn, session, tree = connect_to_share(
                server, port, username, password, share_name
            )
            dir_handle = open_directory(tree, remote_path)
            log.info(
                "Watching: \\\\%s\\%s\\%s -> %s",
                server,
                share_name,
                remote_path,
                local_path,
            )

            while True:
                # FileSystemWatcher sends an SMB2 CHANGE_NOTIFY and
                # blocks on .wait() until the server fires an event.
                watcher = FileSystemWatcher(dir_handle)
                watcher.start(
                    completion_filter=completion_filter,
                    flags=ChangeNotifyFlags.SMB2_WATCH_TREE,
                    output_buffer_length=65536,
                )
                results = watcher.wait()

                if results is None:
                    continue

                now = time.monotonic()

                for change in results:
                    action = change["action"].get_value()
                    filename = change["file_name"].get_value()
                    if isinstance(filename, bytes):
                        filename = filename.decode("utf-16-le").rstrip("\x00")
                    if not isinstance(action, int):
                        action = int(action)

                    filename = filename.replace("\\", "/")

                    # Suppress duplicate events from SMB burst-firing
                    key = (action, filename)
                    if now - last_seen.get(key, 0) < EVENT_COOLDOWN:
                        continue
                    last_seen[key] = now

                    replay_event(action, filename, local_path)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error("Watch error on %s\\%s: %s", share_name, remote_path, e)
            log.info("Reconnecting in %d seconds...", RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

MODNAME = "inotify-trigger"
MODVER = "1.0"
SERVICE_FILENAME = "smb_changenotify_to_inotify_translator.service"


def install_kernel_module():
    """Build and install the inotify_trigger kernel module via DKMS."""
    script_dir = Path(__file__).resolve().parent
    mod_src = script_dir / "inotify_trigger"

    if not mod_src.exists():
        log.error("Kernel module source not found at: %s", mod_src)
        log.error("Copy the inotify_trigger/ directory next to this script.")
        sys.exit(1)

    log.info("Installing kernel module build dependencies...")
    subprocess.check_call(["apt-get", "update", "-qq"])
    subprocess.check_call([
        "apt-get", "install", "-y", "-qq",
        "dkms", "build-essential",
        f"linux-headers-{os.uname().release}",
    ])

    dkms_src = Path(f"/usr/src/{MODNAME}-{MODVER}")

    # Remove old DKMS entry if present
    ret = subprocess.run(
        ["dkms", "status", f"{MODNAME}/{MODVER}"],
        capture_output=True, text=True,
    )
    if MODNAME in ret.stdout:
        log.info("Removing old DKMS entry...")
        subprocess.call(["dkms", "remove", f"{MODNAME}/{MODVER}", "--all"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Copy source into DKMS tree
    if dkms_src.exists():
        import shutil
        shutil.rmtree(dkms_src)
    dkms_src.mkdir(parents=True)
    for fname in ("inotify_trigger.c", "Makefile", "dkms.conf"):
        src = mod_src / fname
        if src.exists():
            import shutil
            shutil.copy2(src, dkms_src / fname)

    log.info("Building kernel module via DKMS...")
    subprocess.check_call(["dkms", "add", "-m", MODNAME, "-v", MODVER])
    subprocess.check_call(["dkms", "build", "-m", MODNAME, "-v", MODVER])
    subprocess.check_call(["dkms", "install", "-m", MODNAME, "-v", MODVER])

    log.info("Loading kernel module...")
    subprocess.check_call(["modprobe", "inotify_trigger"])

    # Auto-load on boot
    autoload = Path("/etc/modules-load.d/inotify_trigger.conf")
    autoload.write_text("inotify_trigger\n")

    if os.path.exists(INOTIFY_TRIGGER):
        log.info("Kernel module installed and loaded: %s", INOTIFY_TRIGGER)
    else:
        log.error("Module loaded but %s not found — something went wrong", INOTIFY_TRIGGER)
        sys.exit(1)


def uninstall_kernel_module():
    """Unload and remove the inotify_trigger kernel module."""
    # Unload
    subprocess.call(["rmmod", "inotify_trigger"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Kernel module unloaded.")

    # Remove DKMS entry
    ret = subprocess.run(
        ["dkms", "status", f"{MODNAME}/{MODVER}"],
        capture_output=True, text=True,
    )
    if MODNAME in ret.stdout:
        subprocess.call(["dkms", "remove", f"{MODNAME}/{MODVER}", "--all"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("DKMS entry removed.")

    # Remove source
    dkms_src = Path(f"/usr/src/{MODNAME}-{MODVER}")
    if dkms_src.exists():
        import shutil
        shutil.rmtree(dkms_src)

    # Remove auto-load
    autoload = Path("/etc/modules-load.d/inotify_trigger.conf")
    if autoload.exists():
        autoload.unlink()
    log.info("Kernel module auto-load removed.")


def install_all():
    """Full install: pip deps, kernel module, systemd service."""
    log.info("=== Full install ===")

    # 1. Python deps
    log.info("--- Python dependencies ---")
    _pip_install("smbprotocol")

    # 2. Kernel module
    log.info("--- Kernel module ---")
    if os.path.exists(INOTIFY_TRIGGER):
        log.info("Kernel module already loaded, skipping. (Use --reinstall-module to force.)")
    else:
        install_kernel_module()

    # 3. Systemd service
    log.info("--- Systemd service ---")
    script_path = Path(__file__).resolve()
    service_content = f"""\
[Unit]
Description=SMB ChangeNotify to inotify Translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={sys.executable} {script_path}
WorkingDirectory={script_path.parent}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    systemd_path = Path("/etc/systemd/system") / SERVICE_FILENAME
    with open(systemd_path, "w") as f:
        f.write(service_content)
    log.info("Service file written: %s", systemd_path)

    subprocess.check_call(["systemctl", "daemon-reload"])
    subprocess.check_call(["systemctl", "enable", "--now", SERVICE_FILENAME])
    log.info("Service enabled and started.")

    log.info("=== Install complete ===")


def uninstall_all():
    """Full uninstall: stop service, remove service, unload kernel module."""
    log.info("=== Full uninstall ===")

    # 1. Stop and disable service
    log.info("--- Systemd service ---")
    subprocess.call(["systemctl", "stop", SERVICE_FILENAME],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["systemctl", "disable", SERVICE_FILENAME],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    systemd_path = Path("/etc/systemd/system") / SERVICE_FILENAME
    if systemd_path.exists():
        systemd_path.unlink()
    subprocess.call(["systemctl", "daemon-reload"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Service stopped, disabled, and removed.")

    # 2. Kernel module
    log.info("--- Kernel module ---")
    uninstall_kernel_module()

    log.info("=== Uninstall complete ===")
    log.info("Python packages (smbprotocol) left in place — remove manually if desired.")


def test_connection():
    """Connect to each share and list the root directory contents, then exit."""
    config = load_config()
    server = config["smb_server"]
    port = config.get("smb_port", 445)
    username = config["smb_username"]
    password = config["smb_password"]

    for watch in config["watches"]:
        share_name = watch["share"]
        remote_path = watch["remote_path"]
        conn = None
        try:
            conn, session, tree = connect_to_share(server, port, username, password, share_name)
            dir_handle = open_directory(tree, remote_path)

            from smbprotocol.file_info import FileInformationClass
            entries = dir_handle.query_directory(
                "*",
                FileInformationClass.FILE_DIRECTORY_INFORMATION,
            )

            label = f"\\\\{server}\\{share_name}\\{remote_path}" if remote_path else f"\\\\{server}\\{share_name}"
            print(f"\n{label}:")
            for entry in entries:
                name = entry["file_name"].get_value()
                if isinstance(name, bytes):
                    name = name.decode("utf-16-le").rstrip("\x00")
                if name in (".", ".."):
                    continue
                attrs = entry["file_attributes"].get_value()
                is_dir = bool(attrs & FileAttributes.FILE_ATTRIBUTE_DIRECTORY)
                prefix = "[DIR] " if is_dir else "      "
                print(f"  {prefix}{name}")

        except Exception as e:
            log.error("Test failed for %s\\%s: %s", share_name, remote_path, e)
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass


def main():
    if "--install" in sys.argv:
        install_all()
        sys.exit(0)

    if "--uninstall" in sys.argv:
        uninstall_all()
        sys.exit(0)

    if "--reinstall-module" in sys.argv:
        install_kernel_module()
        sys.exit(0)

    if "--test" in sys.argv:
        test_connection()
        sys.exit(0)

    # Verify kernel module is loaded
    if not os.path.exists(INOTIFY_TRIGGER):
        print(f"ERROR: {INOTIFY_TRIGGER} not found.")
        print("The inotify_trigger kernel module is not loaded.")
        print("Run: python3 {0} --install".format(sys.argv[0]))
        sys.exit(1)

    config = load_config()

    server = config["smb_server"]
    port = config.get("smb_port", 445)
    username = config["smb_username"]
    password = config["smb_password"]
    watches = config["watches"]

    log.info("SMB ChangeNotify to inotify Translator")
    log.info("Server: %s:%d", server, port)
    log.info("Watches: %d", len(watches))
    log.info("Kernel module: %s", INOTIFY_TRIGGER)

    threads = []
    for watch in watches:
        t = threading.Thread(
            target=watch_loop,
            args=(
                server,
                port,
                username,
                password,
                watch["share"],
                watch["remote_path"],
                watch["local_path"],
            ),
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(
            "  -> %s\\%s -> %s",
            watch["share"],
            watch["remote_path"],
            watch["local_path"],
        )

    log.info("All watchers started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")

    log.info("Done.")


if __name__ == "__main__":
    main()
