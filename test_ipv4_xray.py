#!/usr/bin/env python3
"""Probe nodes.txt (IPv4 only), rewrite config.json outbounds, print APPLIED/NO_WORKING_NODE.

Pass/fail standard: the node must reach ChatGPT (Codex backend). Google 204 alone
is NOT enough — many exits open Google but time out on chatgpt.com.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parent
_state = os.environ.get("XRAY_STATE_DIR", "").strip()
BASE = Path(_state).expanduser().resolve() if _state else KIT
_bin = os.environ.get("XRAY_BIN", "").strip()
XRAY = Path(_bin) if _bin else (KIT / "xray")
CONFIG = BASE / "config.json"
NODES = BASE / "nodes.txt"
PORTS_FILE = BASE / "ports.env"
APPLIED_FILE = BASE / "applied.json"
PIN_FILE = BASE / "pin.json"

# Codex actually talks to this endpoint. Override with XRAY_PROBE_URL if needed.
PROBE_URL = os.environ.get(
    "XRAY_PROBE_URL", "https://chatgpt.com/backend-api/codex/responses"
).strip()
# POST empty JSON → reachable nodes typically return 401 Unauthorized (not timeout).
PROBE_POST_BODY = "{}"
MAX_WORKERS = 8
PROBE_TIMEOUT = 12
MAX_APPLY = 0  # 0 = no limit
SKIP_HOSTS = {"192.0.2.1", "127.0.0.1", "localhost", "0.0.0.0"}


def http_reachable(code: str) -> bool:
    """Any real HTTP status means TCP+TLS to ChatGPT worked (401/403 are OK)."""
    try:
        n = int(code)
    except ValueError:
        return False
    return 200 <= n < 600


def load_ports() -> tuple[int, int]:
    """Read SOCKS/HTTP ports from env or ports.env (relative to this package)."""
    socks = os.environ.get("XRAY_SOCKS_PORT", "").strip()
    http = os.environ.get("XRAY_HTTP_PORT", "").strip()
    if PORTS_FILE.is_file():
        for line in PORTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "XRAY_SOCKS_PORT" and not socks:
                socks = v
            elif k == "XRAY_HTTP_PORT" and not http:
                http = v
    if not socks or not http:
        raise SystemExit("ports.env 里没有端口。请重新运行 start.sh。")
    if socks in {"31808", "31809"} or http in {"31808", "31809"}:
        raise SystemExit("31808/31809 是另一套个人代理，rootlessxray 不会占用。")
    return int(socks), int(http)


SOCKS_PORT, HTTP_PORT = load_ports()


def log(msg: str) -> None:
    print(msg, flush=True)


# AliDNS and DNSPod UDP/53 are blocked from this host. Cloudflare and Google are not.
_BLOCKED_ECH_DNS = ("223.5.5.5", "119.29.29.29")


def normalize_ech(ech: str) -> str:
    for blocked in _BLOCKED_ECH_DNS:
        if blocked in ech:
            ech = ech.replace(blocked, "1.1.1.1")
    return ech


def b64decode_auto(data: str) -> bytes:
    s = data.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s)


def is_ipv4(host: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host or ""))


def is_ipv6(host: str) -> bool:
    return ":" in (host or "")


def resolve_ipv4(host: str) -> str | None:
    if not host or host in SKIP_HOSTS:
        return None
    if is_ipv4(host):
        return host
    if is_ipv6(host):
        return None
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        ip = info[4][0]
        if is_ipv4(ip) and ip not in SKIP_HOSTS:
            return ip
    return None


def parse_ss(uri: str) -> dict[str, Any] | None:
    # ss://method:password@host:port  OR  ss://base64(method:password@host:port)
    # OR ss://base64(method:password)@host:port#name
    # Also handles clients that emit empty query before fragment: ...:1815?#name
    raw = uri[5:]
    name = ""
    if "#" in raw:
        raw, frag = raw.split("#", 1)
        name = urllib.parse.unquote(frag)
    # Drop unused query (?plugin=... or bare "?") so port stays numeric.
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    try:
        if "@" in raw and not raw.split("@", 1)[0].endswith("="):
            # maybe userinfo@host already, or base64userinfo@host
            userinfo, hostport = raw.rsplit("@", 1)
            if ":" not in userinfo:
                userinfo = b64decode_auto(userinfo).decode("utf-8", "replace")
            method, password = userinfo.split(":", 1)
            host, port_s = hostport.rsplit(":", 1)
        else:
            decoded = b64decode_auto(raw).decode("utf-8", "replace")
            userinfo, hostport = decoded.rsplit("@", 1)
            method, password = userinfo.split(":", 1)
            host, port_s = hostport.rsplit(":", 1)
        host = host.strip("[]")
        port = int(port_s)
    except Exception:
        return None
    ipv4 = resolve_ipv4(host)
    if not ipv4:
        return None
    return {
        "protocol": "shadowsocks",
        "tag_name": name or f"ss-{ipv4}",
        "address": ipv4,
        "port": port,
        "method": method,
        "password": password,
        "source_host": host,
    }


def parse_vmess(uri: str) -> dict[str, Any] | None:
    """Parse vmess://<base64(json)>[#name] share links."""
    raw = uri[8:]
    name_override = ""
    if "#" in raw:
        raw, frag = raw.split("#", 1)
        name_override = urllib.parse.unquote(frag)
    try:
        obj = json.loads(b64decode_auto(raw).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    host = str(obj.get("add") or "").strip().strip("[]")
    if not host:
        return None
    try:
        port = int(obj.get("port") or 0)
    except (TypeError, ValueError):
        return None
    uuid = str(obj.get("id") or "").strip()
    if not uuid or port <= 0:
        return None
    ipv4 = resolve_ipv4(host)
    if not ipv4:
        return None
    try:
        alter_id = int(obj.get("aid") or 0)
    except (TypeError, ValueError):
        alter_id = 0
    tls = str(obj.get("tls") or "").strip().lower()
    security = "tls" if tls in {"tls", "1", "true"} else "none"
    sni = str(obj.get("sni") or obj.get("host") or host).strip()
    network = str(obj.get("net") or "tcp").strip() or "tcp"
    name = name_override or str(obj.get("ps") or "").strip() or f"vmess-{ipv4}"
    return {
        "protocol": "vmess",
        "tag_name": name,
        "address": ipv4,
        "port": port,
        "uuid": uuid,
        "alter_id": alter_id,
        "cipher": str(obj.get("scy") or "auto").strip() or "auto",
        "network": network,
        "security": security,
        "sni": sni,
        "fp": str(obj.get("fp") or "chrome").strip() or "chrome",
        "alpn": [x for x in str(obj.get("alpn") or "").split(",") if x],
        "path": str(obj.get("path") or ""),
        "headerType": str(obj.get("type") or "none"),
        "host_header": str(obj.get("host") or ""),
        "serviceName": str(obj.get("path") or "") if network == "grpc" else "",
        "mode": "",
        "ech": normalize_ech(str(obj.get("ech") or "")),
        "source_host": host,
    }


def parse_vless(uri: str) -> dict[str, Any] | None:
    try:
        u = urllib.parse.urlparse(uri)
        if u.scheme != "vless" or not u.username or not u.hostname:
            return None
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        host = u.hostname.strip("[]")
        port = u.port or 443
        ipv4 = resolve_ipv4(host)
        if not ipv4:
            return None
        name = urllib.parse.unquote(u.fragment or "") or f"vless-{ipv4}"
        return {
            "protocol": "vless",
            "tag_name": name,
            "address": ipv4,
            "port": int(port),
            "uuid": urllib.parse.unquote(u.username),
            "encryption": qs.get("encryption", "none"),
            "flow": qs.get("flow", ""),
            "network": qs.get("type", "tcp"),
            "security": qs.get("security", "none"),
            "sni": qs.get("sni") or qs.get("host") or host,
            "fp": qs.get("fp") or "chrome",
            "alpn": [x for x in urllib.parse.unquote(qs.get("alpn", "")).split(",") if x],
            "pbk": qs.get("pbk", ""),
            "sid": qs.get("sid", ""),
            "spx": qs.get("spx", ""),
            "path": urllib.parse.unquote(qs.get("path", "")),
            "headerType": qs.get("headerType", "none"),
            "host_header": qs.get("host", ""),
            "serviceName": qs.get("serviceName", ""),
            "mode": qs.get("mode", ""),
            "ech": normalize_ech(qs.get("ech", "")),
            "source_host": host,
        }
    except Exception:
        return None


def parse_node(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("vless://"):
        return parse_vless(line)
    if line.startswith("ss://"):
        return parse_ss(line)
    if line.startswith("vmess://"):
        return parse_vmess(line)
    return None


def route_key(node: dict[str, Any]) -> tuple[str, ...]:
    """Identity of a dialable route.

    Resolved IPv4 plus port and credential is not enough: many CDN links share
    one edge IP and UUID, and differ by SNI, Host, path, or ECH query domain.
    Backup hostnames that resolve to the same IP with the same route still collapse.
    """
    return (
        str(node.get("protocol") or ""),
        str(node.get("address") or ""),
        str(node.get("port") or ""),
        str(node.get("uuid") or node.get("password") or ""),
        str(node.get("method") or ""),
        str(node.get("network") or ""),
        str(node.get("security") or ""),
        str(node.get("sni") or ""),
        str(node.get("host_header") or ""),
        str(node.get("path") or ""),
        str(node.get("serviceName") or ""),
        str(node.get("ech") or ""),
        str(node.get("pbk") or ""),
        str(node.get("sid") or ""),
        str(node.get("flow") or ""),
    )


def tls_block(node: dict[str, Any], server_name: str) -> dict[str, Any]:
    tls: dict[str, Any] = {
        "serverName": server_name,
        "fingerprint": node.get("fp") or "chrome",
    }
    if node.get("alpn"):
        tls["alpn"] = node["alpn"]
    ech = str(node.get("ech") or "").strip()
    if ech:
        # Share-link form, e.g. crypto.cloudflare.com+udp://223.5.5.5
        tls["echConfigList"] = ech
    return tls


def _stream_settings_from_node(node: dict[str, Any]) -> dict[str, Any]:
    stream: dict[str, Any] = {
        "network": node.get("network") or "tcp",
        "security": node.get("security") or "none",
    }

    net = stream["network"]
    if net == "ws":
        ws: dict[str, Any] = {"path": node.get("path") or "/"}
        if node.get("host_header"):
            ws["headers"] = {"Host": node["host_header"]}
        stream["wsSettings"] = ws
    elif net in {"xhttp", "splithttp"}:
        # newer Xray uses xhttp / splithttp
        stream["network"] = "xhttp"
        xhttp: dict[str, Any] = {"path": node.get("path") or "/"}
        if node.get("host_header"):
            xhttp["host"] = node["host_header"]
        if node.get("mode"):
            xhttp["mode"] = node["mode"]
        stream["xhttpSettings"] = xhttp
    elif net == "grpc":
        grpc: dict[str, Any] = {"serviceName": node.get("serviceName") or ""}
        if node.get("mode"):
            grpc["multiMode"] = node["mode"] == "multi"
        stream["grpcSettings"] = grpc
    else:
        # tcp
        if node.get("headerType") and node["headerType"] != "none":
            stream["tcpSettings"] = {
                "header": {
                    "type": node["headerType"],
                    "request": {
                        "headers": {"Host": [node["host_header"]]} if node.get("host_header") else {}
                    },
                }
            }

    sec = stream["security"]
    if sec == "tls":
        stream["tlsSettings"] = tls_block(
            node,
            node.get("sni") or node["source_host"],
        )
    elif sec == "reality":
        reality: dict[str, Any] = {
            "serverName": node.get("sni") or node["source_host"],
            "fingerprint": node.get("fp") or "chrome",
            "publicKey": node.get("pbk") or "",
            "shortId": node.get("sid") or "",
        }
        if node.get("spx"):
            reality["spiderX"] = node["spx"]
        stream["realitySettings"] = reality
    return stream


def outbound_from_node(node: dict[str, Any], tag: str) -> dict[str, Any]:
    if node["protocol"] == "shadowsocks":
        return {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": node["address"],
                        "port": node["port"],
                        "method": node["method"],
                        "password": node["password"],
                    }
                ]
            },
        }

    stream = _stream_settings_from_node(node)

    if node["protocol"] == "vmess":
        user: dict[str, Any] = {
            "id": node["uuid"],
            "alterId": int(node.get("alter_id") or 0),
            "security": node.get("cipher") or "auto",
        }
        return {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": node["address"],
                        "port": node["port"],
                        "users": [user],
                    }
                ]
            },
            "streamSettings": stream,
        }

    # vless
    user = {
        "id": node["uuid"],
        "encryption": node.get("encryption") or "none",
    }
    if node.get("flow"):
        user["flow"] = node["flow"]

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": node["address"],
                    "port": node["port"],
                    "users": [user],
                }
            ]
        },
        "streamSettings": stream,
    }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def probe_node(node: dict[str, Any]) -> tuple[bool, float, str]:
    """Return (ok, latency_seconds, detail)."""
    socks_port = free_port()
    tag = "probe"
    outbound = outbound_from_node(node, tag)
    cfg = {
        "log": {"loglevel": "error"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }
        ],
        "outbounds": [outbound],
    }

    with tempfile.TemporaryDirectory(prefix="xray-probe-") as td:
        conf_path = Path(td) / "config.json"
        conf_path.write_text(json.dumps(cfg), encoding="utf-8")
        log_path = Path(td) / "run.log"
        proc = subprocess.Popen(
            [str(XRAY), "run", "-config", str(conf_path)],
            stdout=open(log_path, "wb"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "LD_LIBRARY_PATH": ""},
        )
        try:
            # wait until port is open
            deadline = time.time() + 3
            while time.time() < deadline:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    if s.connect_ex(("127.0.0.1", socks_port)) == 0:
                        break
                if proc.poll() is not None:
                    return False, -1.0, "xray exited early"
                time.sleep(0.1)
            else:
                return False, -1.0, "xray port not ready"

            t0 = time.time()
            curl_env = {
                k: v
                for k, v in os.environ.items()
                if k.lower()
                not in {
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "ld_library_path",
                }
            }
            curl_env["LD_LIBRARY_PATH"] = ""
            curl_bin = "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else "curl"
            curl_cmd = [
                curl_bin,
                "-sS",
                "-o",
                "/dev/null",
                "--max-time",
                str(PROBE_TIMEOUT),
                "--socks5-hostname",
                f"127.0.0.1:{socks_port}",
                "-w",
                "%{http_code}",
            ]
            # Codex backend needs POST; plain homepage GET works if XRAY_PROBE_URL is overridden.
            if "backend-api" in PROBE_URL:
                curl_cmd += [
                    "-X",
                    "POST",
                    "-H",
                    "content-type: application/json",
                    "-d",
                    PROBE_POST_BODY,
                ]
            curl_cmd.append(PROBE_URL)
            curl = subprocess.run(
                curl_cmd,
                capture_output=True,
                text=True,
                env=curl_env,
            )
            dt = time.time() - t0
            code = (curl.stdout or "").strip()
            if curl.returncode == 0 and http_reachable(code):
                return True, dt, f"HTTP {code}"
            return False, dt, f"curl_rc={curl.returncode} code={code} err={(curl.stderr or '')[:80]}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def load_nodes() -> list[dict[str, Any]]:
    if not NODES.is_file():
        raise SystemExit(f"Missing nodes file: {NODES}")
    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for line in NODES.read_text(encoding="utf-8", errors="replace").splitlines():
        node = parse_node(line)
        if not node:
            continue
        key = route_key(node)
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
    return nodes


def read_pin_name() -> str:
    """Return the pinned node name, or empty when selection stays automatic."""
    try:
        data = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict) or data.get("mode") != "pin":
        return ""
    return str(data.get("name") or "").strip()


def build_config(working: list[dict[str, Any]], selector: str = "proxy-") -> dict[str, Any]:
    # Always rebuild inbounds from ports.env so copied kits / custom ports work.
    base: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": SOCKS_PORT,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            },
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": HTTP_PORT,
                "protocol": "http",
                "settings": {},
            },
        ],
    }

    outbounds = []
    for i, node in enumerate((working if MAX_APPLY <= 0 else working[:MAX_APPLY]), start=1):
        tag = f"proxy-{i:03d}"
        outbounds.append(outbound_from_node(node, tag))

    base["outbounds"] = outbounds
    base["observatory"] = {
        "subjectSelector": ["proxy-"],
        "probeURL": PROBE_URL,
        "probeInterval": "5m",
    }
    base["routing"] = {
        "domainStrategy": "AsIs",
        "balancers": [
            {
                "tag": "auto",
                "selector": [selector],
                "strategy": {"type": "leastPing"},
            }
        ],
        "rules": [
            {
                "type": "field",
                "network": "tcp,udp",
                "balancerTag": "auto",
            }
        ],
    }
    return base


def main() -> int:
    if not XRAY.is_file() or not os.access(XRAY, os.X_OK):
        log(f"ERROR: xray binary missing/unexecutable: {XRAY}")
        return 2

    nodes = load_nodes()
    log(f"Loaded {len(nodes)} IPv4-resolvable unique nodes from {NODES.name}")
    log(f"Probe standard: ChatGPT reachability via {PROBE_URL} (any HTTP 2xx-5xx = OK)")
    if not nodes:
        log("NO_WORKING_NODE")
        return 0

    working: list[tuple[float, dict[str, Any], str]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(probe_node, n): n for n in nodes}
        for fut in as_completed(futs):
            node = futs[fut]
            try:
                ok, latency, detail = fut.result()
            except Exception as e:
                ok, latency, detail = False, -1.0, str(e)
            label = f"{node['protocol']} {node['address']}:{node['port']}"
            sni = node.get("sni") or ""
            if sni and sni != node["address"]:
                label += f" sni={sni}"
            if ok:
                log(f"OK\t{latency:.3f}s\t{label}\t{detail}\t{node.get('tag_name','')[:40]}")
                working.append((latency, node, detail))
            else:
                log(f"FAIL\t{label}\t{detail}")

    working.sort(key=lambda x: x[0])
    if not working:
        log("NO_WORKING_NODE")
        return 0

    ranked = working if MAX_APPLY <= 0 else working[:MAX_APPLY]
    chosen = [n for _, n, _ in ranked]
    pin_name = read_pin_name()
    selector = "proxy-"
    if pin_name:
        for i, node in enumerate(chosen, start=1):
            if (node.get("tag_name") or "") == pin_name:
                selector = f"proxy-{i:03d}"
                log(f"PIN\t{selector}\t{pin_name}")
                break
        else:
            log(f"PIN_MISSED\t{pin_name}")
            pin_name = ""
    cfg = build_config(chosen, selector)
    applied_doc = {
        "mode": "pin" if pin_name else "auto",
        "pin": pin_name,
        "nodes": [],
    }
    for i, (latency, node, detail) in enumerate(ranked, start=1):
        tag = f"proxy-{i:03d}"
        applied_doc["nodes"].append(
            {
                "tag": tag,
                "name": node.get("tag_name") or tag,
                "protocol": node.get("protocol") or "",
                "address": node.get("address") or "",
                "port": node.get("port") or 0,
                "latency": round(latency, 3),
                "detail": detail,
                "active": selector == "proxy-" or tag == selector,
            }
        )
    # validate before write
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(cfg, tf, ensure_ascii=False, indent=2)
        tmp_name = tf.name
    try:
        test = subprocess.run(
            [str(XRAY), "run", "-test", "-config", tmp_name],
            capture_output=True,
            text=True,
            env={**os.environ, "LD_LIBRARY_PATH": ""},
        )
        if test.returncode != 0:
            log("ERROR: generated config failed xray -test")
            log((test.stderr or test.stdout or "")[:500])
            return 3
        # /tmp and /disk227 are different filesystems; use copy+replace
        shutil.copyfile(tmp_name, CONFIG)
        APPLIED_FILE.write_text(
            json.dumps(applied_doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(APPLIED_FILE, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    tags = [f"proxy-{i:03d}" for i in range(1, len(chosen) + 1)]
    log(
        "APPLIED\t"
        + f"count={len(chosen)}\t"
        + f"best={working[0][0]:.3f}s\t"
        + ",".join(tags[:5])
        + ("..." if len(tags) > 5 else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
