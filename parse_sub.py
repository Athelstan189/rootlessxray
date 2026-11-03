#!/usr/bin/env python3
"""Read ./subscription and write the kept share links to nodes.txt.

Uses the same IPv4 parser as test_ipv4_xray.py. Does not restart xray.
start.sh runs this first, then restarts the proxy.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import test_ipv4_xray as core

_state = os.environ.get("XRAY_STATE_DIR", "").strip()
BASE = Path(_state).expanduser().resolve() if _state else Path(__file__).resolve().parent
NODES = BASE / "nodes.txt"
SUB = BASE / "subscription"
MAX_BODY = 2_000_000
URI_RE = re.compile(r"(?:vless|vmess|ss|trojan)://\S+")
_URL_RE = re.compile(r"https?://\S+")


def usage() -> None:
    print(
        "把一行订阅地址写进 ~/rootlessxray/subscription，然后 source 本目录的 start.sh",
        file=sys.stderr,
    )


def public_error(exc: BaseException) -> str:
    return _URL_RE.sub("订阅地址", str(exc))


def decode_subscription(body: str) -> str:
    text = body.replace("\ufeff", "").strip()
    if re.search(r"(?:vless|vmess|ss|trojan)://", text):
        return text
    if "proxies:" in text or text.startswith("mixed-port:"):
        return text
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return text
    try:
        decoded = core.b64decode_auto(compact).decode("utf-8", "replace")
    except Exception:
        return text
    return decoded if "://" in decoded else text


def fetch_url(url: str) -> tuple[str, str]:
    # This provider returns a dummy 192.0.2.1 node unless the client
    # identifies as v2rayN 7.2x or newer.
    req = urllib.request.Request(url, headers={"User-Agent": "v2rayN/7.25.6"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise RuntimeError("订阅内容超过 2MB")
        return raw.decode("utf-8", "replace"), "直连"
    except Exception as first:
        proxy = f"http://127.0.0.1:{core.HTTP_PORT}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        try:
            with opener.open(req, timeout=25) as resp:
                raw = resp.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise RuntimeError("订阅内容超过 2MB")
            return raw.decode("utf-8", "replace"), f"本机代理 {proxy}"
        except Exception as second:
            raise RuntimeError(f"直连失败（{first}）；经本机代理也失败（{second}）") from second


def load_subscription() -> str | None:
    """Return decoded share-link text, or None when subscription is absent.

    A single http(s) line is fetched once. Several lines are the body itself.
    """
    if not SUB.is_file():
        return None
    text = SUB.read_text(encoding="utf-8", errors="replace").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise SystemExit("subscription 是空的。写入订阅地址后再运行。代理没重启。")
    if len(lines) == 1 and lines[0].startswith(("http://", "https://")):
        try:
            body, via = fetch_url(lines[0])
        except Exception as exc:
            raise SystemExit(f"获取订阅失败：{public_error(exc)}。代理没重启。") from exc
        print(f"已获取订阅（{via}）", file=sys.stderr)
        return reject_revoked(decode_subscription(body))
    return reject_revoked(decode_subscription("\n".join(lines)))


def reject_revoked(text: str) -> str:
    if "订阅已失效" in text:
        raise SystemExit(
            "订阅已失效（客户端变更或疑似泄露）。"
            "到面板重新生成地址，只写进 subscription，再运行一次。"
            "不要用浏览器或其他软件打开这个地址。"
            "nodes.txt 未改，代理没重启。"
        )
    return text


def classify(blob: str) -> tuple[list[tuple[str, str, dict[str, Any] | None]], dict[str, int]]:
    found = URI_RE.findall(blob)
    if "proxies:" in blob and not found:
        raise SystemExit("这是 Clash 配置。这里解析 vless://、vmess://、ss:// 分享链接。")
    socket.setdefaulttimeout(5)

    def parse_one(uri: str) -> dict[str, Any] | None:
        if uri.startswith("trojan://"):
            return None
        return core.parse_node(uri)

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            parsed = list(pool.map(parse_one, found))
    finally:
        socket.setdefaulttimeout(None)

    seen: set[tuple[str, ...]] = set()
    rows: list[tuple[str, str, dict[str, Any] | None]] = []
    counts = {"links": len(found), "keep": 0, "dup": 0, "drop": 0}
    for uri, node in zip(found, parsed):
        if not node:
            counts["drop"] += 1
            rows.append(("未得到 IPv4", uri, None))
            continue
        key = core.route_key(node)
        if key in seen:
            counts["dup"] += 1
            rows.append(("重复", uri, node))
            continue
        seen.add(key)
        counts["keep"] += 1
        rows.append(("保留", uri, node))
    return rows, counts


def show(rows: list[tuple[str, str, dict[str, Any] | None]], counts: dict[str, int]) -> None:
    print(
        f"链接 {counts['links']} 条，保留 {counts['keep']}，重复 {counts['dup']}，未得到 IPv4 {counts['drop']}"
    )
    shown_drops = 0
    for status, uri, node in rows:
        if status == "未得到 IPv4":
            shown_drops += 1
            if shown_drops <= 20:
                print(f"DROP\t{uri[:120]}")
            continue
        assert node is not None
        sni = node.get("sni") or ""
        extra = f"\tsni={sni}" if sni and sni != node.get("address") else ""
        path = node.get("path") or ""
        if path:
            extra += f"\tpath={path[:40]}"
        ech = node.get("ech") or ""
        if ech:
            extra += f"\tech={ech[:40]}"
        name = str(node.get("tag_name") or "")[:40]
        print(f"{'KEEP' if status == '保留' else 'DUP'}\t{node.get('protocol')} {node.get('address')}:{node.get('port')}{extra}\t{name}")
    hidden = counts["drop"] - min(counts["drop"], 20)
    if hidden > 0:
        print(f"DROP\t还有 {hidden} 条未得到 IPv4 的链接没有逐条列出")


def write_nodes(rows: list[tuple[str, str, dict[str, Any] | None]]) -> int:
    kept = [uri for status, uri, _ in rows if status == "保留"]
    if not kept:
        raise SystemExit("没有可以写入的 IPv4 节点，nodes.txt 未改。")
    tmp = NODES.with_name("nodes.txt.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmp, NODES)
    print(f"已写入 {len(kept)} 条到 nodes.txt。")
    return len(kept)


def main(argv: list[str]) -> int:
    if any(arg in {"-h", "--help"} for arg in argv):
        usage()
        return 0
    if any(arg == "--write" for arg in argv):
        print("不用加 --write。地址写在 subscription 里，直接运行即可。", file=sys.stderr)
    blob = load_subscription()
    if blob is None:
        print("没有 subscription，继续使用现有 nodes.txt。")
        return 0
    rows, counts = classify(blob)
    show(rows, counts)
    write_nodes(rows)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        raise SystemExit(0)
