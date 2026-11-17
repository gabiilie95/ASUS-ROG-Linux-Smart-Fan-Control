#!/bin/bash
# Rebuilds systemdgenie from KDE's source and installs it for the current user (~/.local).
# Used on 2026-11-16 because Fedora 45, Flathub and the COPR have no package (built v0.100.0).
# Needs once:  sudo dnf install -y kf6-kauth-devel kf6-kcoreaddons-devel kf6-kcrash-devel \
#              kf6-ki18n-devel kf6-ktexteditor-devel kf6-kio-devel systemd-devel
# The root helper and polkit policy are NOT installed (they need system paths), so saving edited
# unit files under /etc from the app may not work.
set -e
W=$(mktemp -d)
git clone --depth 1 https://invent.kde.org/system/systemdgenie.git "$W/src"
cmake -S "$W/src" -B "$W/build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$HOME/.local" -DBUILD_TESTING=OFF
ninja -C "$W/build"
DESTDIR="$W/stage" cmake --install "$W/build"
S="$W/stage$HOME/.local"
install -Dm755 "$S/bin/systemdgenie" "$HOME/.local/bin/systemdgenie"
install -Dm644 "$S/share/applications/org.kde.systemdgenie.desktop" "$HOME/.local/share/applications/org.kde.systemdgenie.desktop"
install -Dm644 "$S/share/metainfo/org.kde.systemdgenie.metainfo.xml" "$HOME/.local/share/metainfo/org.kde.systemdgenie.metainfo.xml"
mkdir -p "$HOME/.local/share/locale" && cp -r "$S/share/locale/." "$HOME/.local/share/locale/"
( cd "$S" && find bin share/applications share/metainfo share/locale -type f | sed "s#^#$HOME/.local/#" ) > "$HOME/.local/share/systemdgenie-installed-files.txt"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
"$HOME/.local/bin/systemdgenie" --version
echo "Build directory left at $W"
