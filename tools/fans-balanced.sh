#!/bin/bash
# Balanced Performance curve from the 2026-11-16 calibration: spread the airflow over all three fans
# so none needs its high-pitched top range, and go to full only above 80-88 C as a safety.
#
#   light load (CPU ~57-68 C, GPU ~54-66 C): middle ~70 %, CPU and GPU fans low   (calibrated clean)
#   heavy load (CPU ~66-77 C, GPU ~70-79 C): about 80 / 80 / 80 %                  (calibrated clean)
#   not enough: 70/70/70 under heavy load (1.4/s), middle 60 % at light load (7/s)
#
# Assumes the EC drives the CPU and middle fans from CPU temperature and the GPU fan from GPU
# temperature. Not yet validated as a whole curve. Undo: tools/fans-ramped.sh or tools/fans-original.sh
#
# 2026-11-17: quieter below 70 C. The middle fan was at 60-70 % (5200 rpm, whiny) from 58 C, which the
# desktop reaches at idle; it is now 31 / 39 / 51 % at 58 / 62 / 66 C, with the CPU fan a little higher.
# rog-fand's floors (middle 70 % from 50 W GPU) cover light and heavy load. Idle check: middle fan
# about 4000 rpm, 0 stalls, 0 USB drops, same temperatures. From 70 C the curve is unchanged.
asusctl fan-curve --mod-profile performance --fan cpu --data "40c:20,58c:50,62c:68,66c:100,70c:150,72c:204,80c:204,88c:255"
asusctl fan-curve --mod-profile performance --fan gpu --data "40c:33,58c:38,62c:68,66c:100,70c:153,72c:204,82c:204,86c:255"
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,52c:58,58c:80,62c:100,66c:130,70c:178,72c:204,86c:255"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
