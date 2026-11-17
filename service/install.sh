#!/bin/bash
# Install from a trusted checkout. The invoking desktop user owns only the mode file.
set -euo pipefail
src=$(cd -- "$(dirname -- "$0")" && pwd -P)
app=/usr/local/lib/rog-fand
etc=/etc/rog-fand
state=/var/lib/rog-fand
unit=/etc/systemd/system/rog-fand.service
tmp=
cleanup() {
    if [[ -n $tmp ]]; then rm -f -- "$tmp"; fi
}
trap cleanup EXIT

copy_atomic() {
    tmp=$(mktemp -- "$(dirname -- "$2")/.rog-fand.XXXXXXXX")
    install -o root -g root -m 0644 -- "$1" "$tmp"
    mv -fT -- "$tmp" "$2"
    tmp=
}

case "${1:-}" in
    status)
        systemctl --no-pager status rog-fand.service
        ;;
    install|undo)
        if (( EUID != 0 )); then
            echo "Run install or undo as root." >&2
            exit 1
        fi
        if [[ $1 == install ]]; then
            install -d -o root -g root -m 0755 -- "$app" "$etc"
            install -d -o root -g root -m 0700 -- "$state"
            if [[ -f $unit ]]; then systemctl stop rog-fand.service; fi
            for file in rog_fand.py rog_sensors.py config.example.json; do
                copy_atomic "$src/$file" "$app/$file"
            done
            if [[ ! -e $etc/config.json ]]; then
                tmp=$(mktemp -- "$etc/.config.XXXXXXXX")
                /usr/bin/python3 -I - "$app/config.example.json" > "$tmp" <<'PY'
import json, os, pwd, sys
with open(sys.argv[1]) as src:
    cfg = json.load(src)
# sudo sets SUDO_USER; pkexec sets PKEXEC_UID. Either names the desktop user who controls the mode.
user = os.environ.get("SUDO_USER")
if not user and os.environ.get("PKEXEC_UID", "").isdigit():
    user = pwd.getpwuid(int(os.environ["PKEXEC_UID"])).pw_name
if user and pwd.getpwnam(user).pw_uid != 0:
    cfg["control_user"] = user
else:
    print("note: no desktop user found (run with sudo or pkexec); the panel cannot change the mode",
          file=sys.stderr)
print(json.dumps(cfg, indent=2))
PY
                mv -fT -- "$tmp" "$etc/config.json"
                tmp=
            fi
            copy_atomic "$src/rog-fand.service" "$unit"
            systemctl daemon-reload
            systemctl enable --now rog-fand.service
        else
            if [[ -f $unit ]]; then
                systemctl stop rog-fand.service
            fi
            # Retry a stop-time restore before removing the only copy of its base curve.
            if [[ -f $state/restore.json ]]; then
                /usr/bin/python3 -IB "$app/rog_fand.py" --restore || {
                    echo "Restore pending. Select Performance and repeat undo." >&2
                    exit 1
                }
            fi
            if [[ -f $unit ]]; then systemctl disable rog-fand.service; fi
            rm -f -- "$unit" "$app/rog_fand.py" "$app/rog_sensors.py" "$app/config.example.json"
            rm -f -- "$etc/config.json" "$etc/model.json" "$state/state.json"
            rm -f -- /var/log/rog-fand/*.csv /run/rog-fand/mode /run/rog-fand/status.json
            for dir in "$app" "$etc" "$state" /var/log/rog-fand /run/rog-fand; do
                if [[ -d $dir ]]; then rmdir -- "$dir"; fi
            done
            systemctl daemon-reload
        fi
        ;;
    *)
        echo "Usage: $0 install|undo|status" >&2
        exit 2
        ;;
esac
