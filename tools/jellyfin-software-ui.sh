#!/bin/bash
# Jellyfin Desktop (flatpak): draw the web interface in software so only the video uses
# graphics memory (470 MB instead of 2.7-4.3 GB on 2026-11-16). Takes effect on Jellyfin's next start.
#   tools/jellyfin-software-ui.sh on | off | status
APP=org.jellyfin.JellyfinDesktop
case "${1:-status}" in
  on)  flatpak override --user --env=QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu "$APP" && echo "on (restart Jellyfin to apply)" ;;
  off) flatpak override --user --unset-env=QTWEBENGINE_CHROMIUM_FLAGS "$APP" && echo "off (restart Jellyfin to apply)" ;;
  status) if flatpak override --user --show "$APP" 2>/dev/null | grep -q "QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu"; then echo on; else echo off; fi ;;
  *) echo "usage: $0 on|off|status"; exit 1 ;;
esac
