#!/usr/bin/env python3
"""
Installer for SMB ChangeNotify to inotify Translator

Handles:
  - Building the C++ binary (libsmbclient-dev)
  - Kernel module (inotify_trigger) build/install via DKMS
  - Systemd service setup
  - SMB connection testing

Usage:
  python3 install.py --install           # Full install
  python3 install.py --uninstall         # Full uninstall
  python3 install.py --reinstall-module  # Rebuild kernel module only
  python3 install.py --build             # Build C++ binary only
  python3 install.py --test              # Test SMB connections
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("install")

SCRIPT_DIR = Path(__file__).resolve().parent
BINARY_NAME = "smb_changenotify_to_inotify_translator"
BINARY_PATH = SCRIPT_DIR / BINARY_NAME
CPP_SOURCE = SCRIPT_DIR / f"{BINARY_NAME}.cpp"
CONFIG_NAME = f"{BINARY_NAME}_config.json"
INOTIFY_TRIGGER = "/proc/inotify_trigger"
MODNAME = "inotify-trigger"
MODVER = "1.0"
SERVICE_FILENAME = f"{BINARY_NAME}.service"

DEFAULT_CONFIG = {
    "servers": [
        {
            "smb_server": "192.168.1.50",
            "smb_port": 445,
            "smb_username": "mediauser",
            "smb_password": "secret123",
            "watches": [
                {
                    "share": "media",
                    "remote_path": "Video",
                    "local_path": "/media/video",
                },
                {
                    "share": "media",
                    "remote_path": "Music",
                    "local_path": "/media/music",
                },
            ],
        },
        {
            "smb_server": "192.168.1.51",
            "smb_port": 445,
            "smb_username": "backupuser",
            "smb_password": "hunter2",
            "watches": [
                {
                    "share": "backups",
                    "remote_path": "Photos",
                    "local_path": "/media/photos",
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kwargs):
    """Run a shell command, logging it first."""
    if isinstance(cmd, list):
        log.info("Running: %s", " ".join(cmd))
    else:
        log.info("Running: %s", cmd)
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True if capture else None,
        **kwargs,
    )


def load_config():
    """Load config JSON, generating a default if missing."""
    config_path = SCRIPT_DIR / CONFIG_NAME
    if not config_path.exists():
        with open(config_path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        log.info("Generated default config at: %s", config_path)
        log.info("Edit it with your settings, then run again.")
        sys.exit(0)
    with open(config_path, "r") as f:
        return json.load(f)


def _ensure_pip():
    """Make sure pip is available, installing it via apt if needed."""
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.info("pip not found — installing via apt...")
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "pip"])


def _pip_install(package):
    """Install a pip package, using --break-system-packages for Debian compat."""
    _ensure_pip()
    run([sys.executable, "-m", "pip", "install", "--break-system-packages", package])


def _ensure_smbprotocol():
    """Import smbprotocol, auto-installing it if missing."""
    try:
        import smbprotocol  # noqa: F401
    except ImportError:
        log.info("'smbprotocol' not found — installing automatically...")
        try:
            _pip_install("smbprotocol")
        except Exception as e:
            log.error("Failed to install smbprotocol: %s", e)
            log.error("Try manually: pip install smbprotocol")
            sys.exit(1)
        try:
            import smbprotocol  # noqa: F401
        except ImportError:
            log.error("smbprotocol installed but still cannot be imported.")
            sys.exit(1)
        log.info("'smbprotocol' installed successfully.")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_binary(debug=False):
    """Compile the C++ binary."""
    if not CPP_SOURCE.exists():
        log.error("C++ source not found at: %s", CPP_SOURCE)
        sys.exit(1)

    log.info("--- Installing build dependencies ---")
    run(["apt-get", "update", "-qq"])
    run(["apt-get", "install", "-y", "-qq",
         "g++", "libsmbclient-dev"])

    log.info("--- Compiling %s ---", BINARY_NAME)
    cmd = [
        "g++", "-std=gnu++17", "-Wall",
        "-I/usr/include/samba-4.0",
        "-o", str(BINARY_PATH),
        str(CPP_SOURCE),
        "-lsmbclient",
    ]
    if debug:
        cmd.insert(3, "-O0")
        cmd.insert(4, "-DDEBUG")
    else:
        cmd.insert(3, "-O2")

    run(cmd)
    os.chmod(BINARY_PATH, 0o755)
    log.info("Binary built: %s", BINARY_PATH)


# ---------------------------------------------------------------------------
# Kernel module
# ---------------------------------------------------------------------------

def install_kernel_module():
    """Build and install the inotify_trigger kernel module via DKMS."""
    mod_src = SCRIPT_DIR / "inotify_trigger"

    if not mod_src.exists():
        log.error("Kernel module source not found at: %s", mod_src)
        log.error("Copy the inotify_trigger/ directory next to this script.")
        sys.exit(1)

    log.info("Installing kernel module build dependencies...")
    run(["apt-get", "update", "-qq"])
    run([
        "apt-get", "install", "-y", "-qq",
        "dkms", "build-essential",
        f"linux-headers-{os.uname().release}",
    ])

    dkms_src = Path(f"/usr/src/{MODNAME}-{MODVER}")

    # Remove old DKMS entry if present
    ret = run(["dkms", "status", f"{MODNAME}/{MODVER}"],
              check=False, capture=True)
    if MODNAME in ret.stdout:
        log.info("Removing old DKMS entry...")
        run(["dkms", "remove", f"{MODNAME}/{MODVER}", "--all"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Copy source into DKMS tree
    if dkms_src.exists():
        shutil.rmtree(dkms_src)
    dkms_src.mkdir(parents=True)
    for fname in ("inotify_trigger.c", "Makefile", "dkms.conf"):
        src = mod_src / fname
        if src.exists():
            shutil.copy2(src, dkms_src / fname)

    log.info("Building kernel module via DKMS...")
    run(["dkms", "add", "-m", MODNAME, "-v", MODVER])
    run(["dkms", "build", "-m", MODNAME, "-v", MODVER])
    run(["dkms", "install", "-m", MODNAME, "-v", MODVER])

    log.info("Loading kernel module...")
    run(["modprobe", "inotify_trigger"])

    # Auto-load on boot
    Path("/etc/modules-load.d/inotify_trigger.conf").write_text(
        "inotify_trigger\n"
    )

    if os.path.exists(INOTIFY_TRIGGER):
        log.info("Kernel module installed and loaded: %s", INOTIFY_TRIGGER)
    else:
        log.error("Module loaded but %s not found — something went wrong",
                  INOTIFY_TRIGGER)
        sys.exit(1)


def uninstall_kernel_module():
    """Unload and remove the inotify_trigger kernel module."""
    run(["rmmod", "inotify_trigger"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Kernel module unloaded.")

    ret = run(["dkms", "status", f"{MODNAME}/{MODVER}"],
              check=False, capture=True)
    if MODNAME in ret.stdout:
        run(["dkms", "remove", f"{MODNAME}/{MODVER}", "--all"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("DKMS entry removed.")

    dkms_src = Path(f"/usr/src/{MODNAME}-{MODVER}")
    if dkms_src.exists():
        shutil.rmtree(dkms_src)

    autoload = Path("/etc/modules-load.d/inotify_trigger.conf")
    if autoload.exists():
        autoload.unlink()
    log.info("Kernel module auto-load removed.")


# ---------------------------------------------------------------------------
# Full install / uninstall
# ---------------------------------------------------------------------------

def install_all():
    """Full install: build binary, kernel module, systemd service."""
    log.info("=== Full install ===")

    # 1. Build binary
    log.info("--- C++ binary ---")
    build_binary()

    # 2. Kernel module
    log.info("--- Kernel module ---")
    if os.path.exists(INOTIFY_TRIGGER):
        log.info("Kernel module already loaded, skipping. "
                 "(Use --reinstall-module to force.)")
    else:
        install_kernel_module()

    # 3. Systemd service
    log.info("--- Systemd service ---")
    if not BINARY_PATH.exists():
        log.error("Binary not found at %s — build failed?", BINARY_PATH)
        sys.exit(1)

    service_content = f"""\
[Unit]
Description=SMB ChangeNotify to inotify Translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={BINARY_PATH}
WorkingDirectory={SCRIPT_DIR}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    systemd_path = Path("/etc/systemd/system") / SERVICE_FILENAME
    with open(systemd_path, "w") as f:
        f.write(service_content)
    log.info("Service file written: %s", systemd_path)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", SERVICE_FILENAME])
    log.info("Service enabled and started.")

    # 4. Generate config if missing
    config_path = SCRIPT_DIR / CONFIG_NAME
    if not config_path.exists():
        with open(config_path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        log.info("Generated default config at: %s", config_path)
        log.info("Edit it with your settings, then restart the service.")

    log.info("=== Install complete ===")


def uninstall_all():
    """Full uninstall: stop service, remove service, unload kernel module."""
    log.info("=== Full uninstall ===")

    # 1. Stop and disable service
    log.info("--- Systemd service ---")
    run(["systemctl", "stop", SERVICE_FILENAME], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["systemctl", "disable", SERVICE_FILENAME], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    systemd_path = Path("/etc/systemd/system") / SERVICE_FILENAME
    if systemd_path.exists():
        systemd_path.unlink()
    run(["systemctl", "daemon-reload"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Service stopped, disabled, and removed.")

    # 2. Kernel module
    log.info("--- Kernel module ---")
    uninstall_kernel_module()

    log.info("=== Uninstall complete ===")
    log.info("Binary and config left in place — remove manually if desired.")


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------

def test_connection():
    """Connect to each configured share and list its root directory."""
    _ensure_smbprotocol()

    import uuid
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
            FILE_LIST_DIRECTORY = 0x00000001
    from smbprotocol.session import Session
    from smbprotocol.tree import TreeConnect

    # Silence smbprotocol noise
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)

    config = load_config()

    for srv in config["servers"]:
        server = srv["smb_server"]
        port = srv.get("smb_port", 445)
        username = srv["smb_username"]
        password = srv["smb_password"]

        for watch in srv["watches"]:
            share_name = watch["share"]
            remote_path = watch["remote_path"]
            conn = None
            try:
                conn = Connection(uuid.uuid4(), server, port)
                conn.connect()
                session = Session(conn, username, password)
                session.connect()
                tree = TreeConnect(session, r"\\%s\%s" % (server, share_name))
                tree.connect()

                dir_open = Open(tree, remote_path)
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

                from smbprotocol.file_info import FileInformationClass
                entries = dir_open.query_directory(
                    "*",
                    FileInformationClass.FILE_DIRECTORY_INFORMATION,
                )

                label = (f"\\\\{server}\\{share_name}\\{remote_path}"
                         if remote_path
                         else f"\\\\{server}\\{share_name}")
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
                log.error("Test failed for %s\\%s: %s",
                          share_name, remote_path, e)
            finally:
                if conn:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if "--install" in sys.argv:
        install_all()
    elif "--uninstall" in sys.argv:
        uninstall_all()
    elif "--reinstall-module" in sys.argv:
        install_kernel_module()
    elif "--build" in sys.argv:
        debug = "--debug" in sys.argv
        build_binary(debug=debug)
    elif "--test" in sys.argv:
        test_connection()
    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
