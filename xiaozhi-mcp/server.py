"""xiaozhi.me 云智能体 MCP bridge (stdio 侧)。

配合 mcp_pipe.py 使用: xiaozhi.me broker 是 MCP *客户端*, 本程序是 MCP *服务端*,
通过 wss 接入点把本地工具暴露给云智能体的 LLM (与 stackchan-claude-bridge 同架构)。

遵循 xiaozhi MCP 注意事项:
- 返回值限制 ~1024 字节 -> 统一截到 900 字符
- 工具名/参数自解释 + 文档注释说明何时使用
- 长任务(Claude/Codex)必须异步: wss keepalive ~30s, 同步会超时被踢;
  立即返回口语回执, 结果写 outbox, 由 agent_result_check 取回
- stdio 传输数据, 不能用 print, 调试走 logger(stderr)
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows: stdio 管道强制 UTF-8, 否则 JSON 里的中文会乱码/中断
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
OUTBOX = ROOT / "outbox"
(OUTBOX / "done").mkdir(parents=True, exist_ok=True)

MAX_RETURN_CHARS = 900      # 文档限制 ~1024 字节, 留余量(中文 1 字 ~3 字节)
ASYNC_TIMEOUT_SECS = 300    # 后台 claude/codex 最长执行
QUICK_TIMEOUT_SECS = 25     # 同步工具必须在 keepalive 30s 内返回

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("xiaozhi-bridge")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
EXEC_CWD = os.environ.get("BRIDGE_WORKDIR", str(ROOT))


def _no_window() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _resolve(name: str) -> str:
    return shutil.which(name) or name


def _run(argv: list[str], timeout: int, cwd: str) -> dict:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL,
            creationflags=_no_window(),
        )
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "out": "", "err": f"exec timeout(>{timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "rc": -2, "out": "", "err": str(e)}


def _run_agent(cli_name: str, argv: list[str], timeout: int) -> dict:
    """Windows 上 .cmd/.bat 走 cmd /c, 其余直接 CreateProcess(同 fusion_gateway)。"""
    cli = _resolve(cli_name)
    clean = [str(a).replace('"', "'") for a in argv]
    if cli.lower().endswith((".cmd", ".bat")):
        full = ["cmd", "/c", cli] + clean
    else:
        full = [cli] + clean
    return _run(full, timeout, EXEC_CWD)


def _truncate(text: str, n: int = MAX_RETURN_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n] + "...(truncated)"


def _outbox_write(tag: str, task: str, ok: bool, output: str) -> None:
    f = OUTBOX / f"{int(time.time() * 1000)}.txt"
    f.write_text(f"[{tag}] {'OK' if ok else 'FAIL'}: {task}\n\n{output}", encoding="utf-8")
    logger.info("background done: %s -> %s", task[:40], f.name)


def _outbox_read_latest() -> str:
    files = sorted(OUTBOX.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if not files:
        return "还没有完成的任务。"
    latest = files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    latest.rename(OUTBOX / "done" / latest.name)
    return _truncate(text)


from mcp.server.fastmcp import FastMCP  # noqa: E402

import sys as _sys
_sys.path.insert(0, str((ROOT.parent / "gateway").resolve()))
import agents_core  # noqa: E402

mcp = FastMCP("xiaozhi-cloud-bridge")


@mcp.tool()
async def claude_query(task: str) -> str:
    """调用电脑上的 Claude Code CLI 执行任务(访问本地文件/知识库/运行技能)。

    何时使用: 用户要求 Claude 写文章、查资料、分析、处理本地文件等复杂任务。
    本工具异步执行: 立即返回"正在执行"的回执, 结果稍后由 agent_result_check 获取。
    简单闲聊/查时间/控制设备不要调用本工具。
    Args:
        task: 给 Claude Code 的中文指令, 越具体越好。
    """
    async def _bg() -> None:
        res = await asyncio.to_thread(
            lambda: _run_agent(CLAUDE_BIN, ["-p", task, "--output-format", "text"], ASYNC_TIMEOUT_SECS)
        )
        if res["ok"]:
            result = _truncate(res["out"])
        else:
            result = f"Claude Code 执行失败(rc={res['rc']}): {_truncate(res['err'] or res['out'], 300)}"
        _outbox_write("claude", task, res["ok"], result)

    asyncio.create_task(_bg())
    return f"Claude Code 正在执行「{task[:30]}」, 稍后问「结果出来了吗」"


@mcp.tool()
async def codex_query(task: str) -> str:
    """调用电脑上的 Codex CLI 执行任务。

    何时使用: 用户明确要求 Codex 执行代码任务时调用。
    注意: 本机 Codex 若是商店版, 可能无法从后台进程启动, 此时会返回失败信息。
    Args:
        task: 给 Codex 的指令。
    """
    async def _bg() -> None:
        res = await asyncio.to_thread(
            lambda: _run_agent(CODEX_BIN, ["exec", "--full-auto", "--skip-git-repo-check", task], ASYNC_TIMEOUT_SECS)
        )
        if res["ok"]:
            result = _truncate(res["out"])
        else:
            result = f"Codex 执行失败(rc={res['rc']}): {_truncate(res['err'] or res['out'], 300)}"
        _outbox_write("codex", task, res["ok"], result)

    asyncio.create_task(_bg())
    return f"Codex 正在执行「{task[:30]}」, 稍后问「结果出来了吗」"


@mcp.tool()
def agent_result_check() -> str:
    """获取最近一个已完成的后台任务(Claude/Codex)结果。

    何时使用: 用户问「结果出来了吗 / 查到了吗 / 写完了吗」时调用。
    读取后该结果会归档, 下次返回更早或更新的结果。
    """
    return _outbox_read_latest()


@mcp.tool()
def docker_status() -> str:
    """查询电脑上 Docker 容器运行状态(容器名/状态/端口)。

    何时使用: 用户问 Docker、容器、服务是否在运行时调用。
    """
    if shutil.which("docker") is None:
        return "Docker 不可用(未找到 docker 命令)"
    res = _run(
        ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
        QUICK_TIMEOUT_SECS, EXEC_CWD,
    )
    if not res["ok"]:
        return f"无法查询 Docker: {_truncate(res['err'] or res['out'], 300)}"
    out = (res["out"] or "").strip()
    return _truncate(out) if out else "没有运行中的容器"


@mcp.tool()
def agent_status(agent: str = "all") -> str:
    """查询本机各 agent(agy/pi/claude/codex) 状态: CLI 可用性/运行进程/待确认问题/最近事件。

    何时使用: 用户问「电脑上 agent 状态 / 哪些 agent 能用 / 谁在运行」时调用。
    Args:
        agent: all 或 agy/pi/claude/codex。
    """
    name = (agent or "all").lower()
    if name == "all":
        return _truncate(agents_core.status_all_text())
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {name} (可选: {', '.join(agents_core.AGENT_CLIS)})"
    return _truncate(agents_core.status_text(name))


@mcp.tool()
async def agent_query(agent: str, task: str) -> str:
    """调用电脑上指定 agent(agy/pi/claude/codex) 执行任务。

    何时使用: 用户要求 agy/pi/claude/codex 查资料、写代码、分析等复杂任务时。
    本工具异步执行: 立即返回"正在执行"回执, 结果稍后由 agent_result_check 获取。
    Args:
        agent: agy/pi/claude/codex。
        task: 给该 agent 的中文指令。
    """
    name = (agent or "").lower()
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {name} (可选: {', '.join(agents_core.AGENT_CLIS)})"

    async def _bg() -> None:
        result = await asyncio.to_thread(
            lambda: agents_core.query(name, task, ASYNC_TIMEOUT_SECS)
        )
        _outbox_write(name, task, True, result)

    asyncio.create_task(_bg())
    return f"{name} 正在执行「{task[:30]}」, 稍后问「结果出来了吗」"


@mcp.tool()
def agent_pending(clear: bool = False) -> str:
    """获取待播报的 agent 事件与待确认问题(如「claude 需要确认: 是否允许运行命令」)。

    何时使用: 用户问「有没有 agent 消息/待办/谁找我」, 或回答待确认问题时先调用。
    Args:
        clear: 读完是否清除已读事件(建议 true)。
    """
    return _truncate(agents_core.pending_text(bool(clear)))


@mcp.tool()
def agent_confirm(agent: str, answer: str) -> str:
    """把用户的语音回答回写给指定 agent 的待确认问题。

    何时使用: 用户对「agent 需要确认」的问题给出回答(允许/拒绝/补充说明)后调用。
    Args:
        agent: agy/pi/claude/codex。
        answer: 用户的回答内容。
    """
    name = (agent or "").lower()
    ok, msg = agents_core.confirm_answer(name, answer)
    return _truncate(msg)


if __name__ == "__main__":
    logger.info("xiaozhi-cloud-bridge server starting (cwd=%s)", EXEC_CWD)
    mcp.run(transport="stdio")
