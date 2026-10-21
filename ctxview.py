"""Read the logs written by context_proxy.py.

  python ctxview.py live                 watch every request as it happens
  python ctxview.py list [--limit N]     table of logged requests
  python ctxview.py show SEQ [--chars N] the full context of one request, role by role
  python ctxview.py diff A B [--top N]   what grew / changed between two requests
  python ctxview.py stats                totals, biggest contexts, prefix reuse

Token numbers are estimates (~3.5 characters per token), unless Strata reported
"usage" in the response, which `list` and `stats` show when present.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.environ.get("CTX_LOG_DIR") or os.path.join(HERE, "logs")
LOG = os.path.join(LOGDIR, "requests.jsonl")


def _load(path=LOG):
    if not os.path.exists(path):
        sys.exit("no log file yet: %s\nStart the proxy first (context_proxy.py)." % path)
    reqs, resps, order = {}, {}, []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "request":
                reqs[e["seq"]] = e
                order.append(e["seq"])
            elif e.get("type") == "response":
                resps[e["seq"]] = e
    return reqs, resps, order


def _usage_parts(usage):
    """(prompt tokens, cached tokens, completion tokens) from Strata's own usage block."""
    if not isinstance(usage, dict):
        return None, None, None
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    out = usage.get("completion_tokens") or usage.get("output_tokens")
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("cached_tokens")
    return prompt, cached, out


def _fmt_req(e, resps):
    r = resps.get(e["seq"])
    prompt, cached, out = _usage_parts((r or {}).get("usage"))
    status = (r or {}).get("status", "...")
    est = e.get("est_prompt_tokens") or 0
    kb = "%.1f KB" % (e.get("body_bytes", 0) / 1024.0)
    breakdown = "sys %s hist %s tools %s" % (e.get("system_tokens"), e.get("history_tokens"),
                                             e.get("tool_def_tokens"))
    if prompt:
        # Strata's own count first; the character estimate is only a cross-check
        ctx = "%-7s" % f"{prompt:,}"
        extra = "(est ~%s; %s; %s)" % (f"{est:,}", kb, breakdown)
        tail = " reused %s  new %s" % (f"{cached or 0:,}", f"{prompt - (cached or 0):,}")
        if out:
            tail += "  out %s" % f"{out:,}"
    else:
        ctx = "est %s" % f"{est:,}"
        extra = "(%s; %s)" % (kb, breakdown)
        tail = ""
    return "seq %-6s %s  %-17s model=%-14s ctx %-8s %-34s %s %s" % (
        e["seq"], (e.get("ts") or "")[11:19], e.get("api") or "?", e.get("model") or "?",
        ctx, extra, status, ("Strata:" + tail) if prompt else "")


def cmd_list(args):
    reqs, resps, order = _load()
    seqs = order[-args.limit:] if args.limit else order
    for s in seqs:
        print(_fmt_req(reqs[s], resps))
    print("\n%d requests in %s" % (len(order), LOG))


def cmd_live(args):
    if not os.path.exists(LOG):
        sys.exit("no log file yet: %s\nStart the proxy first (context_proxy.py)." % LOG)
    print("watching %s  (Ctrl+C to stop)\n" % LOG)
    sys.stdout.flush()
    with open(LOG, "r", encoding="utf-8", errors="ignore") as f:
        if not args.from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(args.poll)
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "request":
                print(_fmt_req(e, {}))
                sys.stdout.flush()
            elif t == "response":
                print("        -> %s" % e.get("summary"))
                sys.stdout.flush()


def _payload(e):
    p = os.path.join(LOGDIR, e.get("payload_file") or "")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _messages(body_text):
    try:
        obj = json.loads(body_text)
    except Exception:
        return None, None, None
    msgs = obj.get("messages")
    if msgs is None and obj.get("input") is not None:
        msgs = [{"role": "input", "content": obj.get("input")}]
        if obj.get("instructions"):
            msgs = [{"role": "system", "content": obj["instructions"]}] + msgs
    if not isinstance(msgs, list):
        return obj, None, None
    return obj, msgs, obj.get("tools")


def cmd_show(args):
    reqs, resps, _ = _load()
    e = reqs.get(args.seq)
    if not e:
        sys.exit("seq %s not in the log" % args.seq)
    print(_fmt_req(e, resps))
    if (args.seq in resps) and (resps[args.seq].get("completion_preview")):
        print("\n--- completion (first %d chars) ---" % min(1200, len(resps[args.seq]["completion_preview"])))
        print(resps[args.seq]["completion_preview"])
    text = _payload(e)
    if text is None:
        print("\n(payload file missing)")
        return
    obj, msgs, tools = _messages(text)
    print("\n--- %s request (%d bytes, %s/payloads) ---" % (e.get("api"), len(text), LOGDIR))
    if msgs is None:
        print(text[:args.chars])
        return
    if tools:
        print("\ntools: %d defined, ~%d tokens" % (len(tools), sum(len(json.dumps(t, ensure_ascii=False)) for t in tools) // 4))
        for t in tools[:args.top]:
            name = t.get("name") or (t.get("function") or {}).get("name") or "?"
            print("   - %s" % name)
        if len(tools) > args.top:
            print("   ... %d more" % (len(tools) - args.top))
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        text_c = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        extra = ""
        if m.get("tool_calls"):
            extra = "  tool_calls=%s" % [c.get("function", {}).get("name") or c.get("name") for c in m["tool_calls"]]
        print("\n[%d] %s  ~%d tokens%s" % (i, m.get("role"), len(text_c) / 3.5, extra))
        print("    " + text_c[:args.chars].replace("\n", "\n    "))
        if len(text_c) > args.chars:
            print("    ... %d more characters (full copy in the payload file)" % (len(text_c) - args.chars))


def _role_sizes(text):
    obj, msgs, tools = _messages(text)
    if msgs is None:
        return None
    c = Counter()
    for m in msgs:
        if isinstance(m, dict):
            content = m.get("content")
            s = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            c[m.get("role", "?")] += len(s)
    if tools:
        c["tools(json)"] = sum(len(json.dumps(t, ensure_ascii=False)) for t in tools)
    return c


def cmd_diff(args):
    reqs, _, _ = _load()
    a, b = reqs.get(args.a), reqs.get(args.b)
    if not a or not b:
        sys.exit("seq not in the log")
    ta, tb = _payload(a), _payload(b)
    if not ta or not tb:
        sys.exit("payload file missing for one of the requests")
    ca, cb = _role_sizes(ta), _role_sizes(tb)
    print("seq %s  ~%s tokens  ->  seq %s  ~%s tokens" % (
        a["seq"], a.get("est_prompt_tokens"), b["seq"], b.get("est_prompt_tokens")))
    print("\nrole            before (chars)   after (chars)   delta")
    for k in sorted(set(ca) | set(cb), key=lambda x: -(cb or {}).get(x, 0)):
        d = cb.get(k, 0) - ca.get(k, 0)
        print("%-14s %13d %15d %9s" % (k, ca.get(k, 0), cb.get(k, 0), ("+%d" % d) if d >= 0 else d))
    n = min(len(ta), len(tb))
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ta[:mid] == tb[:mid]:
            lo = mid
        else:
            hi = mid - 1
    print("\nidentical prefix: %d characters (~%d tokens, %.1f%% of the later request)"
          % (lo, lo / 3.5, 100.0 * lo / max(1, len(tb))))
    print("-> Strata's prompt cache reuses that prefix; only the rest is read again.")


def cmd_stats(args):
    reqs, resps, order = _load()
    if not order:
        sys.exit("no requests logged")
    tot = [reqs[s].get("est_prompt_tokens") or 0 for s in order]
    print("requests: %d   prompt tokens (est): avg %d  max %d  min %d" % (
        len(order), sum(tot) // len(tot), max(tot), min(tot)))
    by_api = Counter(reqs[s].get("api") for s in order)
    print("by api:", ", ".join("%s=%d" % kv for kv in by_api.items()))
    real = [_usage_parts((resps.get(s) or {}).get("usage")) for s in order]
    real = [r for r in real if r[0]]
    if real:
        pin = sum(r[0] for r in real)
        cach = sum(r[1] or 0 for r in real)
        pout = sum(r[2] or 0 for r in real)
        print("Strata's own counts over %d answered requests: %s prompt tokens in "
              "(%s reused from its prompt cache, %s read new), %s tokens out"
              % (len(real), f"{pin:,}", f"{cach:,}", f"{pin - cach:,}", f"{pout:,}"))

    # the biggest single context, by Strata's count when it has one
    def _size(s):
        p = _usage_parts((resps.get(s) or {}).get("usage"))[0]
        return p or (reqs[s].get("est_prompt_tokens") or 0)
    peak = max(order, key=_size)
    pk = _usage_parts((resps.get(peak) or {}).get("usage"))[0]
    print("biggest context: seq %s  %s tokens %s  %.1f KB of request, tool definitions ~%s est tokens"
          % (peak, f"{_size(peak):,}", "(Strata's count)" if pk else "(estimated)",
             reqs[peak].get("body_bytes", 0) / 1024.0, f"{reqs[peak].get('tool_def_tokens') or 0:,}"))
    try:
        from ctxpage import find_context_window
        window, src = find_context_window()
    except Exception:
        window, src = 0, ""
    if window:
        print("context window: %s tokens (%s); the biggest round filled %.1f%% of it, %s tokens of room left"
              % (f"{window:,}", src, 100.0 * _size(peak) / window, f"{window - _size(peak):,}"))
    else:
        print("context window: not found in Strata's config, so counts are shown as plain numbers")
    print("\nlargest contexts:")
    for s in sorted(order, key=lambda x: -_size(x))[:args.top]:
        print("  " + _fmt_req(reqs[s], resps))
    prev = None
    reuse = []
    for s in order:
        t = _payload(reqs[s])
        if t is None:
            continue
        if prev is not None:
            n = min(len(prev), len(t))
            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if prev[:mid] == t[:mid]:
                    lo = mid
                else:
                    hi = mid - 1
            reuse.append((lo, len(t)))
        prev = t
    if reuse:
        saved = sum(a for a, _ in reuse)
        allc = sum(b for _, b in reuse)
        print("\nconsecutive rounds share %.1f%% of their characters on average (identical prefix)"
              % (100.0 * saved / max(1, allc)))
        if real:
            print("Strata's own numbers: %s of %s prompt tokens were already in its prompt cache "
                  "(%.1f%%), so only %s had to be read new"
                  % (f"{cach:,}", f"{pin:,}", 100.0 * cach / pin, f"{pin - cach:,}"))


def main():
    ap = argparse.ArgumentParser(description="read Strata context logs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("live")
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--from-start", action="store_true")
    p.set_defaults(func=cmd_live)
    p = sub.add_parser("show")
    p.add_argument("seq", type=int)
    p.add_argument("--chars", type=int, default=700)
    p.add_argument("--top", type=int, default=12)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("diff")
    p.add_argument("a", type=int)
    p.add_argument("b", type=int)
    p.add_argument("--top", type=int, default=12)
    p.set_defaults(func=cmd_diff)
    p = sub.add_parser("stats")
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(func=cmd_stats)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
