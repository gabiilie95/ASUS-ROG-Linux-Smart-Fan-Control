#!/bin/bash
# The fix found on 2026-11-16: Performance profile, middle fan to 100 % from 70 C upward.
# CPU and GPU fans keep their original curves. No root needed; asusd stores it.
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,58c:58,62c:71,66c:150,70c:255,74c:255,78c:255,88c:255"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
