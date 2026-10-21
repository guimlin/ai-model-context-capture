"""The context monitor as one web page: watch the capture, save and open capture
files, group rounds into conversations, and read the exact context of every round.

    python ctxweb.py                 start it and open the page in your browser
    python ctxweb.py --port 8790     a different port
    python ctxweb.py --no-open       start it without opening a browser

Everything else - starting and stopping the capture, saving a capture, opening one,
hiding rounds, deleting rounds out of a capture file, rebuilding the round page -
happens in the page. It binds to loopback only and never talks to the internet.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import ctxlink
import ctxpage
import ctxstore

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {
    "proxy_port": int(os.environ.get("CTX_PROXY_PORT") or 8788),
    "log_dir": os.path.join(HERE, "logs"),
    "captures": os.path.join(HERE, "captures"),
    "web_dir": os.path.join(HERE, "web"),
    "upstream": os.environ.get("CTX_UPSTREAM") or "",
    "source": "",                      # empty means: view the live log folder
}
_proc = None                          # the capture process, when this page started it
_lock = threading.Lock()
_view_cache = {}
_upstream_cache = {"at": 0.0, "list": []}


# --------------------------------------------------------------- finding things

def _get(url, timeout=2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except Exception:
        return None, None


def proxy_state():
    """What the capture says about itself, or None when nothing answers on its port."""
    status, text = _get("http://127.0.0.1:%d/ctx-stats" % CFG["proxy_port"])
    if status != 200:
        return None
    try:
        info = json.loads(text)
    except ValueError:
        return None
    info["port"] = CFG["proxy_port"]
    info["ours"] = _proc is not None and _proc.poll() is None
    return info


def listening_ports():
    """Loopback TCP ports that are listening, as (port, owning pid)."""
    out = []
    try:
        text = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                              text=True, timeout=15).stdout
    except Exception:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        if not local.startswith("127.0.0.1:"):
            continue
        try:
            out.append((int(local.rsplit(":", 1)[1]), int(parts[4])))
        except ValueError:
            continue
    return out


def find_upstream(again=False):
    """Strata's own URL. The app picks a new loopback port every time it starts, so
    this probes the listeners and keeps the ones that answer like Strata. The capture
    forwards /v/models too, so anything that answers /ctx-stats is the capture, not
    Strata. Cached for a few seconds, because the page asks often."""
    now = time.time()
    if not again and now - _upstream_cache["at"] < 15:
        return _upstream_cache["list"]
    found = []
    for port in sorted(set(p for p, _ in listening_ports())):
        status, text = _get("http://127.0.0.1:%d/ctx-stats" % port, timeout=1.2)
        if status == 200 and '"proxy"' in (text or ""):
            continue
        status, text = _get("http://127.0.0.1:%d/v1/models" % port, timeout=2)
        if status != 200 or '"strata"' not in re.sub(r"\s+", "", text or ""):
            continue
        match = re.search(r'"id"\s*:\s*"([^"]+)"', text or "")
        found.append({"port": port, "model": match.group(1) if match else "?",
                      "url": "http://127.0.0.1:%d/v1" % port})
    _upstream_cache.update(at=now, list=found)
    return found


# ------------------------------------------------------------- capture start/stop

def start_capture():
    global _proc
    state = proxy_state()
    if state:
        return {"ok": True, "already": True, "state": state}
    upstream = CFG["upstream"]
    if not upstream:
        candidates = find_upstream(again=True)
        if not candidates:
            return {"ok": False, "error": "Strata is not answering on loopback - start the "
                                          "model first, then start the capture."}
        upstream = candidates[0]["url"]
    os.makedirs(CFG["log_dir"], exist_ok=True)
    command = [sys.executable, os.path.join(HERE, "context_proxy.py"),
               "--port", str(CFG["proxy_port"]), "--upstream", upstream,
               "--dir", CFG["log_dir"]]
    console = open(os.path.join(CFG["log_dir"], "logger-out.txt"), "ab")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # detached, own process group
    with _lock:
        _proc = subprocess.Popen(command, cwd=HERE, stdout=console, stderr=console,
                                 creationflags=flags)
    for _ in range(40):
        time.sleep(0.25)
        state = proxy_state()
        if state:
            return {"ok": True, "pid": _proc.pid, "upstream": upstream, "state": state}
    return {"ok": False, "error": "the capture started but is not answering on port %d - see "
                                  "logs\\logger-out.txt" % CFG["proxy_port"]}


def stop_capture():
    global _proc
    stopped = []
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        stopped.append(_proc.pid)
        for _ in range(20):
            time.sleep(0.2)
            if _proc.poll() is not None:
                break
    _proc = None
    if proxy_state():                     # started elsewhere: stop whoever owns the port
        for port, pid in listening_ports():
            if port == CFG["proxy_port"] and pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                stopped.append(pid)
    if proxy_state():
        return {"ok": False, "error": "something still answers on port %d, so it is still "
                                      "capturing" % CFG["proxy_port"]}
    return {"ok": True, "stopped": stopped}


# -------------------------------------------------------------------- what to show

def source_path():
    return CFG["source"] or CFG["log_dir"]


def drop_view(path=None):
    """Forget the rounds remembered for a source, and let go of its file, so the next
    look starts fresh - after a capture file has been rewritten, for instance."""
    for key in ([path] if path else list(_view_cache)):
        hit = _view_cache.pop(key, None)
        if hit and hit[1] and hit[1][0] is not None:
            try:
                hit[1][0].close()
            except Exception:
                pass
    for key in ([path] if path else list(_source_cache)):
        cap = _source_cache.pop(key, None)
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass


_source_cache = {}


def open_source():
    """The source being viewed, opened once and kept, so one file is not opened over
    and over by the page - and so drop_view can put it down again when the file is
    about to be rewritten."""
    path = source_path()
    cap = _source_cache.get(path)
    if cap is None or not os.path.exists(path):
        cap = ctxstore.Capture(path)
        _source_cache[path] = cap
    return cap


def view(cap=None):
    """The rounds in the source being viewed, with the view-only hidden ones pulled
    out, and the conversations worked out for the rest. Cached until something changes."""
    cap = cap or open_source()
    hidden_list = ctxstore.load_hidden(HERE).get(str(cap.path), [])
    events = cap.events()
    key = (cap.kind, len(events), len(hidden_list), tuple(hidden_list),
           os.path.getmtime(cap.path))
    hit = _view_cache.get(cap.path)
    if hit and hit[0] == key:
        return hit[1]
    reqs, resps, order = ctxstore._events(cap)
    hidden = set(hidden_list)
    shown = [s for s in order if s not in hidden]
    links = ctxlink.attach([reqs[s] for s in shown], cap, resps)
    result = (cap, reqs, resps, order, shown, hidden, links)
    _view_cache[cap.path] = (key, result)
    return result


def window_for(cap):
    """The model's context window: the one saved in a capture wins, then Strata's own
    run config, so the percentages match how the model is really being run."""
    size, where = cap.window()
    if size:
        return size, where or "saved in this capture"
    return ctxpage.find_context_window(0)


def tally(reqs, resps, shown, window):
    total = {"prompt": 0, "cached": 0, "out": 0, "peak": 0, "peak_seq": None, "requests": len(shown)}
    for seq in shown:
        prompt, cached, out = ctxstore._usage((resps.get(seq) or {}).get("usage"))
        size = prompt or (reqs[seq].get("est_prompt_tokens") or 0)
        total["prompt"] += prompt
        total["cached"] += cached
        total["out"] += out
        if size >= total["peak"]:
            total["peak"], total["peak_seq"] = size, seq
    total["window"] = window
    total["pct"] = round(100.0 * total["peak"] / window, 1) if window and total["peak"] else None
    return total


def describe_source(cap, reqs, resps, order, shown, hidden, links):
    window, where = window_for(cap)
    state = {
        "kind": cap.kind, "path": cap.path, "label": cap.label, "live": cap.kind == "dir",
        "rounds": len(order), "shown": len(shown), "hidden": sorted(hidden),
        "threads": len(links.get("_threads", [])), "window": window, "window_src": where,
        "counts": tally(reqs, resps, shown, window),
    }
    if cap.manifest:
        state["manifest"] = cap.manifest
        state["note"] = cap.manifest.get("note") or ""
        state["created"] = cap.manifest.get("created")
        state["edited"] = cap.manifest.get("edited")
        state["removed"] = cap.manifest.get("removed") or []
        state["selection"] = cap.manifest.get("selection")
        try:
            state["bytes"] = os.path.getsize(cap.path)
        except OSError:
            pass
    return state


def log_rounds():
    """How many rounds are in the live log folder right now, ignoring the page
    traffic the older capture used to write down."""
    try:
        _reqs, _resps, order = ctxstore._events(ctxstore.Capture(CFG["log_dir"]))
        return len(order)
    except (SystemExit, OSError):
        return 0


def api_state():
    cap, reqs, resps, order, shown, hidden, links = view()
    proxy = proxy_state()
    report = os.path.join(CFG["web_dir"], "report.html")
    return {
        "source": describe_source(cap, reqs, resps, order, shown, hidden, links),
        "capture": {
            "running": bool(proxy), "port": CFG["proxy_port"],
            "upstream": (proxy or {}).get("upstream") or CFG["upstream"],
            "seen": (proxy or {}).get("requests_seen"), "we_started": (proxy or {}).get("ours"),
            "strata": find_upstream(),
        },
        "paths": {"log": CFG["log_dir"], "captures": CFG["captures"], "report": report,
                  "agent_base": "http://127.0.0.1:%d" % CFG["proxy_port"]},
        "live_rounds": log_rounds() if cap.kind != "dir" else len(order),
        "report": {"built": int(os.path.getmtime(report)) if os.path.exists(report) else None,
                   "rounds": len(shown)},
    }


def api_rounds(query):
    cap, reqs, resps, order, shown, hidden, links = view()
    limit = int(query.get("limit", ["60"])[0] or 60)
    window = window_for(cap)[0]
    rows = []
    for seq in reversed(shown[-limit:]):
        req, resp = reqs[seq], resps.get(seq)
        link = links.get(seq) or {}
        prompt, cached, out = ctxstore._usage((resp or {}).get("usage"))
        rows.append({
            "seq": seq, "ts": (req.get("ts") or "")[11:19], "api": req.get("api") or "?",
            "model": req.get("model") or "?", "thread": link.get("thread") or "?",
            "rel": ("new" if link.get("parent") is None
                    else "again" if link.get("added") == 0 else "+%s" % link.get("added")),
            "prompt": prompt, "est": req.get("est_prompt_tokens") or 0,
            "size": prompt or (req.get("est_prompt_tokens") or 0),
            "cached": cached, "new": max(0, prompt - cached) if prompt else None, "out": out,
            "status": (resp or {}).get("status"), "hidden": seq in hidden,
            "tools": [c.get("name") for c in (resp or {}).get("tool_calls") or []],
        })
    return {"rows": rows, "of": len(shown), "total": len(order), "hidden": sorted(hidden),
            "window": window}


def api_log(query):
    tail = int(query.get("tail", ["14"])[0] or 14)
    lines = []
    for line in open_source().events().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "request" and not entry.get("api"):
            continue                    # the app's own page traffic, not a round
        lines.append({"seq": entry.get("seq"), "type": entry.get("type"),
                      "ts": (entry.get("ts") or "")[11:19], "summary": entry.get("summary") or ""})
    return {"lines": lines[-tail:]}


def api_threads():
    cap, reqs, resps, order, shown, hidden, links = view()
    out = []
    for thread in links.get("_threads", []):
        first = links.get(thread["first"]) or {}
        notes = []
        if thread["rounds_with_root_change"]:
            notes.append("root prompt changed at " + ", ".join("#%d" % s
                                                              for s in thread["rounds_with_root_change"]))
        if thread["rounds_with_tool_change"]:
            notes.append("tools changed at " + ", ".join("#%d" % s
                                                        for s in thread["rounds_with_tool_change"]))
        if thread.get("repeats"):
            notes.append("the same history went out again at "
                         + ", ".join("#%d" % s for s in thread["repeats"]))
        same = [t["name"] for t in links["_threads"]
                if t["name"] != thread["name"] and t["root_fp"] and t["root_fp"] == thread["root_fp"]]
        if same:
            notes.append("shares its root prompt with thread " + ", ".join(same))
        rounds = []
        for seq in thread["rounds"]:
            link = links.get(seq) or {}
            prompt, cached, out_tokens = ctxstore._usage((resps.get(seq) or {}).get("usage"))
            rounds.append({
                "seq": seq, "ts": (reqs[seq].get("ts") or "")[11:19],
                "api": reqs[seq].get("api") or "?", "parent": link.get("parent"),
                "added": link.get("added"), "size": prompt or (reqs[seq].get("est_prompt_tokens") or 0),
                "exact": bool(prompt), "root_changed": link.get("root_changed"),
                "same_history": link.get("same_history") or [], "hidden": seq in hidden,
            })
        out.append({"name": thread["name"], "id": thread["id"], "rounds": rounds,
                    "count": len(thread["rounds"]), "first": thread["first"], "last": thread["last"],
                    "client": first.get("client") or "", "root_chars": thread["root_chars"],
                    "root_fp": thread["root_fp"], "root_head": thread["root_head"],
                    "n_tools": thread["n_tools"], "tools_fp": thread["tools_fp"], "notes": notes})
    return {"threads": out, "hidden": sorted(hidden), "source_is_file": cap.kind == "file",
            "rounds": len(order), "shown": len(shown)}


# ------------------------------------------------------------------------ actions

def api_save(payload):
    """Save some of the rounds being viewed into a new capture file."""
    source = ctxstore.Capture(CFG["log_dir"]) if payload.get("from") == "live" else open_source()
    reqs, resps, order = ctxstore._events(source)
    chosen = ctxstore.choose(reqs, order, last=int(payload.get("last") or 0),
                             rounds=str(payload.get("rounds") or ""),
                             since=str(payload.get("since") or ""),
                             until=str(payload.get("until") or ""),
                             threads=str(payload.get("threads") or ""), cap=source)
    picked = [int(s) for s in payload.get("pick") or []]
    if picked:
        chosen = [s for s in chosen if s in set(picked)] or picked
    if not chosen:
        return {"ok": False, "error": "nothing matches that selection - this source holds "
                                      "rounds %s..%s" % (order[0] if order else "-",
                                                         order[-1] if order else "-")}
    window, where = window_for(source)
    bits = []
    if picked:
        bits.append("the %d rounds picked in the page" % len(picked))
    for key, label in (("rounds", "rounds "), ("threads", "threads "), ("last", "newest "),
                       ("since", "from "), ("until", "until ")):
        if payload.get(key):
            bits.append(str(label) + str(payload[key]))
    name = "capture-" + time.strftime("%Y%m%d-%H%M%S") + ctxstore.SUFFIX
    out_path = os.path.join(CFG["captures"], name)
    manifest = ctxstore.save(source, out_path, chosen, window=window, window_src=where or "",
                             note=str(payload.get("note") or ""),
                             with_payloads=not payload.get("no_payloads"),
                             selection="; ".join(bits) or "everything in this source")
    if payload.get("open_it", True):
        CFG["source"] = out_path
    return {"ok": True, "name": name, "path": out_path, "rounds": manifest["rounds"],
            "bytes": os.path.getsize(out_path)}


def api_open(payload):
    path = str(payload.get("path") or "")
    if path in ("", "live"):
        CFG["source"] = ""
        return {"ok": True, "source": CFG["log_dir"], "kind": "dir", "live": True}
    cap = ctxstore.Capture(path)
    CFG["source"] = cap.path
    return {"ok": True, "source": cap.path, "kind": cap.kind}


def api_hide(payload):
    cap = open_source()
    table = ctxstore.load_hidden(HERE)
    current = set(table.get(str(cap.path), []))
    seqs = set(int(s) for s in payload.get("seqs") or [])
    if payload.get("clear"):
        current = set()
    elif payload.get("undo"):
        current -= seqs
    else:
        current |= seqs
    ctxstore.set_hidden(HERE, cap.path, sorted(current))
    cap.close()
    drop_view(cap.path)
    build_report()
    return {"ok": True, "hidden": sorted(current)}


def api_delete(payload):
    seqs = [int(s) for s in payload.get("seqs") or []]
    if not seqs:
        return {"ok": False, "error": "pick the rounds to delete first"}
    path = source_path()
    drop_view(path)                        # stop holding the file we are about to rewrite
    try:
        result = ctxstore.delete_rounds(ctxstore.Capture(path), seqs)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    drop_view(path)
    build_report()
    return {"ok": True, "removed": result["removed"], "rounds": result["rounds"],
            "backup": result["backup"]}


def api_extract(payload):
    cap = open_source()
    if cap.kind != "file":
        return {"ok": False, "error": "you are looking at a live log folder already"}
    to = str(payload.get("to") or os.path.join(HERE, "logs-from-capture"))
    return {"ok": True, "to": to, "payloads": ctxstore.extract(cap, to)}


def api_reveal(payload):
    path = str(payload.get("path") or source_path())
    if not os.path.exists(path):
        return {"ok": False, "error": "not there: %s" % path}
    subprocess.Popen(["explorer", "/select,", os.path.abspath(path)])
    return {"ok": True, "path": path}


def build_report():
    """Write the context page for the source being viewed, hidden rounds left out."""
    cap, reqs, resps, order, shown, hidden, links = view()
    if not shown:
        return {"ok": False, "error": "nothing to build: all %d rounds in this source are "
                                      "hidden from the view. Press 'show hidden again' on "
                                      "the Threads tab first." % len(order)}
    ctxpage._cap(cap.path)
    window, where = window_for(cap)
    os.makedirs(CFG["web_dir"], exist_ok=True)
    html = ctxpage.render_html(reqs, resps, shown, 12000, 0, window, where, 10)
    out = os.path.join(CFG["web_dir"], "report.html")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(html)
    return {"ok": True, "out": out, "rounds": len(shown), "window": window,
            "bytes": os.path.getsize(out)}


# ------------------------------------------------------------------------- serving

class Handler(BaseHTTPRequestHandler):
    server_version = "CtxWeb/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "ignore"))
        except ValueError:
            return {}

    def do_GET(self):
        parts = urlparse(self.path)
        try:
            if parts.path in ("/", "/index.html"):
                return self._send(200, PAGE.encode("utf-8"), "text/html")
            if parts.path == "/api/state":
                return self._send(200, api_state())
            if parts.path == "/api/rounds":
                return self._send(200, api_rounds(parse_qs(parts.query)))
            if parts.path == "/api/log":
                return self._send(200, api_log(parse_qs(parts.query)))
            if parts.path == "/api/threads":
                return self._send(200, api_threads())
            if parts.path == "/api/captures":
                return self._send(200, {"captures": ctxstore.list_captures(CFG["captures"]),
                                        "folder": CFG["captures"], "source": source_path()})
            if parts.path == "/report":
                report = os.path.join(CFG["web_dir"], "report.html")
                if not os.path.exists(report):
                    build_report()
                with open(report, "rb") as handle:
                    return self._send(200, handle.read(), "text/html")
        except SystemExit as exc:               # a path or file that cannot be opened
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:                # noqa: BLE001 - keep the page alive
            return self._send(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        return self._send(404, {"ok": False, "error": "no such page: %s" % parts.path})

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._body()
        zero_arg = ("/api/capture/start", "/api/capture/stop", "/api/report/build")
        actions = {
            "/api/capture/start": start_capture, "/api/capture/stop": stop_capture,
            "/api/save": api_save, "/api/open": api_open, "/api/hide": api_hide,
            "/api/delete": api_delete, "/api/extract": api_extract,
            "/api/reveal": api_reveal, "/api/report/build": build_report,
        }
        action = actions.get(path)
        if action is None:
            return self._send(404, {"ok": False, "error": "no such action: %s" % path})
        try:
            result = action() if path in zero_arg else action(payload)
            return self._send(200, result)
        except SystemExit as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:                # noqa: BLE001
            return self._send(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strata context monitor</title>
<style>
:root { --bg:#141821; --panel:#1c2230; --panel2:#222a3b; --ink:#e6e9ef; --dim:#98a2b3;
        --line:#2c3547; --reuse:#3ddc84; --new:#f5a524; --over:#ff6b6b;
        --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--sans) }
header { padding:18px 22px 0; border-bottom:1px solid var(--line) }
h1 { margin:0 0 6px; font-size:20px }
.sub { color:var(--dim); font-size:13px }
.tabs { display:flex; gap:6px; margin:14px 0 -1px; flex-wrap:wrap }
.tab { background:var(--panel); border:1px solid var(--line); border-bottom:none; color:var(--ink);
       padding:9px 16px; font:inherit; cursor:pointer; border-radius:8px 8px 0 0; opacity:.65 }
.tab.on { background:var(--panel2); opacity:1; font-weight:600 }
main { padding:18px 22px 60px }
section { display:none } section.on { display:block }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px;
        margin:0 0 14px }
.card h2 { margin:0 0 6px; font-size:15px }
.card p { margin:6px 0; color:var(--dim); font-size:13.5px }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); gap:12px }
.kv { font-family:var(--mono); font-size:12.5px; color:var(--dim) }
.kv b { display:block; color:var(--ink); font-size:17px; margin-top:2px }
button { background:#2b3548; color:var(--ink); border:1px solid var(--line); border-radius:8px;
         padding:8px 13px; font:inherit; font-size:14px; cursor:pointer }
button:hover { border-color:#3d4a63 }
button.go { background:#1f4634; border-color:#2f6b4d }
button.stopb { background:#4a2330; border-color:#6d3040 }
button.warn { background:#4a3a1f; border-color:#6d5530 }
button[disabled] { opacity:.4; cursor:not-allowed }
input, select, textarea { background:var(--bg); color:var(--ink); border:1px solid var(--line);
        border-radius:7px; padding:7px 9px; font:inherit; font-size:14px }
label { font-size:13px; color:var(--dim); display:inline-flex; align-items:center; gap:6px }
table { border-collapse:collapse; width:100%; font-size:13.5px }
th { text-align:left; color:var(--dim); font-size:11.5px; text-transform:uppercase;
     letter-spacing:.06em; padding:7px 9px; border-bottom:1px solid var(--line) }
td { padding:6px 9px; border-bottom:1px solid var(--line); vertical-align:top }
td.n, th.n { text-align:right; font-family:var(--mono) }
.mono { font-family:var(--mono); font-size:12.5px }
.dim { color:var(--dim) }
.pill { display:inline-block; padding:1px 7px; border-radius:20px; background:var(--panel2);
        border:1px solid var(--line); font-size:11.5px; font-family:var(--mono) }
.chip { display:inline-flex; align-items:center; gap:5px; padding:3px 8px; margin:2px 3px 2px 0;
        border:1px solid var(--line); border-radius:7px; background:var(--panel2);
        font-family:var(--mono); font-size:12px; cursor:pointer }
.chip.hid { opacity:.4; text-decoration:line-through }
.chip.warn { border-color:#6d3040 }
.chips { margin-top:8px }
.toggle { font-size:12.5px; padding:4px 10px; background:var(--bg) }
.card h2 { white-space:nowrap }
pre { font-family:var(--mono); font-size:12.5px; background:var(--bg); border:1px solid var(--line);
      border-radius:8px; padding:9px 11px; overflow:auto; margin:6px 0; white-space:pre-wrap }
.bars { height:14px; border-radius:4px; background:var(--panel2); overflow:hidden; display:flex;
        min-width:110px }
.bars i { display:block; height:100%; background:var(--reuse) }
.bars i.new { background:var(--new) } .bars i.est { background:#5a6479 } .bars i.over { background:var(--over) }
iframe { width:100%; height:74vh; border:1px solid var(--line); border-radius:10px; background:#fff }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:8px 0 }
#toast { position:fixed; right:18px; bottom:18px; background:var(--panel2); border:1px solid var(--line);
         border-radius:10px; padding:11px 14px; font-size:13.5px; max-width:52vw; display:none }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:var(--over);
       margin-right:6px } .dot.on { background:var(--reuse) }
</style></head><body>
<header>
  <h1>Strata context monitor</h1>
  <div class="sub" id="topline">loading&hellip;</div>
  <div class="tabs">
    <button class="tab on" data-tab="monitor">1 &middot; Monitor</button>
    <button class="tab" data-tab="capture">2 &middot; Capture files</button>
    <button class="tab" data-tab="threads">3 &middot; Threads</button>
    <button class="tab" data-tab="context">4 &middot; Context</button>
  </div>
</header>
<main>
<section id="monitor" class="on">
  <div class="card">
    <div class="row">
      <button id="start" class="go">Start capture</button>
      <button id="stop" class="stopb">Stop capture</button>
      <span id="capstate" class="sub"></span>
    </div>
    <p>The capture copies every prompt your agents send to Strata and every answer Strata
       gives back, then passes it through untouched. Start it before you point an agent at
       it, stop it when you are done. It keeps every round until you delete one.</p>
  </div>
  <div class="grid" id="moncards"></div>
  <div class="card">
    <h2>Point an agent at the capture</h2>
    <pre id="agenturls"></pre>
    <p class="sub">Change the agent's base URL, restart the agent, and its rounds start
       appearing below. Strata itself keeps running at its own port.</p>
  </div>
  <div class="card">
    <h2>What just went by</h2>
    <pre id="tail">waiting&hellip;</pre>
  </div>
</section>

<section id="capture">
  <div class="card">
    <h2>What you are looking at</h2>
    <div id="nowview"></div>
  </div>
  <div class="card">
    <h2>Save it as a capture file</h2>
    <div class="row">
      <label>newest only <input id="savelast" size="5" placeholder="all"></label>
      <label>rounds <input id="saverounds" size="13" placeholder="1-20,45"></label>
      <label>threads <input id="savethreads" size="7" placeholder="A,B"></label>
      <label>note <input id="savenote" size="24" placeholder="what this capture is"></label>
      <label><input id="nopayloads" type="checkbox"> numbers only, no prompt text</label>
    </div>
    <div class="row">
      <button id="save" class="go">Save and open it</button>
      <button id="savepick">Save the rounds I picked in Threads</button>
      <button id="extract">Unpack to a log folder</button>
    </div>
    <p class="sub">A capture file is one zip holding the rounds, their answers, the full
       prompt text and a note about when it was taken and what is in it. Save one and you
       can clear the live log, or send the file to someone else, who opens it here.</p>
  </div>
  <div class="card">
    <h2>Capture files in <span class="mono" id="capdir"></span></h2>
    <div id="caplist"></div>
  </div>
</section>

<section id="threads">
  <div class="card">
    <h2>Pick rounds, then hide them or delete them</h2>
    <div class="row">
      <button id="selall">select every shown round</button>
      <button id="clearsel">clear selection</button>
      <button id="openall">open every thread list</button>
      <button id="hide" class="warn">hide from view</button>
      <button id="unhide">show hidden again</button>
      <button id="delfile" class="stopb">delete from the capture file</button>
    </div>
    <p>Hiding only changes what this page shows: the rounds stay in the log, and a hidden
       round is left out of the Context page too. Deleting rewrites the capture file
       without those rounds, and keeps the file as it was beside it. The live log cannot
       be edited while the capture is writing it - save a capture first.</p>
    <div id="hiddenbox"></div>
  </div>
  <div id="roundlist"></div>
  <div id="threadlist"></div>
</section>

<section id="context">
  <div class="card">
    <div class="row">
      <button id="build" class="go">Rebuild this page</button>
      <a href="/report" target="_blank"><button>Open full size in a new tab</button></a>
      <span class="sub" id="reportinfo"></span>
    </div>
    <p class="sub">The round page: the exact text that went out, what the model answered,
       the tokens Strata counted, and where each round came from. Rebuild it after you
       hide or delete rounds. The <b>show only</b> box above the round table filters the
       page down to one conversation.</p>
  </div>
  <iframe id="page" src="/report"></iframe>
</section>
</main>
<div id="toast"></div>
<script>
var picked = {};

function $(id) { return document.getElementById(id); }
function esc(s) { return ('' + (s == null ? '' : s)).replace(/[&<>"]/g, function (c) {
  return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
function num(n) { return (n == null || n === 0) ? '-' : Number(n).toLocaleString('en-US'); }
function toast(msg, bad) {
  var box = $('toast'); box.textContent = msg; box.style.display = 'block';
  box.style.borderColor = bad ? '#6d3040' : '#2f6b4d';
  clearTimeout(toast.t); toast.t = setTimeout(function () { box.style.display = 'none'; }, 5200);
}
function get(url) { return fetch(url).then(function (r) { return r.json(); }); }
function post(url, body) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body || {}) })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j && j.ok === false && j.error) { toast(j.error, true); } return j; });
}

document.querySelectorAll('.tab').forEach(function (b) {
  b.onclick = function () {
    document.querySelectorAll('.tab').forEach(function (x) { x.classList.remove('on'); });
    document.querySelectorAll('main > section').forEach(function (x) { x.classList.remove('on'); });
    b.classList.add('on'); $(b.dataset.tab).classList.add('on');
    refreshAll(false);
    if (b.dataset.tab === 'capture') { loadCaptures(); }
    if (b.dataset.tab === 'threads') { loadThreads(); loadRounds(); }
  };
});

$('start').onclick = function () {
  $('start').disabled = true; toast('starting the capture...');
  post('/api/capture/start').then(function (j) {
    $('start').disabled = false;
    if (j.ok && !j.already) { toast('capturing on port ' + j.state.port + ' -> ' + j.upstream); }
    else if (j.already) { toast('it was already capturing'); }
    refreshAll(false);
  });
};
$('stop').onclick = function () {
  $('stop').disabled = true;
  post('/api/capture/stop').then(function (j) {
    $('stop').disabled = false;
    if (j.ok) { toast('capture stopped'); }
    refreshAll(false);
  });
};

function bars(row, win) {
  var size = row.size || 0, scale = win || size || 1;
  var w = Math.min(100, 100 * size / scale), parts;
  if (!row.prompt) {
    parts = '<i class="est" style="width:' + w + '%"></i>';
  } else {
    var a = Math.min(100, 100 * (row.cached || 0) / scale);
    var b = Math.min(100, 100 * (size - (row.cached || 0)) / scale);
    parts = '<i style="width:' + a + '%"></i><i class="new" style="width:' + b + '%"></i>';
    if (size > scale) { parts += '<i class="over" style="width:2%"></i>'; }
  }
  return '<span class="bars">' + parts + '</span>';
}

function loadRounds() {
  get('/api/rounds?limit=40').then(function (d) {
    var html = '<div class="card"><h2>The ' + d.rows.length + ' newest rounds of '
      + d.of + ' shown</h2><table><thead><tr><th class="n">#</th><th>time</th><th>thread</th>'
      + '<th>api</th><th class="n">context</th><th class="n">read new</th><th class="n">answer</th>'
      + '<th>against the window</th><th class="n">http</th><th></th></tr></thead><tbody>';
    d.rows.forEach(function (r) {
      html += '<tr class="' + (r.hidden ? 'dim' : '') + '"><td class="n">' + r.seq
        + '</td><td>' + esc(r.ts) + '</td><td>' + esc(r.thread) + ' <span class="dim">'
        + esc(r.rel) + '</span></td><td>' + esc(r.api) + '</td><td class="n">'
        + num(r.prompt) + (r.prompt ? '' : num(r.est) + ' est')
        + '</td><td class="n">' + num(r.new) + '</td><td class="n">' + num(r.out)
        + '</td><td>' + bars(r, d.window) + '</td><td class="n">' + (r.status || '-')
        + '</td><td><a class="mono" href="#" data-jump="' + r.seq + '">open</a></td></tr>';
    });
    $('roundlist').innerHTML = html + '</tbody></table>'
      + (d.hidden.length ? '<p class="sub">' + d.hidden.length + ' rounds hidden from this view'
        + ' - undo them in the card above</p>' : '') + '</div>';
  });
}

function refreshMonitor(state) {
  var cap = state.capture, src = state.source, cnt = src.counts || {};
  $('capstate').innerHTML = '<span class="dot ' + (cap.running ? 'on' : '') + '"></span>'
    + (cap.running ? 'capturing on port ' + cap.port + ' &rarr; ' + esc(cap.upstream || '?')
                   : 'not capturing')
    + (cap.seen != null ? ' &middot; ' + num(cap.seen) + ' requests through' : '');
  $('start').disabled = cap.running; $('stop').disabled = !cap.running;
  var win = src.window || 0;
  $('moncards').innerHTML =
      '<div class="card kv">viewing<b>' + (src.live ? 'the live log' : 'a capture file')
    + '</b>' + num(src.rounds) + ' rounds in ' + num(src.threads) + ' conversations'
    + (src.hidden.length ? ' (' + src.hidden.length + ' hidden)' : '') + '</div>'
    + '<div class="card kv">live log right now<b>' + num(state.live_rounds) + ' rounds</b>'
    + esc(state.paths.log) + '</div>'
    + '<div class="card kv">biggest context<b>' + num(cnt.peak) + ' tok</b>'
    + (cnt.peak_seq ? 'round #' + cnt.peak_seq
        + (cnt.pct != null ? ' &middot; ' + cnt.pct + '% of the ' + num(win) + ' window' : '') : '')
    + '</div>'
    + '<div class="card kv">prompt tokens<b>' + num(cnt.prompt) + ' counted</b>'
    + num(cnt.cached) + ' reused &middot; ' + num(cnt.prompt - cnt.cached) + ' read new'
    + '</div>';
  var strata = (cap.strata && cap.strata[0]) ? cap.strata[0] : null;
  $('agenturls').textContent =
      'OpenAI-shaped agents   ' + state.paths.agent_base + '/v1\n'
    + 'Claude Code            ANTHROPIC_BASE_URL=' + state.paths.agent_base + '\n'
    + 'Codex                  OPENAI_BASE_URL=' + state.paths.agent_base + '/v1\n'
    + 'Strata answers at      ' + (strata ? strata.url + '  (model ' + strata.model + ')'
                                          : 'not found - is the model running?');
  $('topline').innerHTML = (cap.running
      ? '<span class="dot on"></span>capturing into ' + esc(state.paths.log)
      : '<span class="dot"></span>capture stopped')
    + ' &middot; viewing ' + (src.live ? 'the live log' : 'a capture file')
    + ' &middot; ' + num(src.rounds) + ' rounds, ' + num(src.threads) + ' conversations'
    + (win ? ' &middot; window ' + num(win) + ' tokens' : '');
  var info = state.report || {};
  $('reportinfo').textContent = info.built
    ? 'built ' + new Date(info.built * 1000).toLocaleTimeString() + ' - ' + info.rounds + ' rounds'
    : 'not built yet';
}

function refreshTail() {
  get('/api/log?tail=16').then(function (d) {
    $('tail').textContent = (d.lines || []).map(function (l) {
      return l.ts + '  #' + l.seq + (l.type === 'response' ? '  ->  ' : '  <-  ') + l.summary;
    }).join('\n') || 'nothing in this source yet';
  });
}

function refreshCapture(state) {
  var src = state.source;
  $('nowview').innerHTML = '<div class="row"><span class="pill">'
    + (src.live ? 'live log folder' : 'capture file') + '</span><span class="mono">'
    + esc(src.path) + '</span>'
    + (src.live ? '' : '<button data-open="live">back to the live log</button>')
    + '<button data-reveal="' + esc(src.path) + '">show in folder</button></div>'
    + '<p class="sub">' + num(src.shown) + ' of ' + num(src.rounds) + ' rounds shown'
    + (src.hidden.length ? ' (' + src.hidden.length + ' hidden)' : '') + ' &middot; '
    + num(src.threads) + ' conversations &middot; window '
    + num(src.window) + ' (' + esc(src.window_src || 'not known') + ')</p>'
    + (src.manifest ? '<pre>saved     ' + esc(src.created || '?')
        + (src.edited ? '\nedited    ' + esc(src.edited) : '')
        + '\nselection ' + esc(src.selection || '-')
        + (src.note ? '\nnote      ' + esc(src.note) : '')
        + (src.removed && src.removed.length
            ? '\ndeleted   ' + src.removed.length + ' rounds were removed from this file' : '')
        + '\nsize      ' + ((src.bytes || 0) / 1024).toFixed(1) + ' KB</pre>' : '');
  $('capdir').textContent = state.paths.captures;
  bindButtons();
}

function loadCaptures() {
  get('/api/captures').then(function (d) {
    if (!d.captures.length) {
      $('caplist').innerHTML = '<p class="sub">no capture files here yet. Save one above and '
        + 'it will appear here, so you can open it again or hand the file to someone else.</p>';
      return;
    }
    var html = '<table><thead><tr><th>file</th><th>taken</th><th class="n">rounds</th>'
      + '<th>what is in it</th><th>note</th><th class="n">size</th><th></th></tr></thead><tbody>';
    d.captures.forEach(function (c) {
      var m = c.manifest || {};
      html += '<tr><td class="mono">' + esc(c.name) + '</td><td>'
        + esc((m.created || '').replace('T', ' ').slice(0, 19)) + '</td><td class="n">'
        + num(m.rounds) + '</td><td class="dim">' + esc(m.selection || '')
        + '</td><td class="dim">' + esc(m.note || '') + '</td><td class="n">'
        + (c.bytes / 1024).toFixed(0) + ' KB</td><td><button data-open="' + esc(c.path)
        + '">open</button> <button data-reveal="' + esc(c.path) + '">show</button></td></tr>';
    });
    $('caplist').innerHTML = html + '</tbody></table>';
    bindButtons();
  });
}

function bindButtons() {
  document.querySelectorAll('[data-open]').forEach(function (b) {
    if (b.bound) { return; } b.bound = 1;
    b.onclick = function () {
      post('/api/open', { path: b.dataset.open }).then(function (j) {
        if (j.ok) { toast(j.live ? 'now viewing the live log' : 'now viewing ' + j.source);
          picked = {}; refreshAll(true); loadThreads(); loadRounds(); }
      });
    };
  });
  document.querySelectorAll('[data-reveal]').forEach(function (b) {
    if (b.bound) { return; } b.bound = 1;
    b.onclick = function () { post('/api/reveal', { path: b.dataset.reveal }); };
  });
  document.querySelectorAll('[data-jump]').forEach(function (b) {
    if (b.bound) { return; } b.bound = 1;
    b.onclick = function (ev) {
      ev.preventDefault();
      document.querySelector('.tab[data-tab=context]').click();
      $('page').src = '/report?t=' + Date.now() + '#r' + b.dataset.jump;
    };
  });
}

function loadThreads() {
  get('/api/threads').then(function (d) {
    var html = '';
    d.threads.forEach(function (t) {
      html += '<div class="card"><div class="row"><h2 style="margin:0">thread ' + esc(t.name)
        + '</h2><span class="pill">' + t.count + ' rounds</span>'
        + '<span class="mono dim">#' + t.first + ' &rarr; #' + t.last + '</span>'
        + '<span class="dim">' + esc(t.client || 'client not named') + '</span>'
        + '<button class="toggle" data-toggle="t' + t.id + '">its ' + t.count + ' rounds</button>'
        + '</div><p class="sub">root prompt ' + num(t.root_chars) + ' chars <span class="pill">'
        + esc((t.root_fp || '').slice(0, 8)) + '</span> &middot; ' + t.n_tools + ' tools '
        + '<span class="pill">' + esc((t.tools_fp || '').slice(0, 8)) + '</span>'
        + (t.notes.length ? '<br>' + esc(t.notes.join(' - ')) : '') + '</p>'
        + '<div class="chips" id="t' + t.id + '" hidden>';
      t.rounds.forEach(function (r) {
        html += '<span class="chip ' + (r.hidden ? 'hid ' : '') + (r.root_changed ? 'warn' : '')
          + '" data-seq="' + r.seq + '" title="' + esc(r.api + ' - ' + num(r.size)
            + (r.exact ? ' tokens' : ' estimated tokens')
            + (r.parent ? ' - continues #' + r.parent + ' (+' + r.added + ' messages)'
                        : ' - a new conversation')
            + (r.same_history.length ? ' - same history as #' + r.same_history.join(', #') : '')
            + (r.root_changed ? ' - the root prompt changed here' : '')) + '">'
          + '<input type="checkbox" ' + (picked[r.seq] ? 'checked' : '') + '>' + r.seq
          + ' <span class="dim">' + esc(r.ts) + '</span></span>';
      });
      html += '</div></div>';
    });
    if (!d.threads.length) { html = '<div class="card"><p>nothing to group yet</p></div>'; }
    $('threadlist').innerHTML = html;
    var hidden = d.hidden || [];
    $('hiddenbox').innerHTML = hidden.length
      ? '<p>Rounds this page is hiding: ' + hidden.map(function (s) { return '<span class="chip hid">'
          + s + '</span>'; }).join('') + '</p>'
      : '';
    document.querySelectorAll('#threadlist .chip').forEach(function (c) {
      var box = c.querySelector('input');
      c.onclick = function (ev) {
        var seq = Number(c.dataset.seq);
        if (ev.target !== box) { box.checked = !box.checked; }
        if (box.checked) { picked[seq] = true; } else { delete picked[seq]; }
      };
    });
    document.querySelectorAll('[data-toggle]').forEach(function (b) {
      b.onclick = function () {
        var box = document.getElementById(b.dataset.toggle);
        box.hidden = !box.hidden;
        b.textContent = box.hidden ? b.dataset.label : 'hide the list';
      };
      b.dataset.label = b.textContent;
    });
    $('delfile').disabled = !d.source_is_file;
    $('delfile').title = d.source_is_file ? ''
      : 'open a capture file first: the live log is still being written by the capture';
  });
}

$('openall').onclick = function () {
  var panels = document.querySelectorAll('#threadlist .chips');
  var open = $('openall').dataset.open !== '1';
  panels.forEach(function (box) { box.hidden = !open; });
  document.querySelectorAll('[data-toggle]').forEach(function (b) {
    b.textContent = open ? 'hide the list' : b.dataset.label; });
  $('openall').dataset.open = open ? '1' : '';
  $('openall').textContent = open ? 'close every thread list' : 'open every thread list';
};

function chosen() { return Object.keys(picked).filter(function (k) { return picked[k]; }).map(Number); }

$('selall').onclick = function () {
  document.querySelectorAll('#threadlist .chip').forEach(function (c) {
    c.querySelector('input').checked = true; picked[Number(c.dataset.seq)] = true; });
  toast(chosen().length + ' rounds selected');
};
$('clearsel').onclick = function () { picked = {}; loadThreads(); };
$('hide').onclick = function () {
  var seqs = chosen();
  if (!seqs.length) { return toast('pick some rounds first', true); }
  var left = document.querySelectorAll('#threadlist .chip:not(.hid)').length - seqs.length;
  if (left <= 0 && !confirm('This hides every round in this source: the tables would be empty. '
      + 'Hide them all anyway?')) { return; }
  post('/api/hide', { seqs: seqs }).then(function (j) {
    if (j.ok) { toast(seqs.length + ' rounds hidden from view only'); picked = {};
      refreshAll(true); loadThreads(); loadRounds(); }
  });
};
$('unhide').onclick = function () {
  post('/api/hide', { clear: true }).then(function (j) {
    if (j.ok) { toast('every hidden round is back'); refreshAll(true); loadThreads(); loadRounds(); }
  });
};
$('delfile').onclick = function () {
  var seqs = chosen();
  if (!seqs.length) { return toast('pick the rounds to delete first', true); }
  if (!confirm('Delete ' + seqs.length + ' rounds from this capture file? The file as it is now '
      + 'will be kept beside it.')) { return; }
  post('/api/delete', { seqs: seqs }).then(function (j) {
    if (j.ok) {
      toast('deleted ' + j.removed.length + ' rounds, ' + j.rounds + ' left. Previous version: '
        + String(j.backup || '').split(/[\\/]/).pop());
      picked = {}; refreshAll(true); loadThreads(); loadRounds();
    }
  });
};

$('save').onclick = function () {
  post('/api/save', { last: $('savelast').value, rounds: $('saverounds').value,
                      threads: $('savethreads').value, note: $('savenote').value,
                      no_payloads: $('nopayloads').checked }).then(function (j) {
    if (j.ok) { toast('saved ' + j.name + ' with ' + j.rounds + ' rounds, and opened it');
      picked = {}; refreshAll(true); loadCaptures(); loadThreads(); loadRounds(); }
  });
};
$('savepick').onclick = function () {
  var seqs = chosen();
  if (!seqs.length) { return toast('pick the rounds in the Threads tab first', true); }
  post('/api/save', { pick: seqs, note: $('savenote').value }).then(function (j) {
    if (j.ok) { toast('saved ' + j.name + ' with the ' + j.rounds + ' rounds you picked');
      picked = {}; refreshAll(true); loadCaptures(); loadThreads(); }
  });
};
$('extract').onclick = function () {
  post('/api/extract', {}).then(function (j) {
    if (j.ok) { toast('unpacked ' + j.payloads + ' payload files into ' + j.to); }
  });
};
$('build').onclick = function () {
  $('build').disabled = true; toast('building the round page...');
  post('/api/report/build').then(function (j) {
    $('build').disabled = false;
    if (j.ok) { toast('rebuilt with ' + j.rounds + ' rounds');
      $('page').src = '/report?t=' + Date.now(); refreshAll(false); }
  });
};

function refreshAll(reloadFrame) {
  get('/api/state').then(function (state) {
    refreshMonitor(state);
    if (document.querySelector('.tab.on').dataset.tab === 'capture') { refreshCapture(state); }
    if (reloadFrame) { $('page').src = '/report?t=' + Date.now(); }
  }).catch(function () { });
}
refreshAll(false);
refreshTail();
setInterval(function () {
  if (document.querySelector('.tab.on').dataset.tab === 'monitor') { refreshTail(); refreshAll(false); }
}, 4000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="the Strata context monitor, as a web page")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CTX_WEB_PORT") or 8790),
                    help="the page's own port (default 8790)")
    ap.add_argument("--proxy-port", type=int, default=CFG["proxy_port"],
                    help="the port the capture listens on for agents (default 8788)")
    ap.add_argument("--upstream", default=CFG["upstream"],
                    help="Strata's URL; leave blank to find it by itself")
    ap.add_argument("--log", default=CFG["log_dir"], help="folder the capture writes into")
    ap.add_argument("--captures", default=CFG["captures"], help="where capture files live")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser window")
    args = ap.parse_args()

    CFG["proxy_port"] = args.proxy_port
    CFG["upstream"] = args.upstream
    CFG["log_dir"] = os.path.abspath(args.log)
    CFG["captures"] = os.path.abspath(args.captures)
    os.makedirs(CFG["web_dir"], exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("context monitor page: %s" % url)
    print("  capture port %d for agents, log %s, capture files %s"
          % (CFG["proxy_port"], CFG["log_dir"], CFG["captures"]))
    print("Ctrl+C closes the page. A capture that is already running keeps running.")
    if not args.no_open:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url])
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
