#!/usr/bin/env python3
"""Terminal screen for the nodes already accepted by rootlessxray.

Subscription parsing and the ChatGPT probe stay in start.sh. This screen
lists that result, pins one node or returns to automatic selection, and
restarts only this user's xray.
"""

from __future__ import annotations

import curses
import json
import locale
import os
import signal
import subprocess
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("XRAY_STATE_DIR") or (Path.home() / "rootlessxray")).expanduser().resolve()
XRAY = Path(os.environ.get("XRAY_BIN") or (KIT / "xray"))
CONFIG = STATE / "config.json"
APPLIED = STATE / "applied.json"
PIN = STATE / "pin.json"
PORTS = STATE / "ports.env"
SUB = STATE / "subscription"
PID_FILE = STATE / "xray.pid"
RUN_LOG = Path(os.environ.get("XRAY_LOG_DIR") or (KIT / "log" / os.environ.get("USER", "user"))) / "xray_run.log"
FORBIDDEN_STATES = {
    Path("/disk227/lixingqi/software/xray").resolve(),
    Path("/disk227/lixingqi/software/xray4all").resolve(),
    KIT.resolve(),
}
FORBIDDEN_PORTS = {"31808", "31809"}


def read_ports() -> tuple[str, str]:
    socks = http = ""
    try:
        lines = PORTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", ""
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip("\"'")
        if key.strip() == "XRAY_SOCKS_PORT":
            socks = val
        elif key.strip() == "XRAY_HTTP_PORT":
            http = val
    return socks, http


def load_applied() -> dict:
    try:
        data = json.loads(APPLIED.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        return data
    return {"mode": "auto", "pin": "", "nodes": rows_from_config()}


def rows_from_config() -> list[dict]:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    selector = ""
    try:
        selector = str(cfg["routing"]["balancers"][0]["selector"][0])
    except (KeyError, IndexError, TypeError):
        selector = "proxy-"
    rows = []
    for ob in cfg.get("outbounds") or []:
        if not isinstance(ob, dict):
            continue
        tag = str(ob.get("tag") or "")
        if not tag.startswith("proxy-"):
            continue
        address, port = outbound_endpoint(ob)
        rows.append(
            {
                "tag": tag,
                "name": tag,
                "protocol": ob.get("protocol") or "",
                "address": address,
                "port": port,
                "latency": None,
                "detail": "",
                "active": selector == "proxy-" or tag == selector,
            }
        )
    return rows


def outbound_endpoint(ob: dict) -> tuple[str, int]:
    settings = ob.get("settings") or {}
    servers = settings.get("servers") or settings.get("vnext") or []
    if not servers or not isinstance(servers[0], dict):
        return "", 0
    return str(servers[0].get("address") or ""), int(servers[0].get("port") or 0)


def write_pin(mode: str, name: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    PIN.write_text(
        json.dumps({"mode": mode, "name": name}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(PIN, 0o600)


def set_selector(tag: str) -> str:
    """Point the saved config at one proxy tag, or at every proxy- tag."""
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    balancers = cfg.setdefault("routing", {}).setdefault("balancers", [])
    if not balancers:
        raise SystemExit("config.json 里没有自动选择，先按 u 更新一次。")
    balancers[0]["selector"] = [tag]
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    test = subprocess.run(
        [str(XRAY), "run", "-test", "-config", str(tmp)],
        capture_output=True,
        text=True,
        env={**os.environ, "LD_LIBRARY_PATH": ""},
    )
    if test.returncode != 0:
        tmp.unlink(missing_ok=True)
        detail = (test.stderr or test.stdout or "").strip().splitlines()
        raise SystemExit(detail[-1] if detail else "配置校验失败")
    os.replace(tmp, CONFIG)
    return tag


def mark_applied(tag: str) -> None:
    data = load_applied()
    auto = tag == "proxy-"
    for row in data.get("nodes") or []:
        row_tag = str(row.get("tag") or "")
        row["active"] = auto or row_tag == tag
    data["mode"] = "auto" if auto else "pin"
    if not auto:
        match = [row for row in data.get("nodes") or [] if row.get("tag") == tag]
        data["pin"] = match[0].get("name", "") if match else data.get("pin", "")
    else:
        data["pin"] = ""
    APPLIED.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(APPLIED, 0o600)
    except OSError:
        pass


def port_open(port: str) -> bool:
    if not port.isdigit():
        return False
    proc = subprocess.run(
        ["ss", "-lntH"],
        capture_output=True,
        text=True,
    )
    suffix = f":{port}"
    return any(line.split()[3].endswith(suffix) for line in proc.stdout.splitlines() if len(line.split()) >= 4)


def restart_xray() -> str:
    socks, http = read_ports()
    if socks in FORBIDDEN_PORTS or http in FORBIDDEN_PORTS or not socks or not http:
        raise SystemExit("端口无效。31808/31809 属于另一套个人代理。")
    if not CONFIG.is_file():
        raise SystemExit("还没有 config.json。先按 u 从订阅更新。")
    test = subprocess.run(
        [str(XRAY), "run", "-test", "-config", str(CONFIG)],
        capture_output=True,
        text=True,
        env={**os.environ, "LD_LIBRARY_PATH": ""},
    )
    if test.returncode != 0:
        detail = (test.stderr or test.stdout or "").strip().splitlines()
        raise SystemExit(detail[-1] if detail else "配置校验失败")
    if PID_FILE.is_file():
        pid = PID_FILE.read_text(encoding="utf-8").strip()
        if pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
            time.sleep(0.4)
            try:
                os.kill(int(pid), 0)
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
        PID_FILE.unlink(missing_ok=True)
    pattern = f"{XRAY} run -config {CONFIG}"
    listed = subprocess.run(
        ["pgrep", "-u", os.environ.get("USER", ""), "-f", pattern],
        capture_output=True,
        text=True,
    )
    for line in listed.stdout.splitlines():
        if line.strip().isdigit():
            try:
                os.kill(int(line.strip()), signal.SIGTERM)
            except OSError:
                pass
    time.sleep(0.4)
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(RUN_LOG.parent, 0o700)
    except OSError:
        pass
    log_f = RUN_LOG.open("ab")
    proc = subprocess.Popen(
        [str(XRAY), "run", "-config", str(CONFIG)],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env={**os.environ, "LD_LIBRARY_PATH": ""},
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid) + "\n", encoding="utf-8")
    os.chmod(PID_FILE, 0o600)
    time.sleep(0.8)
    if not port_open(socks) or not port_open(http):
        raise SystemExit(f"代理没有在 {socks}/{http} 听。日志：{RUN_LOG}")
    return f"已启动  SOCKS {socks}  HTTP {http}"


def running_pid() -> str:
    if not PID_FILE.is_file():
        return ""
    pid = PID_FILE.read_text(encoding="utf-8").strip()
    if not pid.isdigit():
        return ""
    try:
        os.kill(int(pid), 0)
    except OSError:
        return ""
    return pid


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


class Screen:
    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self.cursor = 0
        self.message = ""
        self.confirm = False

    def reload(self) -> dict:
        return load_applied()

    def draw(self, data: dict) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 8 or width < 40:
            stdscr.addstr(0, 0, "终端太小")
            stdscr.refresh()
            return
        nodes = data.get("nodes") or []
        mode = "固定 " + str(data.get("pin") or "") if data.get("mode") == "pin" else "自动选择"
        socks, http = read_ports()
        pid = running_pid()
        sub = "有订阅" if SUB.is_file() and SUB.stat().st_size > 0 else "没有订阅"
        run = f"运行中 pid {pid}" if pid else "没有运行"
        header = f" rootlessxray    {mode}    {run}    {sub}"
        if socks and http:
            header += f"    {socks}/{http}"
        stdscr.addstr(0, 0, clip(header, width - 1), curses.A_REVERSE)
        stdscr.addstr(1, 0, clip(" 延迟    结果      节点", width - 1))
        body_top = 2
        body_bottom = height - 3
        visible = max(1, body_bottom - body_top)
        if self.cursor >= len(nodes):
            self.cursor = max(0, len(nodes) - 1)
        start = 0
        if self.cursor >= visible:
            start = self.cursor - visible + 1
        if not nodes:
            stdscr.addstr(body_top, 0, "还没有测通的节点。按 u 从订阅更新。")
        for offset, row in enumerate(nodes[start : start + visible]):
            idx = start + offset
            latency = row.get("latency")
            lat = f"{float(latency):5.2f}s" if isinstance(latency, (int, float)) else "  -  "
            detail = str(row.get("detail") or "")
            detail = detail.replace("HTTP ", "")
            name = str(row.get("name") or row.get("tag") or "")
            mark = "●" if row.get("active") and data.get("mode") == "pin" else " "
            line = f"{mark} {lat}  {detail:<8}  {name}"
            attr = curses.A_REVERSE if idx == self.cursor else curses.A_NORMAL
            if idx == self.cursor:
                attr |= curses.A_BOLD
            try:
                stdscr.addstr(body_top + offset, 0, clip(line, width - 1), attr)
            except curses.error:
                pass
        foot = "Enter 固定  a 自动  s 启动  u 更新  q 退出"
        if self.confirm:
            foot = "重新拉订阅并重启这份代理？ y 确认  其他键取消"
        elif self.message:
            foot = self.message
        try:
            stdscr.addstr(height - 2, 0, clip(foot, width - 1), curses.A_REVERSE)
            stdscr.addstr(height - 1, 0, clip(f" 状态目录 {STATE}", width - 1))
        except curses.error:
            pass
        stdscr.refresh()

    def apply_current(self, data: dict, auto: bool) -> None:
        nodes = data.get("nodes") or []
        if auto:
            write_pin("auto", "")
            if CONFIG.is_file():
                set_selector("proxy-")
                mark_applied("proxy-")
            self.message = restart_xray() if CONFIG.is_file() else "已改回自动选择。先按 u 生成节点。"
            return
        if not nodes:
            self.message = "没有可以固定的节点"
            return
        row = nodes[self.cursor]
        name = str(row.get("name") or "")
        tag = str(row.get("tag") or "")
        write_pin("pin", name)
        if CONFIG.is_file():
            set_selector(tag)
            mark_applied(tag)
            self.message = restart_xray() + f"  固定 {name}"
        else:
            self.message = "已记下固定节点。按 u 更新后会使用它。"

    def update_subscription(self) -> None:
        curses.endwin()
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = ""
        env["XRAY_STATE_DIR"] = str(STATE)
        env["XRAY_BIN"] = str(XRAY)
        subprocess.call(["bash", str(KIT / "start.sh")], env=env)
        self.message = "更新结束"
        self.confirm = False

    def loop(self) -> None:
        curses.curs_set(0)
        while True:
            data = self.reload()
            self.draw(data)
            key = self.stdscr.getch()
            if self.confirm:
                self.confirm = False
                if key in (ord("y"), ord("Y")):
                    self.update_subscription()
                else:
                    self.message = "已取消更新"
                continue
            self.message = ""
            if key in (ord("q"), ord("Q"), 27):
                return
            if key in (curses.KEY_UP, ord("k")):
                self.cursor = max(0, self.cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                nodes = data.get("nodes") or []
                if nodes:
                    self.cursor = min(len(nodes) - 1, self.cursor + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                try:
                    self.apply_current(data, auto=False)
                except SystemExit as exc:
                    self.message = str(exc)
            elif key in (ord("a"), ord("A")):
                try:
                    self.apply_current(data, auto=True)
                except SystemExit as exc:
                    self.message = str(exc)
            elif key in (ord("s"), ord("S")):
                try:
                    self.message = restart_xray()
                except SystemExit as exc:
                    self.message = str(exc)
            elif key in (ord("u"), ord("U")):
                self.confirm = True


def ensure_state() -> None:
    if STATE in FORBIDDEN_STATES:
        raise SystemExit("订阅和配置不能放在公共程序目录里。请使用自己的 ~/rootlessxray。")
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE, 0o700)
    except OSError:
        pass
    if not XRAY.is_file():
        raise SystemExit(f"找不到 xray 程序：{XRAY}")


def main() -> None:
    locale.setlocale(locale.LC_ALL, "")
    ensure_state()
    curses.wrapper(lambda stdscr: Screen(stdscr).loop())
    socks, http = read_ports()
    print("界面已退出。这个终端要走代理，执行：")
    print(f"  source {KIT / 'proxy_env.sh'}")
    if socks and http:
        print(f"代理端口  SOCKS {socks}  HTTP {http}")


if __name__ == "__main__":
    main()
