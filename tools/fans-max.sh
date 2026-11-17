#!/bin/bash
# Performance profile: all three fans to 100 % from 70 C upward.
# Needed under full load (GPU ~150 W with the CPU uncapped): the middle fan alone left ~16 stalls/s.
asusctl fan-curve --mod-profile performance --fan cpu --data "40c:20,58c:35,62c:53,66c:150,70c:255,74c:255,78c:255,88c:255"
asusctl fan-curve --mod-profile performance --fan gpu --data "40c:33,58c:38,62c:68,66c:150,70c:255,74c:255,78c:255,88c:255"
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,58c:58,62c:71,66c:150,70c:255,74c:255,78c:255,88c:255"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
