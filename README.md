# SMB ChangeNotify to inotify Translator

Bridges the gap between network filesystem change notifications and Linux inotify.

Uses an SMB share to listen for SMB2 CHANGE_NOTIFY events, then translates them into real inotify events via a custom kernel module. Applications like Navidrome, Jellyfin, and Plex see real-time updates without periodic rescanning.

This program is completely independent of the actual filesystem mount you use. You can keep running NFS on Linux for data access. All this requires is read-access-level SMB to the same data.

Example: If using TrueNAS, set up a read-only SMB share pointing to the same dataset as your existing NFS share.

## The problem

Linux inotify doesn't work on NFS or SMB mounts. When a file changes on the server, local applications watching the mount with inotify see nothing. Most media servers fall back to periodic rescanning, which is slow and wasteful. It is a fundamental limitation of how Linux network filesystems interact with the inotify subsystem. The kernel's VFS layer generates inotify events when local operations happen — but NFS/SMB operations happen on the server, so the local kernel never knows.

## Requirements

- Linux >= 5.14 (for `FSNOTIFY_EVENT_DENTRY` in the kernel module)
- Python 3
- Root access (for the kernel module and systemd service)
- DKMS, build-essential, and kernel headers (installed automatically by `--install`)
- An SMB/Samba share with at least read access to the directories you want to watch

## Install

1. Copy this repo to the target machine wherever you want it to live. The installer will set the service path from where the script is when it executes the install function.

2. Run the installer (this also generates the following example config file):
  a. The install does three things:
  b. Installs the `smbprotocol` Python package
  c. Builds and installs the `inotify_trigger` kernel module via DKMS (survives kernel updates)
  d. Creates and enables a systemd service

```bash
sudo python3 smb_changenotify_to_inotify_translator.py --install
```




3. Edit the generated config `smb_changenotify_to_inotify_translator_config.json`:

```json
{
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
                    "local_path": "/shares/video"
                },
                {
                    "share": "media",
                    "remote_path": "Music",
                    "local_path": "/shares/music"
                }
            ]
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
                    "local_path": "/shares/photos"
                }
            ]
        }
    ]
}
```

- `smb_server` / `smb_port` — the SMB server to connect to
- `smb_username` / `smb_password` — credentials (read-only access is sufficient)
- `share` — the SMB share name
- `remote_path` — subdirectory within the share to watch (SMB2 CHANGE_NOTIFY target)
- `local_path` — where the data is mounted locally (where inotify events are injected)

Multiple servers and multiple watches per server are supported.

4. Restart the service:

```bash
sudo systemctl restart smb_changenotify_to_inotify_translator
```

## Usage

```
--install             Full install (pip deps + kernel module + systemd service)
--uninstall           Full uninstall (stop service + remove module + cleanup)
--reinstall-module    Rebuild just the kernel module
--test                Test SMB connection (lists directory contents)
--debug               Enable debug logging (shows every event and inject payload)
```

Run directly with debug output:

```bash
sudo python3 smb_changenotify_to_inotify_translator.py --debug
```

## Verifying events

A verification script is included to confirm inotify events are arriving:

```bash
python3 verify_inotify.py
```

This watches the configured `local_path` directories with `inotifywait` and prints events as they arrive, tagged with the server and share they came from:

```
[mediauser@192.168.1.50\media] 14:23:01 CREATE /shares/music/Albums/NewAlbum
[mediauser@192.168.1.50\media] 14:23:01 MODIFY /shares/music/Albums/NewAlbum/song.flac
```

Make a change on the SMB server and you should see it appear.

## Uninstall

```bash
sudo python3 smb_changenotify_to_inotify_translator.py --uninstall
```

Stops the service, removes the systemd unit, unloads the kernel module, and removes the DKMS entry.

## Notes

- The SMB connection is completely independent from how you mount the share. You can mount via NFS and still get SMB change notifications — the script opens its own SMB session just for the notification subscription.
- The kernel module auto-rebuilds on kernel updates via DKMS.
- SMB2 CHANGE_NOTIFY watches are recursive — a single watch on a directory covers the entire subtree.
- Duplicate events from SMB burst-firing are suppressed with a 2-second cooldown per (action, path) pair.
- The correct inotify event type is injected (IN_CREATE, IN_DELETE, IN_MODIFY, IN_MOVED_FROM, IN_MOVED_TO) — not a generic IN_ATTRIB.
- Each change fires **two events** to cover different application expectations: (1) on the watch root with the full relative path as child (for Navidrome/Go fsnotify), and (2) on the immediate parent directory with just the filename (for Jellyfin/.NET FileSystemWatcher). Both are zero-cost fsnotify() calls — apps ignore events on watches they don't hold.

## Confirmed working with

- **Navidrome** — Uses `rjeczalik/notify` (Go inotify wrapper) with recursive watches and selective scanning. Receives the root-level event with the full relative path, so `DevSelectiveWatcher` can target the exact changed folder.
- **Jellyfin** — Uses .NET `FileSystemWatcher` with `IncludeSubdirectories = true`, which creates individual inotify watches on every subdirectory. Receives the parent-level event with just the filename — matching the exact pattern .NET expects, enabling targeted library item refresh instead of full rescans.
- **inotifywait** — Standard Linux inotify debugging tool. Events show the child name correctly.


## Modules

### 1. Kernel module (`inotify_trigger`)

A small GPL kernel module that exposes `/proc/inotify_trigger`. It calls the kernel's `fsnotify()` directly, generating real inotify events — indistinguishable from ones generated by actual filesystem operations — without touching the filesystem at all. No disk writes, no network writes, no feedback loops.

The module supports two formats:

```bash
# Self-event (fires on the inode itself):
echo "0x2 /media/music/song.flac" > /proc/inotify_trigger      # IN_MODIFY

# Dir + child (fires on a directory with a named child — preferred for NFS):
printf "0x100 /media/music\tAlbums/new.flac" > /proc/inotify_trigger  # IN_CREATE
```

The dir+child format is how real VFS events work internally — see [why dir+child mode exists](#why-dirchild-mode-exists) below.

### 2. Python translator script

Connects to SMB servers using the SMB2 CHANGE_NOTIFY protocol. When changes are detected, it writes the corresponding event to `/proc/inotify_trigger`. The kernel module fires `fsnotify()` and any application watching with inotify sees it immediately.

```
SMB server detects file change
        |
SMB2 CHANGE_NOTIFY fires
        |
Python script receives notification
        |
Fires TWO inotify events via /proc/inotify_trigger:
        |
        +-- (1) Root-level: watch root + full relative path as child
        |       -> Navidrome (Go fsnotify) picks this up
        |
        +-- (2) Parent-level: immediate parent dir + filename as child
                -> Jellyfin (.NET FileSystemWatcher) picks this up
        |
Kernel module calls fsnotify() for each — zero filesystem I/O
        |
Applications see the exact file that changed
```

Zero filesystem operations in the entire chain. The data mount (NFS, SMB, whatever) is never touched.

## The horrible journey: why it works this way

This section documents the evolution of the kernel module and why the "dir+child" approach exists. If you're just here to install it, skip to [Install](#install). If you want to understand the engineering decisions (or you're trying to solve a similar problem), read on.

### Attempt 1: Self-events on the file path

The obvious approach: resolve the changed file's path to an inode, call `fsnotify()` on it.

```c
kern_path("/shares/music/Albums/NewAlbum/song.flac", &path);
fsnotify(IN_CREATE, path.dentry, FSNOTIFY_EVENT_DENTRY, NULL, NULL, inode, 0);
```

**Problem on NFS:** `kern_path()` returns `-ENOENT` for newly created files because the NFS attribute cache hasn't been invalidated yet. The file exists on the server, but the local NFS client doesn't know about it. Even with aggressive cache settings (`acdirmax=1`, `acregmax=1`), there's a race window.

**Problem with deleted files:** Same issue in reverse — the file is already gone, `kern_path()` can't resolve it.

### Attempt 2: Two-phase VFS pattern (fsnotify_parent + fsnotify)

Mimicked exactly what the kernel's VFS helpers do internally:

```c
fsnotify_parent(dentry, mask, dentry, FSNOTIFY_EVENT_DENTRY);  // Phase 1: notify parent watchers
fsnotify(mask, dentry, FSNOTIFY_EVENT_DENTRY, NULL, NULL, inode, 0);  // Phase 2: self-event
```

**Problem on NFS:** `fsnotify_parent()` checks a flag called `DCACHE_FSNOTIFY_PARENT_WATCHED` on the dentry. On local filesystems, `inotify_add_watch()` sets this flag on child dentries when you watch a directory. On NFS, the dentries that `kern_path()` resolves may be **different dentry objects** than the ones inotify is watching (NFS inode aliasing). The flag isn't set, so `fsnotify_parent()` silently does nothing. Events never propagate to directory watchers.

This is the core of the NFS inotify problem. The kernel has two separate dentry trees for the same files — the one NFS uses for path resolution and the one inotify watches are attached to.

### Attempt 3: Fire on the watch root directory

Since we can't rely on `fsnotify_parent()` propagating events, fire directly on the root directory that applications are watching (e.g., `/shares/music`):

```c
kern_path("/shares/music", &path);  // Always exists, always in cache
fsnotify(IN_CREATE | FS_ISDIR, dentry, FSNOTIFY_EVENT_DENTRY, NULL, NULL, inode, 0);
```

**Result:** `inotifywait` sees the events! But they all look like:

```
/shares/music/ CREATE,ISDIR /
/shares/music/ CREATE,ISDIR /
/shares/music/ MODIFY,ISDIR /
```

Every event is a nameless self-event on the root directory. It says "something happened to `/shares/music/`" but not *what* happened or *where*.

**Problem with applications:** Navidrome uses `rjeczalik/notify` (Go library wrapping inotify) with `DevSelectiveWatcher = true` — it only scans the specific folder identified by the event path. A nameless event on the root causes it to scan only the root directory, missing all nested changes. Jellyfin uses .NET's `FileSystemWatcher` which needs `e.FullPath` to find the affected library item. With no child name, it can't identify what changed.

### Attempt 4: Dir + child name format (root-level)

The correct inotify semantic for "file X was created in directory Y" is:

```c
fsnotify(mask, dir_dentry, FSNOTIFY_EVENT_DENTRY, dir_inode, &child_qstr, NULL, 0);
```

This is exactly what `fsnotify_dirent()` / `fsnotify_create()` / `fsnotify_modify()` call internally. It delivers the event to inotify watchers on the directory with the child's name in the `inotify_event.name` field.

The first version fired on the **watch root** with the full relative path as child name:

```
Format: "<mask> <dir_path>\t<child_name>"
Example: "0x100 /shares/music\tAlbums/NewAlbum/song.flac"
```

This worked for Navidrome and inotifywait — they accept deep relative paths in the name field. But .NET's `FileSystemWatcher` (used by Jellyfin) creates individual inotify watches on **every subdirectory**. It expects events on the immediate parent directory with just the filename — a name containing slashes is non-standard and doesn't match any of its per-subdirectory watches.

### Attempt 5 (final): Fire both root-level and parent-level events

Switching to parent-only events fixed Jellyfin but broke Navidrome — Go's `fsnotify` expects the deep relative path from the root watch. The solution: **fire both**.

Each SMB change notification produces two `fsnotify()` calls:

```
(1) Root-level:   "0x100 /shares/music\tAlbums/NewAlbum/song.flac"
(2) Parent-level: "0x100 /shares/music/Albums/NewAlbum\tsong.flac"
```

- **Navidrome** (`rjeczalik/notify`): Picks up event (1) on its root watch, sees the full relative path, selectively scans the right folder.
- **Jellyfin** (.NET `FileSystemWatcher`): Picks up event (2) on its per-subdirectory watch for `/shares/music/Albums/NewAlbum`, sees `song.flac` — exactly what it expects.
- **inotifywait**: Shows both events.

Both calls are zero-cost (no filesystem I/O), and apps ignore events on watches they don't hold. If the parent directory can't be resolved (e.g., a brand new directory not yet in the NFS dentry cache), the parent-level event is silently skipped — the root-level event still covers it.

### Summary of the notification mechanism landscape

| Approach | Works on local FS? | Works on NFS? | Apps see child name? |
|----------|-------------------|---------------|---------------------|
| `fsnotify()` on file inode | Yes | No (ENOENT race) | No (self-event) |
| `fsnotify_parent()` + `fsnotify()` | Yes | No (DCACHE flag missing) | Depends |
| `fsnotify()` on root dir (self-event) | Yes | Yes | No |
| `fsnotify()` on root dir with deep child path | Yes | Yes | Yes (Navidrome) / No (Jellyfin) |
| `fsnotify()` on parent dir with basename | Yes | Yes | No (Navidrome) / Yes (Jellyfin) |
| **Both root-level + parent-level** | **Yes** | **Yes** | **Yes (all apps)** |

### Why not just use utime/touch/filesystem operations?

You could `touch` the local file to trigger a real inotify event. **Don't.**

- On NFS, `touch` writes an RPC to the server, which fires a CHANGE_NOTIFY, which the translator receives, which touches again... feedback loop.
- Even with deduplication, you're generating real filesystem I/O on every event — pointless network traffic and server load.
- `utime()` generates `IN_ATTRIB`, not `IN_CREATE`/`IN_DELETE`/`IN_MODIFY`. Applications may not react to `ATTRIB` the same way.

The kernel module approach has zero filesystem interaction. The event goes directly from `fsnotify()` to the inotify subsystem. Nothing touches NFS, nothing touches disk, nothing touches the network.
