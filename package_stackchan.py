# -*- coding: utf-8 -*-
"""把当前方案打包到 fusion.firmware.0731/package-stackchan(排除密钥/日志/状态)。"""
import os
import shutil
import zipfile

ROOT = r"<PROJECT_ROOT>\fusion.firmware.0731"
PKG = os.path.join(ROOT, "package-stackchan")
FW_SRC = os.path.join(ROOT, "firmware", "post-fw-v1.0.6-ttsbuf")

def main():
    fw_dst = os.path.join(PKG, "firmware")
    if os.path.isdir(fw_dst):
        shutil.rmtree(fw_dst)
    pc_dst = os.path.join(PKG, "pc")
    if os.path.isdir(pc_dst):
        shutil.rmtree(pc_dst)
    os.makedirs(fw_dst)
    for name in ("merged-binary.bin", "bootloader.bin", "partition-table.bin",
                 "ota_data_initial.bin", "srmodels.bin", "xiaozhi.bin", "generated_assets.bin"):
        shutil.copy(os.path.join(FW_SRC, name), os.path.join(fw_dst, name))

    def copy_dir(src, dst, keep):
        os.makedirs(dst, exist_ok=True)
        for name in keep:
            s = os.path.join(src, name)
            if os.path.exists(s):
                if os.path.isdir(s):
                    shutil.copytree(s, os.path.join(dst, name))
                else:
                    shutil.copy2(s, os.path.join(dst, name))

    copy_dir(os.path.join(ROOT, "xiaozhi-mcp"), os.path.join(PKG, "pc", "xiaozhi-mcp"),
             ["server.py", "mcp_pipe.py", "mcp_config.json", "run_bridge.ps1", "stop_bridge.ps1",
              "run_bridge_hidden.vbs"])
    copy_dir(os.path.join(ROOT, "gateway"), os.path.join(PKG, "pc", "gateway"),
             ["fusion_gateway.py", "agents_core.py", "requirements.txt",
              "run_gateway.ps1", "stop_gateway.ps1", "install_autostart.ps1",
              "watchdog_gateway.ps1", "fusion_tray.ps1"])
    copy_dir(os.path.join(ROOT, "agents"), os.path.join(PKG, "pc", "agents"),
             ["confirm_mcp.py", "claude_run.py", "claude_hook.py", "install_claude_hooks.ps1",
              "antigravity_hook.py", "codex_hook.py", "claude_visible_run.py"])
    # Docker / MCP Toolkit / 守护托盘
    copy_dir(os.path.join(ROOT, "docker"), os.path.join(PKG, "pc", "docker"),
             ["host-executor.py", "run_executor.ps1", "install_executor_task.ps1",
              "fusion-gateway.yaml", "mcp-toolkit-profile.json", "MCP-Toolkit接入说明.md"])
    copy_dir(os.path.join(ROOT, "gateway"), os.path.join(PKG, "pc", "gateway"),
             ["守护与托盘说明.md"])

    # 占位配置模板(密钥一律用占位符)
    env_ex = os.path.join(PKG, "pc", "xiaozhi-mcp", ".env.example")
    open(env_ex, "w", encoding="utf-8").write(
        "# 从 xiaozhi.me 控制台获取: 智能体 -> MCP 设置 -> 获取接入点\n"
        "MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE\n"
        "# CLAUDE_BIN=claude\n# CODEX_BIN=codex\n"
    )
    cfg_ex = os.path.join(PKG, "pc", "gateway", "config.json.example")
    open(cfg_ex, "w", encoding="utf-8").write(
        '{\n'
        '  "ota_url": "https://YOUR_FUNNEL_DOMAIN.ts.net/xiaozhi/ota/",\n'
        '  "robot_mac": "AA:BB:CC:DD:EE:FF",\n'
        '  "auth_token": "CHANGE_ME",\n'
        '  "allow_codex": true,\n  "allow_claude": true,\n'
        '  "codex_cli": "codex",\n  "claude_cli": "claude",\n'
        '  "max_output_chars": 4000,\n  "max_timeout_s": 600,\n'
        '  "http_host": "0.0.0.0",\n  "http_port": 8010,\n'
        '  "push_api_url": "http://127.0.0.1:8003/api/push",\n'
        '  "push_secret": "CHANGE_ME",\n  "push_interval_s": 5\n}\n'
    )

    # README 主文档
    shutil.copy2(os.path.join(ROOT, "README.md"), os.path.join(PKG, "README.md"))
    for name in ("prompt-阿松-v3.md", "prompt-阿松-v2.md", "MEMORY.md"):
        s = os.path.join(ROOT, name)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(PKG, name))

    # 生成 zip(方便分发)
    zip_path = os.path.join(ROOT, "package-stackchan.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, files in os.walk(PKG):
            for f in files:
                p = os.path.join(base, f)
                z.write(p, os.path.relpath(p, ROOT))

    print("PACKAGE OK ->", PKG)
    for base, dirs, files in os.walk(PKG):
        for f in files:
            p = os.path.join(base, f)
            print(" ", os.path.relpath(p, ROOT), os.path.getsize(p))
    print("ZIP ->", zip_path, os.path.getsize(zip_path))

if __name__ == "__main__":
    main()
