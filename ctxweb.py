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
_manual_upstream = False              # --upstream / CTX_UPSTREAM pinned the URL
_lock = threading.Lock()
_view_cache = {}
_upstream_cache = {"at": 0.0, "list": []}

# The ports Strata has been seen answering on: 47831 is the app's own pick, 8080 is
# the fixed-port run config. The app re-picks its port every start, so discovery
# tries these first and still falls back to probing the other loopback listeners.
KNOWN_UPSTREAM_PORTS = (47831, 8080)
DEFAULT_UPSTREAM = "http://127.0.0.1:8080/v1"

_status_log = []                      # (time, text) lines for the page's status window
_status_lock = threading.Lock()


def log_note(text):
    """One line in the status and discovery log the page shows."""
    with _status_lock:
        _status_log.append([time.strftime("%H:%M:%S"), text])
        if len(_status_log) > 300:
            del _status_log[:-300]


def status_log():
    with _status_lock:
        return list(_status_log)


# --------------------------------------------------------------- finding things

def _get(url, timeout=2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except Exception:
        return None, None


def proxy_state(port=None):
    """What the capture says about itself, or None when nothing answers on its port."""
    port = port or CFG["proxy_port"]
    status, text = _get("http://127.0.0.1:%d/ctx-stats" % port)
    if status != 200:
        return None
    try:
        info = json.loads(text)
    except ValueError:
        return None
    info["port"] = port
    info["ours"] = _proc is not None and _proc.poll() is None
    return info


def listening_ports(any_address=False):
    """Loopback TCP ports that are listening, as (port, owning pid). With any_address,
    also count a listener that holds the port on a wildcard address (0.0.0.0 or [::]),
    which still gets in its way: Windows lets a loopback bind sit on top of one."""
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
        loopback = local.startswith("127.0.0.1:")
        wildcard = (local.startswith("0.0.0.0:") or local.startswith("[::]:")
                    or local.startswith(":::"))
        if not (loopback or (any_address and wildcard)):
            continue
        try:
            out.append((int(local.rsplit(":", 1)[1]), int(parts[4])))
        except ValueError:
            continue
    return out


def answers_models(text):
    """Does this /v1/models body come from an OpenAI-shaped model server? Match on the
    shape, not on the word "strata": a real Strata run names its model after its run
    config (strata-iq3_s), and the reverted #17 showed a name match is not enough."""
    norm = re.sub(r"\s+", "", text or "").lower()
    return ('"object":"list"' in norm and '"data":[' in norm) or '"strata' in norm


def probe_upstream_port(port):
    """One loopback port: the capture answers /ctx-stats, Strata answers /v1/models.
    Returns a candidate for Strata, or None when this port is not it."""
    status, text = _get("http://127.0.0.1:%d/ctx-stats" % port, timeout=1.2)
    if status == 200 and '"proxy"' in (text or ""):
        return None                               # that is the capture, not Strata
    status, text = _get("http://127.0.0.1:%d/v1/models" % port, timeout=2)
    if status != 200 or not answers_models(text):
        return None
    match = re.search(r'"id"\s*:\s*"([^"]+)"', text or "")
    return {"port": port, "model": match.group(1) if match else "?",
            "url": "http://127.0.0.1:%d/v1" % port}


def discover_upstream(quiet=False):
    """Look for Strata: the ports it has been seen on first, then every other loopback
    listener. Each try goes into the status log unless quiet."""
    found = []
    for port in KNOWN_UPSTREAM_PORTS:
        hit = probe_upstream_port(port)
        if hit:
            if not quiet:
                log_note("found Strata on port %d (model %s)" % (port, hit["model"]))
            found.append(hit)
        elif not quiet:
            log_note("nothing answering like Strata on port %d" % port)
    if not found:
        others = [p for p in sorted(set(p for p, _ in listening_ports()))
                  if p not in KNOWN_UPSTREAM_PORTS]
        if others and not quiet:
            log_note("trying the other %d loopback ports..." % len(others))
        for port in others:
            hit = probe_upstream_port(port)
            if hit:
                if not quiet:
                    log_note("found Strata on port %d (model %s)" % (port, hit["model"]))
                found.append(hit)
    _upstream_cache.update(at=time.time(), list=found)
    return found


def find_upstream(again=False):
    """Strata's own URL, cached for a few seconds because the page asks often. The
    cached answer may be up to 15 seconds old; Auto Discover asks for a fresh probe."""
    now = time.time()
    if not again and now - _upstream_cache["at"] < 15:
        return _upstream_cache["list"]
    return discover_upstream(quiet=True)


def upstream_port(url):
    """The port a Strata URL points at - the path is cosmetic, the proxy uses host:port."""
    match = re.search(r"://[^/:]*:(\d+)", url or "")
    return int(match.group(1)) if match else None


def upstream_loopback(url):
    """Is this Strata URL on this machine? Only then can the sweep say anything about it."""
    match = re.search(r"://([^/:]+)", url or "")
    return bool(match) and match.group(1) in ("127.0.0.1", "localhost", "[::1]", "::1")


def upstream_answering(url, strata):
    """Does the URL the capture forwards to look like a live Strata? The discovered
    loopback list answers that for free. A Strata on another machine is outside the
    sweep, so there is no opinion - and no warning - about it."""
    if not url:
        return False
    target = resolve_upstream_url(url)
    if any(resolve_upstream_url(s["url"]) == target for s in strata):
        return True
    return not upstream_loopback(target)


# ------------------------------------------------------------- capture start/stop

def resolve_upstream_url(raw):
    """A Strata URL typed into the page: plain http, no credentials, no query. A bare
    host:port gets /v1 on the end, so it matches what discovery hands back. Raises
    ValueError for anything else."""
    value = (raw or "").strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("the Strata URL has an invalid port") from None
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("enter a plain http Strata URL, for example "
                         "http://127.0.0.1:47831/v1")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("the Strata URL port has to be between 1 and 65535")
    path = parsed.path.rstrip("/")
    if path in ("", "/v1"):
        path = "/v1"
    return "http://%s%s" % (parsed.netloc, path)


def pick_upstream(payload):
    """Where the capture forwards: the URL in the page's URL box, else the one this
    page was started with, else a discovered Strata. A URL found earlier does not
    count - Strata moves ports when it restarts, so look again. Raises ValueError
    when there is nothing to forward to."""
    raw = (payload or {}).get("upstream")
    if raw is not None and str(raw).strip():
        return resolve_upstream_url(str(raw))
    if _manual_upstream and CFG["upstream"]:
        return CFG["upstream"]
    log_note("looking for Strata...")
    candidates = find_upstream(again=True)
    if not candidates:
        log_note("Strata is not answering on loopback")
        raise ValueError("Strata is not answering on loopback - start the model first, "
                         "then start the capture, or type its URL in the box.")
    log_note("found Strata at %s" % candidates[0]["url"])
    return candidates[0]["url"]


def start_capture(payload=None):
    global _proc
    # the capture listens on the port this page was started with (--proxy-port, or the
    # launcher's -CapturePort); the page does not offer to move it
    port = CFG["proxy_port"]
    # One start at a time, guards and launch together: two overlapping starts that both
    # decide nothing is running would each leave a capture process behind the page cannot
    # see. Stop capture does not take this lock, so it still works during a slow start.
    with _lock:
        if _proc is not None and _proc.poll() is None:
            state = proxy_state(port) or {"port": port, "ours": True,
                                          "upstream": CFG["upstream"]}
            return {"ok": True, "already": True, "state": state}
        state = proxy_state(port)
        if state:                                  # capturing there, started elsewhere
            return {"ok": True, "already": True, "state": state}
        if port in {p for p, _ in listening_ports(any_address=True)}:
            return {"ok": False, "error": "port %d is taken by something else on this "
                                          "machine - start the page with --proxy-port on "
                                          "another port, or free this one first" % port}
        try:
            upstream = pick_upstream(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        # what the URL box said wins over discovery, so say when nothing is answering there
        up_port = upstream_port(upstream)
        if up_port and upstream_loopback(upstream) and probe_upstream_port(up_port) is None:
            log_note("nothing answering like Strata at %s - forwarding there anyway" % upstream)
        os.makedirs(CFG["log_dir"], exist_ok=True)
        command = [sys.executable, os.path.join(HERE, "context_proxy.py"),
                   "--port", str(port), "--upstream", upstream,
                   "--dir", CFG["log_dir"]]
        console = open(os.path.join(CFG["log_dir"], "logger-out.txt"), "ab")
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # detached, own process group
        _proc = subprocess.Popen(command, cwd=HERE, stdout=console, stderr=console,
                                 creationflags=flags)
        # Adopted as soon as it is launched, not only once it answers: a capture that is
        # slow to bind, or that never does, is still this page's to stop.
        CFG["upstream"] = upstream
        for _ in range(40):
            time.sleep(0.25)
            state = proxy_state(port)
            if state:
                log_note("capturing on port %d -> %s" % (port, upstream))
                return {"ok": True, "pid": _proc.pid, "upstream": upstream, "state": state}
        log_note("the capture is on port %d but is not answering yet" % port)
        return {"ok": False, "error": "the capture is on port %d but is not answering yet - "
                                      "press Stop capture and start it again; "
                                      "logs\\logger-out.txt says what it reported" % port}


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
        for port, pid in listening_ports(any_address=True):
            if port == CFG["proxy_port"] and pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                stopped.append(pid)
    if proxy_state():
        log_note("something still answers on port %d, so it is still capturing"
                 % CFG["proxy_port"])
        return {"ok": False, "error": "something still answers on port %d, so it is still "
                                      "capturing" % CFG["proxy_port"]}
    log_note("capture stopped")
    return {"ok": True, "stopped": stopped}


def ensure_live_log():
    """The page shows its status and discovery log from the first second, before any
    capture has written anything: an empty live log is a log with zero rounds."""
    os.makedirs(CFG["log_dir"], exist_ok=True)
    path = os.path.join(CFG["log_dir"], "requests.jsonl")
    if not os.path.exists(path):
        open(path, "a").close()


def startup_discovery():
    """What the page does the moment it starts: look for Strata on the ports it has
    been seen on, and when it finds one - and nothing is capturing yet - start
    capturing straight away. Everything it tries lands in the status log."""
    log_note("page started; looking for Strata on ports %s"
             % " and ".join(str(p) for p in KNOWN_UPSTREAM_PORTS))
    found = discover_upstream()
    if not found:
        log_note("nothing found - the URL box defaults to %s; start the model and press "
                 "Auto Discover, or start the capture against that URL" % DEFAULT_UPSTREAM)
        return
    if proxy_state():
        log_note("a capture is already answering on port %d; leaving it alone"
                 % CFG["proxy_port"])
        return
    log_note("Strata found - starting the capture -> %s" % found[0]["url"])
    result = start_capture({"upstream": found[0]["url"]})
    if not result.get("ok"):
        log_note("the capture did not start: %s" % result.get("error"))


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
    if not CFG["source"]:
        ensure_live_log()          # the live view works before the first round lands
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
    strata = find_upstream()
    cap_upstream = ((proxy or {}).get("upstream") or CFG["upstream"] or "").strip()
    # is the URL the capture forwards to actually answering like Strata? the
    # discovered list is cached, so this costs no extra probes per poll
    cap_ok = upstream_answering(cap_upstream, strata)
    return {
        "source": describe_source(cap, reqs, resps, order, shown, hidden, links),
        "capture": {
            "running": bool(proxy), "port": CFG["proxy_port"],
            "upstream": cap_upstream, "upstream_ok": cap_ok,
            "seen": (proxy or {}).get("requests_seen"), "we_started": (proxy or {}).get("ours"),
            "strata": strata,
        },
        "upstream": {
            # what the URL box shows: a manual/launch URL wins, then a discovered
            # Strata, then the default the issue asks for when nothing is found
            "url": CFG["upstream"] or (strata[0]["url"] if strata else DEFAULT_UPSTREAM),
            "default": DEFAULT_UPSTREAM,
            "manual": _manual_upstream,
        },
        "status_log": status_log(),
        "paths": {"log": CFG["log_dir"], "captures": CFG["captures"], "report": report,
                  "agent_base": "http://127.0.0.1:%d" % CFG["proxy_port"]},
        "live_rounds": log_rounds() if cap.kind != "dir" else len(order),
        "report": {"built": int(os.path.getmtime(report)) if os.path.exists(report) else None,
                   "rounds": len(shown)},
    }


def api_discover(payload=None):
    """Auto Discover: probe for Strata now, logging every port it tries."""
    found = discover_upstream()
    return {"ok": True, "strata": found,
            "error": None if found else "Strata is not answering on loopback"}


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
                "user_head": link.get("user_head") or "", "user_prompt": link.get("user_prompt") or "",
                "user_chars": link.get("user_chars") or 0,
                "same_history": link.get("same_history") or [], "hidden": seq in hidden,
            })
        out.append({"name": thread["name"], "id": thread["id"], "rounds": rounds,
                    "count": len(thread["rounds"]), "first": thread["first"], "last": thread["last"],
                    "client": first.get("client") or "", "root_chars": thread["root_chars"],
                    "root_fp": thread["root_fp"], "root_head": thread["root_head"],
                    "open_head": thread["open_head"], "open_chars": thread["open_chars"],
                    "open_prompt": thread["open_prompt"],
                    "n_tools": thread["n_tools"], "tools_fp": thread["tools_fp"], "notes": notes})
    return {"threads": out, "hidden": sorted(hidden), "source_is_file": cap.kind == "file",
            "rounds": len(order), "shown": len(shown)}


# ------------------------------------------------------------------------ actions

def api_save(payload):
    """Save some of the rounds being viewed into a new capture file."""
    source = ctxstore.Capture(CFG["log_dir"]) if payload.get("from") == "live" else open_source()
    reqs, resps, order = ctxstore._events(source)
    try:
        chosen = ctxstore.choose(reqs, order, last=int(payload.get("last") or 0),
                                 rounds=str(payload.get("rounds") or ""),
                                 since=str(payload.get("since") or ""),
                                 until=str(payload.get("until") or ""),
                                 threads=str(payload.get("threads") or ""), cap=source)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
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
                if not os.path.exists(report):
                    return self._send(200, "<p>No rounds captured yet. Point an agent at the "
                                           "proxy URL on tab 1 and the report appears here.</p>",
                                      "text/html")
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
        zero_arg = ("/api/capture/stop", "/api/report/build")
        actions = {
            "/api/capture/start": start_capture, "/api/capture/stop": stop_capture,
            "/api/discover": api_discover,
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
.uprompt { cursor:help; border-bottom:1px dotted #4b5872 }
#hoverbox { position:fixed; z-index:60; display:none; max-width:min(700px,86vw); max-height:56vh;
            overflow:auto; background:#10151d; border:1px solid var(--line); border-radius:10px;
            padding:11px 13px; font-family:var(--mono); font-size:12.5px; line-height:1.5;
            color:#dbe2ee; white-space:pre-wrap; word-break:break-word; pointer-events:none;
            box-shadow:0 12px 34px rgba(0,0,0,.55) }
#hoverbox b { display:block; color:var(--dim); font-size:11px; text-transform:uppercase;
              letter-spacing:.05em; margin-bottom:6px }
.card h2 { white-space:nowrap }
pre { font-family:var(--mono); font-size:12.5px; background:var(--bg); border:1px solid var(--line);
      border-radius:8px; padding:9px 11px; overflow:auto; margin:6px 0; white-space:pre-wrap }
.bars { height:14px; border-radius:4px; background:var(--panel2); overflow:hidden; display:flex;
        min-width:110px }
.bars i { display:block; height:100%; background:var(--reuse) }
.bars i.new { background:var(--new) } .bars i.est { background:#5a6479 } .bars i.over { background:var(--over) }
iframe { width:100%; height:74vh; border:1px solid var(--line); border-radius:10px; background:#fff }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:8px 0 }
.monitor { display:flex; gap:14px; align-items:stretch; flex-wrap:wrap }
.monitor .controls { flex:1 1 480px }
.logbox { flex:1 1 300px; max-height:380px; min-height:120px; overflow:auto; margin:0;
          font-size:12px; line-height:1.55 }
.logbox .st { color:var(--ink); font-weight:600 }
.statusbox { flex:1 1 220px; border:1px solid var(--line); border-radius:6px;
             padding:6px 10px; font-family:var(--mono); font-size:13px;
             background:var(--panel) }
.statusbox.off { color:var(--dim) }
.statusbox.warn { color:var(--new); border-color:#5a4a2a }
.statusbox.warn .dot.on { background:var(--new) }
.copybtn { padding:3px 10px; font-size:12.5px }
code { font-family:var(--mono); font-size:12.5px; background:var(--bg);
       border:1px solid var(--line); border-radius:6px; padding:2px 6px }
.aghead { margin:12px 0 3px; font-size:12px; color:var(--dim); text-transform:uppercase;
          letter-spacing:.06em }
.agents .agline { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:3px 0 }
.agents .aglabel { color:var(--dim); font-size:12.5px; min-width:126px }
.agents code { font-size:12px; padding:1px 5px }
.agents .aghint { color:var(--dim); font-size:12px }
.agents .copybtn { padding:2px 8px; font-size:12px }
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
    <div class="monitor">
      <div class="controls">
        <div class="row">
          <button id="start" class="go">Start capture</button>
          <button id="stop" class="stopb">Stop capture</button>
          <span id="capstate" class="statusbox"></span>
        </div>
        <div class="row">
          <label>Strata URL <input id="upurl" size="26" class="mono"
                                   placeholder="http://127.0.0.1:8080/v1"></label>
          <button id="discover">Auto Discover</button>
        </div>
        <div class="row">
          <span class="sub">monitor page</span>
          <a id="pageurl" class="mono" href="/"></a>
        </div>
        <h3 class="aghead">Agent setting</h3>
        <div id="agenturls" class="agents"></div>
      </div>
      <pre id="statuslog" class="logbox">starting&hellip;</pre>
    </div>
  </div>
  <div class="grid" id="moncards"></div>
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
      <label>threads <input id="savethreads" size="20" placeholder="names, e.g. build-failing-node"></label>
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
var upurlTouched = false;
var lastLog = '';

function $(id) { return document.getElementById(id); }
function esc(s) { return ('' + (s == null ? '' : s)).replace(/[&<>"]/g, function (c) {
  return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
function num(n) { return (n == null || n === 0) ? '-' : Number(n).toLocaleString('en-US'); }
function toast(msg, bad) {
  var box = $('toast'); box.textContent = msg; box.style.display = 'block';
  box.style.borderColor = bad ? '#6d3040' : '#2f6b4d';
  clearTimeout(toast.t); toast.t = setTimeout(function () { box.style.display = 'none'; }, 5200);
}
// one floating box for every [data-full]: the whole prompt, only while you hover it
var hover = document.createElement('div');
hover.id = 'hoverbox';
document.body.appendChild(hover);
function placeHover(ev) {
  var pad = 16, w = hover.offsetWidth, h = hover.offsetHeight;
  var x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > window.innerWidth - 10) { x = Math.max(10, ev.clientX - w - pad); }
  if (y + h > window.innerHeight - 10) { y = Math.max(10, ev.clientY - h - pad); }
  hover.style.left = x + 'px'; hover.style.top = y + 'px';
}
document.addEventListener('mouseover', function (e) {
  var el = e.target.closest ? e.target.closest('[data-full]') : null;
  if (!el) { return; }
  hover.innerHTML = '';
  var cap = document.createElement('b');
  cap.textContent = el.getAttribute('data-label') || 'the whole prompt';
  hover.appendChild(cap);
  hover.appendChild(document.createTextNode(el.getAttribute('data-full')));
  hover.style.display = 'block';
  placeHover(e);
});
document.addEventListener('mousemove', function (e) {
  if (hover.style.display === 'block') { placeHover(e); }
});
document.addEventListener('mouseout', function (e) {
  var el = e.target.closest ? e.target.closest('[data-full]') : null;
  if (el && (!e.relatedTarget || !el.contains(e.relatedTarget))) { hover.style.display = 'none'; }
});
window.addEventListener('scroll', function () { hover.style.display = 'none'; }, true);
function get(url) { return fetch(url).then(function (r) { return r.json(); }); }
function post(url, body) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(body || {}) })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j && j.ok === false && j.error) { toast(j.error, true); } return j; });
}
function copyText(text, ok) {
  function fallback() {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    var done = false;
    try { done = document.execCommand('copy'); } catch (e) { done = false; }
    document.body.removeChild(ta);
    toast(done ? 'copied: ' + text : 'could not copy; select it by hand', !done);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { toast(ok || 'copied: ' + text); },
                                            fallback);
  } else { fallback(); }
}
document.addEventListener('click', function (e) {
  var b = e.target.closest ? e.target.closest('.copybtn') : null;
  if (!b) { return; }
  var src = b.dataset.copy ? document.querySelector(b.dataset.copy) : null;
  var text = (src || b.closest('.agline')).querySelector('code').textContent;
  copyText(text);
});

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

$('upurl').oninput = function () { upurlTouched = true; };
$('discover').onclick = function () {
  $('discover').disabled = true; toast('looking for Strata on its usual ports...');
  post('/api/discover', {}).then(function (j) {
    $('discover').disabled = false;
    var s = j.strata && j.strata[0];
    if (s) {
      $('upurl').value = s.url; upurlTouched = false;
      toast('found Strata on port ' + s.port + ' (model ' + s.model + ')');
    } else {
      toast('Strata was not found - start the model, or type its URL in the box', true);
    }
    refreshAll(false);
  }).catch(function () {
    $('discover').disabled = false;
    toast('could not look for Strata; the page could not answer', true);
  });
};
$('start').onclick = function () {
  $('start').disabled = true; toast('starting the capture...');
  post('/api/capture/start', { upstream: $('upurl').value })
    .then(function (j) {
      $('start').disabled = false;
      if (j.ok && !j.already) {
        // the box shows the Strata the capture is really forwarding to now
        if (j.upstream) { $('upurl').value = j.upstream; upurlTouched = false; }
        toast('capturing on port ' + j.state.port + ' -> ' + j.upstream);
      }
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

function refreshStatusLog(state) {
  var cap = state.capture;
  var status = cap.running
    ? 'STATUS: capturing on port ' + cap.port + ' -> ' + (cap.upstream || '?')
      + ' | ' + (cap.seen || 0) + ' requests through'
    : 'STATUS: not capturing';
  var lines = (state.status_log || []).slice().reverse()
    .map(function (l) { return l[0] + '  ' + l[1]; });
  var text = status + '\n' + lines.join('\n');
  if (text === lastLog) { return; }
  lastLog = text;
  var box = $('statuslog');
  box.innerHTML = '<span class="st">' + esc(status) + '</span>\n'
    + lines.map(esc).join('\n');
  box.scrollTop = 0;   // newest entry first, so the top is what matters
}

function refreshMonitor(state) {
  var cap = state.capture, src = state.source, cnt = src.counts || {};
  var capbox = $('capstate');
  if (cap.running) {
    var txt = 'capturing on port ' + cap.port;
    // the capture forwards where the URL box said; say so when nothing answers there
    if (cap.upstream_ok === false) {
      txt += ' &middot; nothing answering at ' + esc(cap.upstream || '?');
    } else {
      txt += ' &rarr; ' + esc(cap.upstream || '?');
    }
    txt += ' &middot; ' + (cap.seen || 0) + ' requests through';
    if (cap.upstream_ok === false && (cap.strata || []).length) {
      txt += ' - Strata is at ' + esc(cap.strata[0].url);
    }
    capbox.className = 'statusbox' + (cap.upstream_ok ? '' : ' warn');
    capbox.innerHTML = '<span class="dot on"></span>' + txt;
  } else {
    capbox.className = 'statusbox off';
    capbox.innerHTML = '<span class="dot"></span>not capturing';
  }
  $('start').disabled = cap.running; $('stop').disabled = !cap.running;
  var up = state.upstream || {};
  if (!upurlTouched) {
    $('upurl').value = up.url || '';
    $('upurl').placeholder = up.default || 'http://127.0.0.1:8080/v1';
  }
  var here = location.origin + '/';
  $('pageurl').textContent = here; $('pageurl').href = here;
  refreshStatusLog(state);
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
  var base = state.paths.agent_base;
  var lines = [
    ['OpenAI-shaped agents', base + '/v1',
     "the agent's base URL setting"],
    ['Claude Code', 'ANTHROPIC_BASE_URL=' + base,
     'set in the shell, then start claude'],
    ['Codex', 'model_base_url = "' + base + '/v1/responses"',
     'a model_provider in .codex\\config.toml']
  ];
  $('agenturls').innerHTML = lines.map(function (l) {
    return '<div class="agline"><span class="aglabel">' + esc(l[0]) + '</span><code>'
      + esc(l[1]) + '</code><button class="copybtn">copy</button>'
      + '<span class="aghint">' + esc(l[2]) + '</span></div>';
  }).join('')
    + '<div class="agline"><span class="aglabel">Strata answers at</span><span class="mono dim">'
    + (strata ? esc(strata.url + '  (model ' + strata.model + ')')
              : 'not found - is the model running?') + '</span></div>';
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

function promptLine(t) {
  if (!t.open_head) { return 'no prompt from a person in this conversation'; }
  var head = t.open_head.length > 90 ? t.open_head.slice(0, 90) + '\u2026' : t.open_head;
  var cut = (t.open_prompt.length < t.open_chars)
    ? ' <span class="dim">(the box holds the first ' + num(t.open_prompt.length) + ' of '
      + num(t.open_chars) + ' chars)</span>' : '';
  return '<span class="uprompt" data-label="thread ' + esc(t.name)
    + ': the first thing a person asked here" data-full="' + esc(t.open_prompt) + '">'
    + esc(head) + '</span> <span class="dim">' + num(t.open_chars) + ' chars</span>' + cut;
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
        + '</div><p class="sub">' + promptLine(t) + '</p>'
        + '<p class="sub">root prompt ' + num(t.root_chars) + ' chars <span class="pill">'
        + esc((t.root_fp || '').slice(0, 8)) + '</span> &middot; ' + t.n_tools + ' tools '
        + '<span class="pill">' + esc((t.tools_fp || '').slice(0, 8)) + '</span>'
        + (t.notes.length ? '<br>' + esc(t.notes.join(' - ')) : '') + '</p>'
        + '<div class="chips" id="t' + t.id + '" hidden>';
      t.rounds.forEach(function (r) {
        var meta = r.api + ' - ' + num(r.size)
          + (r.exact ? ' tokens' : ' estimated tokens')
          + (r.parent ? ' - continues #' + r.parent + ' (+' + r.added + ' messages)'
                      : ' - a new conversation')
          + (r.same_history.length ? ' - same history as #' + r.same_history.join(', #') : '')
          + (r.root_changed ? ' - the root prompt changed here' : '');
        html += '<span class="chip ' + (r.hidden ? 'hid ' : '') + (r.root_changed ? 'warn' : '')
          + '" data-seq="' + r.seq + '" data-label="' + esc(meta) + '" data-full="'
          + esc(r.user_prompt || 'no prompt from a person in this round') + '">'
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
    global _manual_upstream
    _manual_upstream = bool(CFG["upstream"])
    CFG["log_dir"] = os.path.abspath(args.log)
    CFG["captures"] = os.path.abspath(args.captures)
    os.makedirs(CFG["web_dir"], exist_ok=True)
    ensure_live_log()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("context monitor page: %s" % url)
    print("  capture port %d for agents, log %s, capture files %s"
          % (CFG["proxy_port"], CFG["log_dir"], CFG["captures"]))
    print("Ctrl+C closes the page. A capture that is already running keeps running.")
    # Look for Strata on the usual ports and start capturing if it answers, so the
    # page is capturing by the time the browser opens. Off the main thread, because
    # the probes take a second or two and the page should answer right away.
    threading.Thread(target=startup_discovery, daemon=True).start()
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
