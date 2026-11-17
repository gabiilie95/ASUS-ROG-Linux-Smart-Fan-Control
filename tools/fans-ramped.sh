#!/bin/bash
# Quietest Performance curve that stayed clean on 2026-11-16 (CPU cap 70 %, GPU ~135 W):
#   CPU fan    original curve (about 4300 rpm in game instead of 6300)
#   GPU fan    ramps with temperature, 100 % from 80 C
#   middle fan 100 % from 70 C  (6100 rpm was not enough: about 5 stalls/s)
# Always-safe louder alternative: tools/fans-max.sh
asusctl fan-curve --mod-profile performance --fan cpu --data "40c:20,58c:35,62c:53,66c:76,70c:109,74c:147,78c:170,88c:216"
asusctl fan-curve --mod-profile performance --fan gpu --data "40c:33,58c:38,62c:68,66c:90,70c:130,74c:170,77c:215,80c:255"
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,58c:58,62c:71,66c:150,70c:255,74c:255,78c:255,88c:255"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
