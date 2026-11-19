#!/bin/bash
# Install from a trusted checkout. The invoking desktop user owns the mode control directory.
set -euo pipefail
src=$(cd -- "$(dirname -- "$0")" && pwd -P)
app=/usr/local/lib/rog-fand
etc=/etc/rog-fand
state=/var/lib/rog-fand
unit=/etc/systemd/system/rog-fand.service
launcher=/usr/local/share/applications/rog-panel-qt.desktop
panel_files=(ui/{rog-panel-qt,rogcore,fan_auto,fan_calibration,fan_tray,panel_settings}.py
             tools/{fan-calibrate,fan_analysis,stall-counter,autotune,gpuload}.py tools/loadgen.c
             tools/fans-{balanced,max,mid-max,original,ramped}.sh
             tools/jellyfin-software-ui.sh root-tests/{setup-cool-performance,usb-drop-count}.sh
             root-tests/cpu_cap_profiles.py)
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
        desk_user=${SUDO_USER:-}
        if [[ -z $desk_user && ${PKEXEC_UID:-} =~ ^[0-9]+$ ]]; then
            desk_user=$(getent passwd "$PKEXEC_UID" | cut -d: -f1)
        fi
        desk_home=
        if [[ -n $desk_user ]]; then desk_home=$(getent passwd "$desk_user" | cut -d: -f6); fi
        if [[ $1 == install ]]; then
            install -d -o root -g root -m 0755 -- "$app" "$etc"
            install -d -o root -g root -m 0700 -- "$state"
            if [[ -f $unit ]]; then systemctl stop rog-fand.service; fi
            install -d -o root -g root -m 0755 -- "$app"/{ui,tools,root-tests} /usr/local/share/applications
            for file in "${panel_files[@]}"; do
                copy_atomic "$src/../$file" "$app/$file"
            done
            copy_atomic "$src/../ui/rog-panel-qt.desktop" "$launcher"
            # A per-user launcher with the same name takes priority over this one in the menu.
            if [[ -n $desk_home && -e $desk_home/.local/share/applications/rog-panel-qt.desktop ]]; then
                echo "note: $desk_home/.local/share/applications/rog-panel-qt.desktop hides the installed" \
                     "launcher in the menu; remove it to use the installed panel." >&2
            fi
            for file in rog_fand.py rog_sensors.py rog_update.py config.example.json; do
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
            # Last, so an interrupted install is offered again by the updater.
            copy_atomic "$src/../VERSION" "$app/VERSION"
        else
            if [[ -f $unit ]]; then
                systemctl stop rog-fand.service
            fi
            # Retry a stop-time restore before removing the only copy of a profile's own curves.
            if compgen -G "$state/restore*.json" > /dev/null; then
                /usr/bin/python3 -IB "$app/rog_fand.py" --restore || {
                    echo "Restore pending. Select the profile named above and repeat undo." >&2
                    exit 1
                }
            fi
            if [[ -f $unit ]]; then systemctl disable rog-fand.service; fi
            for file in "${panel_files[@]}"; do rm -f -- "$app/$file"; done
            rm -f -- "$launcher" "$unit" "$app/rog_fand.py" "$app/rog_sensors.py" "$app/rog_update.py" \
                  "$app/config.example.json" "$app/VERSION"
            rm -rf -- "$state/update"
            rm -f -- "$etc/config.json" "$etc/model.json" "$state/state.json"
            rm -f -- /var/log/rog-fand/*.csv /run/rog-fand/mode /run/rog-fand/status.json
            rm -f -- /run/rog-fand/control/mode
            if [[ -n $desk_home && -e $desk_home/.config/autostart/rog-panel-qt.desktop ]]; then
                echo "note: the panel still starts at login from $desk_home/.config/autostart/rog-panel-qt.desktop;" \
                     "remove it, or untick Start in the tray at login first." >&2
            fi
            for dir in "$app"/{ui,tools,root-tests} "$app" "$etc" "$state" /var/log/rog-fand /run/rog-fand/control /run/rog-fand; do
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
