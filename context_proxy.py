"""Context Monitor for Strata - a pass-through logging proxy.

Strata's own Monitor / `strata monitor` / `strata logs` deliberately show only
token counts and timings - never prompt bodies. To see the ACTUAL context each
agent sends, point the agent at this proxy instead of at Strata. The proxy sits
between them, forwards everything unchanged (including streaming and tool
calls) and writes one JSON line per request, plus the full request body.

Run:  python context_proxy.py                 (127.0.0.1:8788 -> 127.0.0.1:8080)
      python context_proxy.py --port 8788 --upstream http://127.0.0.1:8080
Point your agent at http://127.0.0.1:8788 :
      OpenAI-compatible base URL  http://127.0.0.1:8788/v1
      Anthropic (Claude Code)     ANTHROPIC_BASE_URL=http://127.0.0.1:8788
      Codex CLI (Responses)       http://127.0.0.1:8788/v1/responses
Live dashboard in another window:  python ctxview.py live
Standard library only. Nothing leaves this PC except to Strata itself.
"""

import argparse
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

# ---------------------------------------------------------------- helpers

_lock = threading.Lock()
_counter = 0
_state = {"proxy": None, "upstream": None, "dir": None}


def _next_seq():
    global _counter
    with _lock:
        _counter += 1
        return _counter


def _est_tokens(s):
    """Rough token estimate: mixed code/CJK text averages ~3.5 characters per token."""
    if not isinstance(s, str):
        return None
    return int(len(s) / 3.5 + 0.5)


def _msg_text(content):
    """Flatten OpenAI/Anthropic message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("type")
                if t in ("text", "text_delta"):
                    out.append(part.get("text") or "")
                elif t == "tool_result":
                    out.append(_msg_text(part.get("content")))
                elif t == "tool_use":
                    out.append(json.dumps(part.get("input", {}), ensure_ascii=False))
                elif t == "image":
                    out.append("[image]")
                elif t == "image_url":
                    out.append("[image]")
                elif "text" in part:
                    out.append(str(part.get("text") or ""))
        return "\n".join(x for x in out if x)
    if content is None:
        return ""
    return str(content)


def _summarize(body_json):
    """Return (api, system_tokens, message_tokens, tool_def_tokens, msg_roles).

    Shape check order matters: an Anthropic request also has "messages", and a
    Responses request may carry "input" beside other keys.
    """
    if not isinstance(body_json, dict):
        return "unknown", None, None, None, []

    tool_tok = 0
    for t in body_json.get("tools") or []:
        tool_tok += _est_tokens(json.dumps(t, ensure_ascii=False)) or 0

    if ("system" in body_json and "messages" in body_json) or "anthropic_version" in body_json:
        # Anthropic Messages
        sys_tok = 0
        system = body_json.get("system")
        if isinstance(system, str):
            sys_tok = _est_tokens(system) or 0
        else:
            for s in system or []:
                sys_tok += _est_tokens(_msg_text(s.get("text") if isinstance(s, dict) else s)) or 0
        msg_tok = 0
        roles = []
        for m in body_json.get("messages") or []:
            if not isinstance(m, dict):
                continue
            roles.append(m.get("role", "?"))
            msg_tok += _est_tokens(_msg_text(m.get("content"))) or 0
        return "anthropic-messages", sys_tok, msg_tok, tool_tok, roles

    if "messages" in body_json:
        sys_tok, msg_tok = 0, 0
        roles = []
        for m in body_json.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "?")
            roles.append(role)
            txt = _msg_text(m.get("content"))
            if m.get("tool_calls"):
                txt += json.dumps(m["tool_calls"], ensure_ascii=False)
            n = _est_tokens(txt) or 0
            if role in ("system", "developer"):
                sys_tok += n
            else:
                msg_tok += n
        return "openai-chat", sys_tok, msg_tok, tool_tok, roles

    if "input" in body_json or "instructions" in body_json:  # OpenAI Responses API (Codex CLI)
        txt = _msg_text(body_json.get("input"))
        instr = _est_tokens(_msg_text(body_json.get("instructions"))) or 0
        return "openai-responses", instr, _est_tokens(txt), tool_tok, []

    return "unknown", None, None, None, []


def _read_headers(rfile):
    headers = {}
    raw_line = rfile.readline()
    if not raw_line:
        return None, None, headers
    request_line = raw_line.decode("latin-1").strip()
    while True:
        line = rfile.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        try:
            name, value = line.decode("latin-1").rstrip("\r\n").split(":", 1)
        except ValueError:
            continue
        headers[name.strip()] = value.strip()
    lower = {k.lower(): v for k, v in headers.items()}
    return request_line, headers, lower


def _read_body(rfile, headers):
    length = next((v for k, v in headers.items() if k.lower() == "content-length"), None)
    if length is not None:
        try:
            n = int(length)
        except ValueError:
            n = 0
        return rfile.read(n) if n > 0 else b""
    te = next((v for k, v in headers.items() if k.lower() == "transfer-encoding"), "")
    if te.lower() == "chunked":
        chunks = []
        while True:
            size_line = rfile.readline().strip()
            if not size_line:
                break
            try:
                size = int(size_line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                rfile.readline()  # trailing CRLF
                break
            chunks.append(rfile.read(size))
            rfile.readline()  # CRLF after chunk
        return b"".join(chunks)
    return b""


def _strip_hop_by_hop(headers):
    drop = {"host", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "transfer-encoding",
            "upgrade", "content-length"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _status_code(status_line):
    """The HTTP status number of a response's first line, or 0 if it is unreadable."""
    try:
        return int((status_line or "").split()[1])
    except Exception:
        return 0


def _mask(headers):
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "proxy-authorization", "x-api-key", "api-key"):
            out[k] = "<masked>"
        else:
            out[k] = v
    return out


_LOG_TYPES = ("request", "response", "event")


def _append_log(entry):
    path = os.path.join(_state["dir"], "requests.jsonl")
    line = json.dumps(entry, ensure_ascii=False)
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if entry.get("type") in _LOG_TYPES:
            print("[seq %s] %s" % (entry.get("seq"), entry.get("summary", "")), flush=True)


# ---------------------------------------------------------------- proxy

class ProxyHandler(socketserver.BaseRequestHandler):

    def handle(self):
        conn = self.request
        conn.settimeout(1800)
        rfile = conn.makefile("rb")
        up = urlparse(_state["upstream"])
        up_host, up_port = up.hostname, up.port or 80
        try:
            while True:
                request_line, headers, _ = _read_headers(rfile)
                if not request_line:
                    return
                body = _read_body(rfile, headers)
                keep_alive = self._one(request_line, headers, body, conn, up_host, up_port)
                if not keep_alive:
                    return
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            sys.stderr.write("proxy: %r\n" % (exc,))
        finally:
            try:
                rfile.close()
            except Exception:
                pass

    def _one(self, request_line, headers, body, conn, up_host, up_port):
        parts = request_line.split()
        method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("?", request_line)

        if method == "GET" and path.rstrip("/") == "/ctx-stats":
            stats = {"proxy": _state["proxy"], "upstream": _state["upstream"],
                     "log_dir": _state["dir"], "requests_seen": _counter}
            payload = json.dumps(stats).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(payload) + payload)
            return

        seq = _next_seq()
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        api, sys_tok, msg_tok, tool_tok, roles = (None,) * 5
        model, wants_stream, body_json = None, False, None

        if body:
            try:
                body_json = json.loads(body.decode("utf-8"))
            except Exception:
                body_json = None
        if body_json is not None:
            api, sys_tok, msg_tok, tool_tok, roles = _summarize(body_json)
            model = body_json.get("model")
            wants_stream = bool(body_json.get("stream"))

        total_tok = None
        if None not in (sys_tok, msg_tok, tool_tok):
            total_tok = sys_tok + msg_tok + tool_tok

        # store the full request body
        payload_file = None
        if body:
            name = "req-%06d.json" % seq
            data = body
            maxb = _state["store_max_bytes"]
            if maxb and len(data) > maxb:
                name = "req-%06d.json.txt" % seq
                data = data[:maxb] + ("\n...[truncated at %d bytes]..." % maxb).encode()
            with open(os.path.join(_state["dir"], "payloads", name), "wb") as f:
                f.write(data)
            payload_file = "payloads/" + name

        _append_log({
            "type": "request", "seq": seq, "ts": ts, "method": method, "path": path,
            "api": api, "model": model, "stream": wants_stream,
            "headers": _mask(headers), "body_bytes": len(body),
            "est_prompt_tokens": total_tok, "system_tokens": sys_tok,
            "history_tokens": msg_tok, "tool_def_tokens": tool_tok,
            "roles": roles, "payload_file": payload_file,
            "summary": "%s model=%s prompt~%s tok (sys %s / history %s / tools %s), %d B" % (
                api or "?", model or "?", total_tok, sys_tok, msg_tok, tool_tok, len(body)),
        })

        # forward upstream unchanged
        fwd = _strip_hop_by_hop(headers)
        fwd["Host"] = "%s:%d" % (up_host, up_port) if up_port != 80 else up_host
        lines = ["%s %s %s" % (method, path, parts[2] if len(parts) > 2 else "HTTP/1.1")]
        for k, v in fwd.items():
            lines.append("%s: %s" % (k, v))
        lines.append("Content-Length: %d" % len(body))
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

        try:
            up_sock = socket.create_connection((up_host, up_port), timeout=1800)
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"error": "proxy cannot reach Strata at %s:%s" % (up_host, up_port),
                              "detail": str(exc)}).encode()
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(err) + err)
            _append_log({"type": "response", "seq": seq, "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "status": 502, "upstream": False, "completion_text": None, "usage": None,
                         "summary": "502 upstream unreachable: %s" % exc})
            return

        wfile = up_sock.makefile("wb")
        wfile.write(head)
        if body:
            wfile.write(body)
        wfile.flush()

        urfile = up_sock.makefile("rb")
        # _read_headers returns (first line, headers, the same headers keyed
        # lower case). Header names are matched case-insensitively from here:
        # the Strata app writes them as "content-type", "transfer-encoding", ...
        status_line, up_headers, up_lower = _read_headers(urfile)
        code = _status_code(status_line)
        # An interim answer (100 Continue, from the Expect header some clients send)
        # is passed through, then the real response is read behind it.
        while 100 <= code < 200 and code != 101:
            conn.sendall(("%s\r\n" % status_line).encode("latin-1"))
            for k, v in up_headers.items():
                conn.sendall(("%s: %s\r\n" % (k, v)).encode("latin-1"))
            conn.sendall(b"\r\n")
            status_line, up_headers, up_lower = _read_headers(urfile)
            code = _status_code(status_line)
        ctype = up_lower.get("content-type", "")
        is_sse = "text/event-stream" in ctype.lower()
        chunked = up_lower.get("transfer-encoding", "").lower() == "chunked"
        clen = up_lower.get("content-length")
        # A body with no Content-Length and no chunked framing ends only when the
        # connection closes, so the client must be told to close as well.
        upstream_closes = (clen is None and not chunked) or \
            up_lower.get("connection", "").lower() == "close"
        client_lower = {k.lower(): v for k, v in headers.items()}
        client_closes = client_lower.get("connection", "").lower() == "close" or \
            (parts[2] if len(parts) > 2 else "") == "HTTP/1.0"
        keep_alive = not (upstream_closes or client_closes)

        out_head = [status_line or "HTTP/1.1 500 Error"]
        for k, v in up_headers.items():
            if k.lower() in ("connection", "keep-alive"):
                continue
            out_head.append("%s: %s" % (k, v))
        out_head.append("Connection: %s" % ("keep-alive" if keep_alive else "close"))
        conn.sendall(("\r\n".join(out_head) + "\r\n\r\n").encode("latin-1"))

        completion = []
        reasoning = []
        toolcalls = {}      # index -> {"name": str, "id": str, "args": [pieces]}
        usage, rid = None, None
        finish = None
        try:
            if is_sse:
                while True:
                    line = urfile.readline()
                    if not line:
                        break
                    conn.sendall(line)
                    if line.startswith(b"data:"):
                        payload = line[5:].strip()
                        if payload and payload != b"[DONE]":
                            try:
                                obj = json.loads(payload.decode("utf-8"))
                            except Exception:
                                obj = None
                            if isinstance(obj, dict):
                                if obj.get("usage"):
                                    usage = obj["usage"]
                                if obj.get("id"):
                                    rid = obj["id"]
                                inner = obj.get("response")
                                if isinstance(inner, dict) and inner.get("id"):
                                    rid = inner["id"]
                                _collect(obj, completion, reasoning, toolcalls)
                                for ch in obj.get("choices") or []:
                                    if ch.get("finish_reason"):
                                        finish = ch["finish_reason"]
                                if obj.get("stop_reason"):
                                    finish = obj["stop_reason"]
            elif chunked:
                got = bytearray()
                while True:
                    size_line = urfile.readline().strip()
                    if not size_line:
                        break
                    conn.sendall(size_line + b"\r\n")
                    try:
                        size = int(size_line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        trail = urfile.readline()
                        conn.sendall(trail)
                        break
                    data = urfile.read(size)
                    conn.sendall(data + b"\r\n")
                    urfile.readline()
                    got += data
                usage, finish, rid = _tally(
                    _scan_json(bytes(got), completion, reasoning, toolcalls), usage, finish, rid)
            elif clen is not None:
                remaining = int(clen)
                got = bytearray()
                while remaining > 0:
                    data = urfile.read(min(remaining, 65536))
                    if not data:
                        break
                    remaining -= len(data)
                    got += data
                    conn.sendall(data)
                usage, finish, rid = _tally(
                    _scan_json(bytes(got), completion, reasoning, toolcalls), usage, finish, rid)
            else:
                got = bytearray()
                while True:
                    data = urfile.read(65536)
                    if not data:
                        break
                    got += data
                    conn.sendall(data)
                usage, finish, rid = _tally(
                    _scan_json(bytes(got), completion, reasoning, toolcalls), usage, finish, rid)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("proxy stream: %r\n" % (exc,))
        finally:
            try:
                urfile.close()
                wfile.close()
            except Exception:
                pass

        text = "".join(completion)
        calls = []
        for idx in sorted(toolcalls):
            call = toolcalls[idx]
            args = "".join(call["args"])
            calls.append({"name": call["name"] or "?", "arguments": args})
        thinking = "".join(reasoning)
        response_file = _write_response(seq, text, thinking, calls)
        _append_log({
            "type": "response", "seq": seq,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": code, "content_type": ctype, "stream": is_sse or wants_stream,
            "completion_chars": len(text), "completion_preview": text[:1200],
            "reasoning_chars": len(thinking),
            "tool_calls": [{"name": c["name"], "args_chars": len(c["arguments"])} for c in calls],
            "response_file": response_file,
            "usage": usage, "finish_reason": finish, "response_id": rid,
            "summary": "HTTP %s, %d chars text, %d tool call(s), usage=%s" % (
                code, len(text), len(calls), usage or "n/a"),
        })
        return keep_alive


def _write_response(seq, text, thinking, calls):
    """Write what the model actually answered - text, thinking and tool calls - to a file."""
    if not (text or thinking or calls):
        return None
    parts = []
    if thinking:
        parts.append("=== thinking ===\n%s" % thinking)
    if text:
        parts.append("=== text ===\n%s" % text)
    for i, call in enumerate(calls, 1):
        try:
            pretty = json.dumps(json.loads(call["arguments"]), ensure_ascii=False, indent=2)
        except Exception:
            pretty = call["arguments"]
        parts.append("=== tool call %d: %s ===\n%s" % (i, call["name"], pretty))
    name = "resp-%06d.txt" % seq
    try:
        with open(os.path.join(_state["dir"], "payloads", name), "w", encoding="utf-8") as f:
            f.write("\n\n".join(parts))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("proxy: cannot write %s: %r\n" % (name, exc))
        return None
    return "payloads/" + name


def _collect(obj, sink, think_sink, calls):
    """Pull the model's real answer out of one streamed JSON chunk.

    Handles the OpenAI chat, Anthropic Messages and OpenAI Responses shapes:
    answer text, thinking text, and tool calls (name plus the arguments as they
    stream in), because an agent turn usually ends in a tool call rather than text.
    """
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or {}
        if isinstance(d.get("content"), str):
            sink.append(d["content"])
        for key in ("reasoning_content", "reasoning"):
            if isinstance(d.get(key), str):
                think_sink.append(d[key])
        for tc in d.get("tool_calls") or []:
            slot = calls.setdefault(tc.get("index", 0), {"name": "", "id": "", "args": []})
            fn = tc.get("function") or {}
            if isinstance(fn.get("name"), str):
                slot["name"] += fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["args"].append(fn["arguments"])
            if tc.get("id"):
                slot["id"] = tc["id"]
        m = ch.get("message") or {}          # non-streaming chat reply
        if isinstance(m.get("content"), str):
            sink.append(m["content"])
        for key in ("reasoning_content", "reasoning"):
            if isinstance(m.get(key), str):
                think_sink.append(m[key])
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            slot = calls.setdefault(tc.get("index", 0), {"name": "", "id": "", "args": []})
            slot["name"] = fn.get("name") or slot["name"]
            slot["id"] = tc.get("id") or slot["id"]
            args = fn.get("arguments")
            slot["args"] = [args] if isinstance(args, str) else []
    blk = obj.get("delta") or {}
    if isinstance(blk.get("text"), str):                    # Anthropic text_delta
        sink.append(blk["text"])
    if isinstance(blk.get("thinking"), str):                # Anthropic thinking_delta
        think_sink.append(blk["thinking"])
    if obj.get("type") == "content_block_start":
        block = obj.get("content_block") or {}
        if block.get("type") == "tool_use":
            slot = calls.setdefault(obj.get("index", 0), {"name": "", "id": "", "args": []})
            slot["name"] = block.get("name") or ""
            slot["id"] = block.get("id") or ""
    if obj.get("type") == "content_block_delta" and isinstance(blk.get("partial_json"), str):
        calls.setdefault(obj.get("index", 0), {"name": "", "id": "", "args": []})["args"].append(
            blk["partial_json"])
    if isinstance(obj.get("output_text"), str):             # Responses API
        sink.append(obj["output_text"])
    if obj.get("type") == "response.output_item.added":
        item = obj.get("item") or {}
        if item.get("type") == "function_call":
            slot = calls.setdefault(obj.get("output_index", 0), {"name": "", "id": "", "args": []})
            slot["name"] = item.get("name") or ""
            slot["id"] = item.get("call_id") or ""
    if obj.get("type") == "response.function_call_arguments.delta":
        calls.setdefault(obj.get("output_index", 0), {"name": "", "id": "", "args": []})["args"].append(
            obj.get("delta") or "")


def _scan_json(data, sink, think_sink, calls):
    """Parse a whole non-streaming JSON body, keep its answer, and return it (or None)."""
    try:
        obj = json.loads(data.decode("utf-8", "ignore"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    _collect(obj, sink, think_sink, calls)
    if isinstance(obj.get("content"), list):  # non-streaming Anthropic
        for blk in obj["content"]:
            if not isinstance(blk, dict):
                continue
            if isinstance(blk.get("text"), str):
                sink.append(blk["text"])
            elif isinstance(blk.get("thinking"), str):
                think_sink.append(blk["thinking"])
            elif blk.get("type") == "tool_use":
                calls.setdefault(blk.get("id", 0), {
                    "name": blk.get("name") or "", "id": blk.get("id") or "",
                    "args": [json.dumps(blk.get("input") or {}, ensure_ascii=False)]})
    return obj


def _tally(obj, usage, finish, rid=None):
    """Take usage, the finish reason and the answer id out of a response body."""
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
        if obj.get("stop_reason"):
            finish = obj["stop_reason"]
        if obj.get("id"):
            rid = obj["id"]
        inner = obj.get("response")
        if isinstance(inner, dict) and inner.get("id"):
            rid = inner["id"]
    return usage, finish, rid


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Strata context logging proxy")
    ap.add_argument("--port", type=int, default=8788, help="proxy listen port (default 8788)")
    ap.add_argument("--host", default="127.0.0.1", help="proxy bind address (default loopback)")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080", help="Strata server URL")
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                    help="log folder (default .\\logs next to this script)")
    ap.add_argument("--store-max-bytes", type=int, default=2_000_000,
                    help="truncate stored request bodies above this size (0 = never)")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.dir, "payloads"), exist_ok=True)
    _state["proxy"] = "http://%s:%d" % (args.host, args.port)
    _state["upstream"] = args.upstream
    _state["dir"] = os.path.abspath(args.dir)
    _state["store_max_bytes"] = args.store_max_bytes

    # when the log file already exists, keep its numbering going
    global _counter
    with _lock:
        p = os.path.join(_state["dir"], "requests.jsonl")
        if os.path.exists(p):
            n = 0
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    n += 1
                    try:
                        old_seq = json.loads(line).get("seq") or 0
                        if isinstance(old_seq, int) and old_seq > _counter:
                            _counter = old_seq
                    except Exception:
                        pass
            print("log file already has %d lines; continuing at seq %d" % (n, _counter))

    srv = Server((args.host, args.port), ProxyHandler)
    print("Context Monitor proxy listening on %s  ->  Strata at %s" % (_state["proxy"], args.upstream))
    print("logs: %s" % os.path.join(_state["dir"], "requests.jsonl"))
    print("OpenAI-compatible base URL : %s/v1" % _state["proxy"])
    print("Claude Code                : set ANTHROPIC_BASE_URL=%s" % _state["proxy"])
    print("Codex CLI                  : %s/v1/responses" % _state["proxy"])
    print("live view in another shell : python ctxview.py live")
    print("stats                      : GET %s/ctx-stats" % _state["proxy"])
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
