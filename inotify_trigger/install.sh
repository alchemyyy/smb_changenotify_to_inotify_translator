#!/bin/bash
# Install the inotify_trigger kernel module via DKMS.
# Survives kernel updates automatically.
set -e

MODNAME="inotify-trigger"
MODVER="1.0"
SRCDIR="/usr/src/${MODNAME}-${MODVER}"

echo "=== inotify_trigger kernel module installer ==="

# Dependencies
apt-get update -qq
apt-get install -y -qq dkms build-essential linux-headers-"$(uname -r)"

# Remove old DKMS entry if present
dkms status "${MODNAME}/${MODVER}" 2>/dev/null | grep -q "${MODNAME}" && \
    dkms remove "${MODNAME}/${MODVER}" --all 2>/dev/null || true

# Copy source
rm -rf "${SRCDIR}"
mkdir -p "${SRCDIR}"
cp inotify_trigger.c Makefile dkms.conf "${SRCDIR}/"

# Build and install via DKMS
dkms add    -m "${MODNAME}" -v "${MODVER}"
dkms build  -m "${MODNAME}" -v "${MODVER}"
dkms install -m "${MODNAME}" -v "${MODVER}"

# Load now
modprobe inotify_trigger

# Auto-load on boot
echo "inotify_trigger" > /etc/modules-load.d/inotify_trigger.conf

echo ""
echo "=== Done ==="
echo "  /proc/inotify_trigger is live."
echo "  Module will auto-load on boot and rebuild on kernel updates."
echo ""
echo "Test it:"
echo "  inotifywait -m /tmp &"
echo "  echo '0x4 /tmp' > /proc/inotify_trigger"
echo "  # You should see an ATTRIB event on /tmp"
