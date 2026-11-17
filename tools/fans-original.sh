#!/bin/bash
# Puts the Performance fan curves back to what they were before the max-fan test (2026-11-16).
asusctl fan-curve --mod-profile performance --fan cpu --data "40c:20,58c:35,62c:53,66c:76,70c:109,74c:147,78c:170,88c:216"
asusctl fan-curve --mod-profile performance --fan gpu --data "40c:33,58c:38,62c:68,66c:73,70c:104,74c:130,78c:153,88c:178"
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,58c:58,62c:71,66c:84,70c:94,74c:104,78c:112,88c:112"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
