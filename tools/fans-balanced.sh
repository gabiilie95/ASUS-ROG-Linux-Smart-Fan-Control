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
asusctl fan-curve --mod-profile performance --fan cpu --data "40c:20,58c:35,62c:53,66c:90,70c:150,72c:204,80c:204,88c:255"
asusctl fan-curve --mod-profile performance --fan gpu --data "40c:33,58c:38,62c:68,66c:100,70c:153,72c:204,82c:204,86c:255"
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,52c:58,58c:153,62c:178,70c:178,72c:204,80c:204,86c:255"
asusctl fan-curve --mod-profile performance --enable-fan-curves true
