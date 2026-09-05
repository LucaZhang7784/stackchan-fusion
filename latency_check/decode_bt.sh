#!/bin/bash
A2L=/opt/esp/tools/xtensa-esp-elf/esp-14.2.0_20251107/xtensa-esp-elf/bin/xtensa-esp32s3-elf-addr2line
"$A2L" -pfiaC -e /diag.elf \
  0x4038d1c2 0x4038cc89 0x4037a2af 0x4037a2c9 0x40379eb8 \
  0x4038e021 0x42175b9d 0x4200cc52 0x4200e8f1 0x4200ea39
