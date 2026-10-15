#!/bin/sh
# Installed on the Steam Deck by `deploy.ps1 -Setup` as /etc/decky-dualsense-wired/deploy.sh
# (owned by root) and allowed for the "deck" user via sudo without a password.
# Updates the plugin with the contents of /tmp/decky-dualsense-wired.tar.
#
# Decky's live reload restarts the plugin on file changes. main.py is copied last,
# so the final reload sees a complete plugin. `systemctl restart plugin_loader` is
# avoided on purpose: the unit uses KillMode=process and leaves the old loader running.
set -eu

NAME=decky-dualsense-wired
TAR=/tmp/$NAME.tar
DIR=/home/deck/homebrew/plugins/$NAME
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

[ -f "$TAR" ] && [ ! -L "$TAR" ] || { echo "missing $TAR" >&2; exit 1; }

tar --no-same-owner -xf "$TAR" -C "$STAGE"
rm -f "$TAR"
chown -R root:root "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"

mkdir -p "$DIR"
# Drop files that are no longer part of the plugin.
(cd "$DIR" && find . -type f) | while read -r f; do
    [ -e "$STAGE/$f" ] || rm -f "$DIR/$f"
done
mv "$STAGE/main.py" "$STAGE.main.py"
cp -a "$STAGE/." "$DIR/"
cp -a "$STAGE.main.py" "$DIR/main.py"
rm -f "$STAGE.main.py"
