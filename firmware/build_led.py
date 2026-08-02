# -*- coding: utf-8 -*-
"""build_led.py — 构建带 LED 状态反馈 + 防误唤醒补丁的帖子固件(docker 容器内构建)。"""
import os
import shutil
import subprocess
import sys

FWROOT = r"<PROJECT_DIR>"
PROJECT = os.path.join(FWROOT, "reference", "stackchan-xiaozhi-firmware")
TMP = os.path.join(FWROOT, "firmware", "build-led-tmp")
OUT = os.path.join(FWROOT, "firmware", "post-fw-v1.0.0-led")
CID = "stackchan_idf_build"


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    os.makedirs(TMP, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    mc = os.path.join(PROJECT, "managed_components")
    backup = os.path.join(TMP, "managed_components_backup")
    if os.path.isdir(mc):
        if not os.path.isdir(backup):
            shutil.copytree(mc, backup)
        shutil.rmtree(mc)

    src = os.path.join(TMP, "src")
    if os.path.isdir(src):
        shutil.rmtree(src)
    shutil.copytree(
        PROJECT, src,
        ignore=shutil.ignore_patterns(".git", "managed_components", "build"),
    )
    print("staged src:", src)

    run(["docker", "rm", "-f", CID])
    cmd = [
        "docker", "run", "-d", "--name", CID,
        "-v", f"{src}:/src:ro",
        "-v", f"{backup}:/stash:ro",
        "-v", f"{OUT}:/out",
        "-v", os.path.join(FWROOT, "firmware", "build_led_ci.sh") + ":/build_ci.sh:ro",
        "espressif/idf:v5.5.2", "bash", "/build_ci.sh",
    ]
    r = run(cmd)
    print("docker run rc:", r.returncode, r.stdout.strip()[:200], r.stderr.strip()[:300])
    if r.returncode != 0:
        return 1

    w = run(["docker", "wait", CID])
    print("docker wait:", w.stdout.strip())
    logs = run(["docker", "logs", CID])
    with open(os.path.join(TMP, "build.log"), "w", encoding="utf-8") as f:
        f.write(logs.stdout + "\n" + logs.stderr)
    run(["docker", "rm", "-f", CID])

    if w.stdout.strip() != "0":
        print("BUILD FAILED - see", os.path.join(TMP, "build.log"))
        return 1

    for name in ("srmodels.bin", "generated_assets.bin"):
        dst = os.path.join(OUT, name)
        if not os.path.exists(dst):
            rel = os.path.join(FWROOT, "firmware", "post-fw-v1.0.0", name)
            if os.path.exists(rel):
                shutil.copy(rel, dst)
                print("fallback copy:", name)

    merge = [
        "python", "-m", "esptool", "--chip", "esp32s3", "merge_bin",
        "-o", os.path.join(OUT, "merged-binary.bin"),
        "--flash_mode", "dio", "--flash_size", "16MB", "--flash_freq", "80m",
        "0x0", os.path.join(OUT, "bootloader.bin"),
        "0x8000", os.path.join(OUT, "partition-table.bin"),
        "0xd000", os.path.join(OUT, "ota_data_initial.bin"),
        "0x10000", os.path.join(OUT, "srmodels.bin"),
        "0x410000", os.path.join(OUT, "xiaozhi.bin"),
        "0xa10000", os.path.join(OUT, "generated_assets.bin"),
    ]
    m = run(merge)
    print("merge rc:", m.returncode, m.stdout.strip()[-300:], m.stderr.strip()[-300:])
    print("OUTPUTS:")
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print(" ", f, os.path.getsize(p))
    return 0 if m.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
