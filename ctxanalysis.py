"""The Capture Analysis page: what the rounds of one source say about the clients that
made them, taken one conversation at a time.

The Context page shows the rounds themselves. This page answers the questions that sit
behind them: how much of each round the model already knew, how fast it read and how
fast it wrote, and the exact rounds where a client threw its prompt cache away. Every
figure comes from the same token counts and timings the Context page uses, and a figure
the log cannot give stays a dash rather than a guess. The charts are inline SVG, so the
page needs no network, no script, and no other file.
"""
import argparse
import math
import os
import sys

import ctxlink
import ctxpage
import ctxstore
from ctxpage import esc, kpi, _rate, _secs

# the colours the Context page already uses for the same ideas: reused prompt in green,
# re-read prompt in amber, reading speed in blue, writing speed in green, a lost cache in red
REUSE, NEW, ACCENT, BAD = "#3fb27f", "#ffb454", "#5aa9ff", "#ff6b6b"
PALETTE = ["#5aa9ff", "#ffb454", "#c084fc", "#5eead4", "#f472b6", "#a3e635"]
MAX_CHARTED = 6            # a capture with a hundred threads makes a hundred chart sets


# --------------------------------------------------------------------- the numbers

def _stats(vals):
    """min / median / max / mean of a list, {} when there is nothing to measure."""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return {}
    return {"n": len(vals), "min": vals[0], "med": vals[len(vals) // 2],
            "max": vals[-1], "avg": sum(vals) / len(vals), "sum": sum(vals)}


def _num(value, digits=0):
    """A figure, or a dash when the log does not say. Never a made-up zero."""
    if value is None:
        return "&mdash;"
    return f"{value:,.{digits}f}"


def _times(ratio):
    """A ratio the prose can read out loud: four times, not 4.0 times."""
    text = f"{ratio:.1f}"
    return text[:-2] if text.endswith(".0") else text


def client_label(text):
    """A short name for the client that made the call, from the headers ctxlink kept.
    The raw string stays available in the page as the tooltip."""
    low = (text or "").lower()
    if "copilotchat" in low or "vscode" in low or "vs code" in low:
        return "VS Code Copilot Chat"
    if "copilot" in low and ("app" in low or "desktop" in low or "agent" in low):
        return "Copilot app"
    if "openai/js" in low or "stainless" in low:
        return "OpenAI JS client"
    if "python" in low or "openai/py" in low:
        return "OpenAI Python client"
    if "curl" in low:
        return "curl"
    if not text or text == "-":
        return "client not shown"
    return text[:40]


def _round_row(seq, req, resp, lk):
    """One round as the analysis sees it: its tokens, its timings, and what changed."""
    prompt, cached, out = ctxstore._usage((resp or {}).get("usage"))
    m = ctxstore.round_metrics(req, resp)
    calls = [(c.get("name") or "?") for c in ((resp or {}).get("tool_calls") or [])]
    return {
        "seq": seq, "ts": (req.get("ts") or "")[11:19],
        "prompt": prompt, "cached": cached, "new": prompt - cached, "out": out,
        "ms": m["ms"], "ttft": m["ttft"], "prefill": m["prefill"], "decode": m["decode"],
        "whole": m["whole"], "split": m["split"],
        "tools": lk.get("n_tools"), "tools_changed": bool(lk.get("tools_changed")),
        "root_changed": bool(lk.get("root_changed")),
        "cold": bool(prompt) and not cached,
        "est_sys": req.get("system_tokens"), "est_tools": req.get("tool_def_tokens"),
        "est_hist": req.get("history_tokens"), "est": req.get("est_prompt_tokens") or 0,
        "reason": (resp or {}).get("finish_reason"), "calls": calls,
        "thinking": (resp or {}).get("reasoning_chars") or 0,
        "answer": (resp or {}).get("completion_chars") or 0,
        "client": lk.get("client") or ctxlink.client_of(req),
    }


def _thread_block(info, seqs, reqs, resps, links):
    """Everything one conversation contributes to the page."""
    rows = [_round_row(s, reqs[s], resps.get(s), links.get(s) or {}) for s in seqs]
    tm = ctxstore.thread_metrics(seqs, reqs, resps)
    prompt = sum(r["prompt"] for r in rows)
    cached = sum(r["cached"] for r in rows)
    out = sum(r["out"] for r in rows)
    first, last = rows[0], rows[-1]
    standing = ((first["est_sys"] or 0) + (first["est_tools"] or 0)) or None
    history = [r["est_hist"] for r in rows if r["est_hist"]]
    changes = []
    for r in rows:
        if not r["tools_changed"]:
            continue
        parent = (links.get(r["seq"]) or {}).get("parent")
        changes.append({"seq": r["seq"], "from": (links.get(parent) or {}).get("n_tools"),
                        "to": r["tools"]})
    finishes = {}
    names = {}
    for r in rows:
        if r["reason"]:
            finishes[r["reason"]] = finishes.get(r["reason"], 0) + 1
        for name in r["calls"]:
            names[name] = names.get(name, 0) + 1
    cold = [r["seq"] for r in rows if r["cold"]]
    # the first round of a conversation has nothing to reuse yet, so it is cold without
    # being a reset; only the cold rounds after it say something went wrong mid-way
    resets = [s for s in cold if s != seqs[0]]
    return {
        "id": info.get("id"), "name": info.get("name") or ("thread %s" % info.get("id")),
        "client": first["client"], "label": client_label(first["client"]),
        "seqs": seqs, "rows": rows, "count": len(rows),
        "first": seqs[0], "last": seqs[-1], "first_ts": first["ts"], "last_ts": last["ts"],
        "prompt": prompt, "cached": cached, "new": prompt - cached, "out": out,
        "cache_pct": (100.0 * cached / prompt) if prompt else None,
        "peak": max((r["prompt"] for r in rows), default=0),
        "avg_prompt": (prompt / len(rows)) if rows else 0.0,
        "wall": (tm["ms"] / 1000.0) if tm["ms"] else None,
        "timed_rounds": tm["rounds"],
        "ttft": _stats([r["ttft"] for r in rows]), "round_ms": _stats([r["ms"] for r in rows]),
        "prefill": tm["prefill"], "decode": tm["decode"], "whole": tm["whole"],
        "whole_rounds": tm["whole_rounds"],
        "warm_prefill": _stats([r["prefill"] for r in rows if r["prefill"] and r["cached"]]),
        "cold": cold, "resets": resets,
        "tool_changes": changes,
        "root_changes": [r["seq"] for r in rows if r["root_changed"]],
        "tools": first["tools"], "tools_max": max([r["tools"] or 0 for r in rows], default=0),
        "standing": standing, "history_avg": (sum(history) / len(history)) if history else None,
        "est_total": sum(r["est"] for r in rows),
        "thinking": sum(r["thinking"] for r in rows), "answer": sum(r["answer"] for r in rows),
        "finishes": finishes, "call_names": names, "calls": sum(len(r["calls"]) for r in rows),
    }


def analyze(cap, reqs, resps, order, shown, links, window=None, window_src=""):
    """The source being viewed, cut up per conversation, as plain figures.

    Hidden rounds are already out of `shown`, so the page analyses what the Context page
    is showing rather than what the file happens to hold.
    """
    shown_set = set(shown)
    blocks = []
    for info in links.get("_threads") or []:
        seqs = sorted(s for s in info.get("rounds", []) if s in shown_set)
        if seqs:
            blocks.append(_thread_block(info, seqs, reqs, resps, links))
    blocks.sort(key=lambda b: (-b["count"], b["seqs"][0]))
    rows = [r for b in blocks for r in b["rows"]]
    prompt = sum(r["prompt"] for r in rows)
    cached = sum(r["cached"] for r in rows)
    overall = {
        "rounds": len(rows), "threads": len(blocks),
        "prompt": prompt, "cached": cached, "new": prompt - cached,
        "out": sum(r["out"] for r in rows),
        "cache_pct": (100.0 * cached / prompt) if prompt else None,
        "wall": sum(r["ms"] or 0 for r in rows) / 1000.0,
        "timed": sum(1 for r in rows if r["ms"]),
        "cold": sum(len(b["resets"]) for b in blocks),
        "tool_changes": sum(len(b["tool_changes"]) for b in blocks),
        "peak": max([r["prompt"] for r in rows], default=0),
        "ttft": _stats([r["ttft"] for r in rows]),
        "thinking": sum(r["thinking"] for r in rows),
    }
    slowest = None
    for r in rows:
        if r["ttft"] and (slowest is None or r["ttft"] > slowest["ttft"]):
            slowest = r
    overall["slowest"] = ({"seq": slowest["seq"], "ttft": slowest["ttft"],
                           "prompt": slowest["prompt"], "cached": slowest["cached"]}
                          if slowest else None)
    data = {
        "source": str(cap.path),
        "label": os.path.basename(str(cap.path)),
        "kind": cap.kind, "rounds_total": len(order), "hidden": len(order) - len(shown_set),
        "model": (cap.manifest or {}).get("model") or (reqs[order[0]].get("model") if order else ""),
        "window": window, "window_src": window_src,
        "first": (reqs[order[0]].get("ts") if order else ""),
        "last": (reqs[max(order)].get("ts") if order else ""),
        "threads": blocks, "overall": overall,
    }
    data["findings"] = findings(data)
    return data


def findings(data):
    """What the figures add up to, in words. Each one is a short lead and a sentence;
    a thread with nothing to say produces nothing rather than a filler line."""
    out = []
    blocks = data["threads"]
    overall = data["overall"]
    if not blocks:
        return [{"lead": "Nothing to analyse",
                 "text": "This source has no rounds the view can show."}]

    if len(blocks) >= 2:
        big = max(blocks, key=lambda b: b["prompt"])
        small = min(blocks, key=lambda b: b["prompt"])
        if small["prompt"] > 0 and big["prompt"] / max(small["prompt"], 1) >= 1.5:
            out.append({
                "lead": "%s sent %s times the prompt tokens of %s"
                       % (big["label"], _times(big["prompt"] / max(small["prompt"], 1)),
                          small["label"]),
                "text": "%s: %s rounds, %s prompt tokens, %s s of model time. %s: %s rounds, "
                        "%s prompt tokens, %s s. %s"
                        % (big["label"], f"{big['count']}", f"{big['prompt']:,}",
                           f"{big['wall']:,.0f}" if big["wall"] else "not timed",
                           small["label"], f"{small['count']}", f"{small['prompt']:,}",
                           f"{small['wall']:,.0f}" if small["wall"] else "not timed",
                           _same_speed(big, small))})
        with_standing = [b for b in blocks if b["standing"]]
        if len(with_standing) >= 2:
            light = min(with_standing, key=lambda b: b["standing"])
            heavy = max(with_standing, key=lambda b: b["standing"])
            if heavy["standing"] >= light["standing"] * 1.3:
                out.append({
                    "lead": "%s carries a smaller standing prompt than %s"
                           % (light["label"], heavy["label"]),
                    "text": "%s estimated tokens of system text and %s tool definitions in every "
                            "round, against %s estimated tokens and %s tool definitions. That is "
                            "the part of the prompt that never changes, so it is the part a client "
                            "can still make smaller."
                            % (f"{light['standing']:,}", light["tools"],
                               f"{heavy['standing']:,}", heavy["tools"])})
    for b in blocks:
        if b["resets"]:
            worst = max((r for r in b["rows"] if r["seq"] in b["resets"]),
                        key=lambda r: r["ttft"] or 0, default=None)
            detail = ""
            if worst and worst["ttft"]:
                detail = (" %s, round #%s, re-read %s tokens and took %s to the first answer token."
                          % ("The slowest of them" if len(b["resets"]) > 1 else "That round",
                             worst["seq"], f"{worst['prompt']:,}", _secs(worst["ttft"])))
            out.append({"lead": "%s lost its prompt cache in %s"
                                % (b["label"], ", ".join("#%s" % s for s in b["resets"])),
                        "text": "%s sent the whole prompt as new tokens, so the model read it "
                                "again from the start.%s"
                                % ("Those rounds" if len(b["resets"]) > 1 else "That round",
                                   detail)})
        for c in b["tool_changes"]:
            out.append({"lead": "%s changed its tool list at round #%s" % (b["label"], c["seq"]),
                        "text": "%s tool definitions became %s. The tool definitions sit at the "
                                "front of the prompt, so everything cached behind them has to be "
                                "read again: the round that follows pays for the whole standing "
                                "prompt." % (c["from"], c["to"])})
        if b["standing"] and b["est_total"]:
            # both sides of this share are the proxy's own estimate, so they are comparable;
            # the model's reported tokens are not, and a rough estimate can look bigger than
            # the real count it stands for
            share = 100.0 * b["standing"] * b["count"] / b["est_total"]
            if share >= 25:
                out.append({"lead": "%s sends %s of its prompt as standing text"
                            % (b["label"], f"{share:.0f}%"),
                            "text": "System text and %s tool definitions are about %s estimated "
                                    "tokens in every round, against %s estimated tokens of "
                                    "conversation on average."
                                    % (b["tools"], f"{b['standing']:,}",
                                       f"{b['history_avg']:,.0f}" if b["history_avg"] else "0")})
        if b["prefill"] and b["warm_prefill"].get("med") and b["warm_prefill"]["med"] > b["prefill"] * 1.4:
            out.append({"lead": "%s reads much faster once its prompt is cached" % b["label"],
                        "text": "Across all of its rounds the model read %s prompt tokens per "
                                "second, but in the rounds that reused the cache it managed %s. "
                                "The cold rounds are what pull the average down."
                                % (_rate(b["prefill"]), _rate(b["warm_prefill"]["med"]))})
    if overall["slowest"]:
        s = overall["slowest"]
        out.append({"lead": "The slowest round was #%s" % s["seq"],
                    "text": "%s to the first answer token, on a prompt of %s tokens of which the "
                            "model had already seen %s."
                            % (_secs(s["ttft"]), f"{s['prompt']:,}",
                               f"{s['cached']:,}" if s["cached"] else "none")})
    if not overall["timed"]:
        out.append({"lead": "This log carries no timings",
                    "text": "The rounds were captured before the proxy measured how long an "
                            "answer took, so the speed figures are empty. Capture some rounds with "
                            "the current proxy and this page fills itself in."})
    return out


def _same_speed(big, small):
    """One clause on whether the two clients wrote at the same speed, which is how the
    page tells a prompt-size difference apart from a model-speed difference."""
    if not big["decode"] or not small["decode"]:
        return "One of them has no decode rate to compare."
    ratio = big["decode"] / small["decode"]
    if 0.8 <= ratio <= 1.25:
        return ("They wrote at the same speed (%s vs %s answer tokens per second), so the whole "
                "gap is prompt size and round count, not the model."
                % (_rate(big["decode"]), _rate(small["decode"])))
    return "They also wrote at different speeds: %s vs %s answer tokens per second." % (
        _rate(big["decode"]), _rate(small["decode"]))


# ------------------------------------------------------------------------- the charts

def _nice_max(value):
    """The top of an axis: a round number just above the largest bar."""
    if not value or value <= 0:
        return 1.0
    e = math.floor(math.log10(value))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * (10.0 ** e) >= value:
            return step * (10.0 ** e)
    return 10.0 ** (e + 1)


def bars(cats, stacks, colors, w=640, h=230, fmt=lambda v: f"{v:,.0f}", titles=None, cold=()):
    """Stacked bars, one per round: `stacks[i]` is the list of segments of bar i, bottom
    up. A cold round gets a red outline, because it is the one worth looking at."""
    pad_l, pad_r, pad_t, pad_b = 58, 12, 14, 30
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    totals = [sum(s) for s in stacks]
    top = _nice_max(max(totals) if totals else 1)
    svg = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (w, h)]
    for i in range(5):
        value = top * i / 4.0
        y = pad_t + ph * (1 - i / 4.0)
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                   % (pad_l, y, w - pad_r, y))
        svg.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>'
                   % (pad_l - 6, y + 4, esc(fmt(value))))
    band = pw / max(len(cats), 1)
    bar_w = band * 0.72
    every = 1 if len(cats) <= 14 else math.ceil(len(cats) / 10)
    for i, (cat, stack) in enumerate(zip(cats, stacks)):
        x = pad_l + band * i + (band - bar_w) / 2.0
        acc = 0.0
        for k, value in enumerate(stack):
            if value > 0:
                y0 = pad_t + ph * (1 - acc / top)
                y1 = pad_t + ph * (1 - (acc + value) / top)
                edge = ' stroke="%s" stroke-width="1.4"' % BAD if i in cold else ""
                svg.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"%s/>'
                           % (x, y1, bar_w, max(y0 - y1, 0.6), colors[k % len(colors)], edge))
                acc += value
        tip = (titles[i] if titles else "%s: %s" % (cat, fmt(totals[i])))
        svg.append('<rect x="%.1f" y="%d" width="%.1f" height="%.1f" fill="transparent">'
                   '<title>%s</title></rect>'
                   % (pad_l + band * i, pad_t, band, ph, esc(tip)))
        if i % every == 0:
            svg.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                       % (x + bar_w / 2.0, h - pad_b + 16, esc(cat)))
    svg.append("</svg>")
    return "".join(svg)


def hstack(labels, segs, colors, w=640, fmt=lambda v: f"{v:,.0f}"):
    """Horizontal stacked bars, one per thread: what one round of it is made of."""
    row_h, pad_l, pad_r, pad_t = 44, 170, 16, 22
    h = pad_t + row_h * len(labels) + 24
    pw = w - pad_l - pad_r
    top = max(sum(s) for s in segs) if segs else 1
    svg = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (w, h)]
    for i in range(5):
        x = pad_l + pw * i / 4.0
        svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>'
                   % (x, pad_t - 6, x, h - 22))
        svg.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                   % (x, h - 8, esc(fmt(top * i / 4.0))))
    for i, (label, seg) in enumerate(zip(labels, segs)):
        y = pad_t + row_h * i
        x = pad_l
        for k, value in enumerate(seg):
            if value > 0:
                width = pw * value / top
                svg.append('<rect x="%.1f" y="%d" width="%.1f" height="20" fill="%s">'
                           '<title>%s</title></rect>'
                           % (x, y, width, colors[k % len(colors)],
                              esc("%s: %s" % (label, fmt(value)))))
                if width > 46:
                    svg.append('<text x="%.1f" y="%d" class="inbar" text-anchor="middle">%s</text>'
                               % (x + width / 2.0, y + 14, esc(fmt(value))))
                x += width
        svg.append('<text x="%d" y="%d" class="ax" text-anchor="end">%s</text>'
                   % (pad_l - 8, y + 14, esc(label)))
    svg.append("</svg>")
    return "".join(svg)


def step(xs, series, colors, w=640, h=190, fmt=lambda v: f"{v:,.0f}"):
    """Step lines over the whole source: how many tool definitions each conversation sent
    at every round. A step is where a client changed its tool set."""
    pad_l, pad_r, pad_t, pad_b = 46, 12, 12, 28
    pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
    vals = [v for _, vs in series for v in vs if v is not None]
    top = _nice_max(max(vals) if vals else 1)
    svg = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (w, h)]
    for i in range(5):
        y = pad_t + ph * (1 - i / 4.0)
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                   % (pad_l, y, w - pad_r, y))
        svg.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>'
                   % (pad_l - 6, y + 4, esc(fmt(top * i / 4.0))))

    def x_at(i):
        return pad_l + pw * (i + 0.5) / max(len(xs), 1)

    def y_at(value):
        return pad_t + ph * (1 - value / top)

    for (label, values), color in zip(series, colors):
        points, last = [], None
        for i, value in enumerate(values):
            if value is None:
                continue
            if last is not None and last[1] != value:
                points.append((x_at(i), y_at(last[1])))
            points.append((x_at(i), y_at(value)))
            last = (i, value)
        if points:
            svg.append('<path d="M%s" fill="none" stroke="%s" stroke-width="2"/>'
                       % (" L".join("%.1f %.1f" % p for p in points), color))
        for i, value in enumerate(values):
            if value is not None:
                svg.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s"><title>%s</title></circle>'
                           % (x_at(i), y_at(value), color,
                              esc("%s round %s: %s" % (label, xs[i], fmt(value)))))
    every = 1 if len(xs) <= 14 else math.ceil(len(xs) / 12)
    for i in range(0, len(xs), every):
        svg.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                   % (x_at(i), h - pad_b + 16, esc(str(xs[i]))))
    svg.append("</svg>")
    return "".join(svg)


def legend(items):
    return ('<div class="lgd">' + "".join('<span><i style="background:%s"></i>%s</span>'
                                          % (color, esc(text)) for color, text in items) + "</div>")


# -------------------------------------------------------------------------- the page

EXTRA_CSS = """
/* the analysis page borrows the Context page's look and adds only what the charts need */
.chart { width:100%; height:auto; display:block; margin:4px 0 2px }
.grid { stroke:var(--line); stroke-width:1 } .ax { font-size:10px; fill:var(--dim) }
.inbar { font-size:10px; fill:#0f1115 }
.lgd { color:var(--dim); font-size:12px; margin:2px 0 4px }
.lgd i { display:inline-block; width:10px; height:10px; margin:0 5px 0 12px; border-radius:2px }
.lgd span:first-child i { margin-left:0 }
.cardhead h3 { margin:0; font-size:15px }
.card .kpis { margin:12px 0 6px }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px }
@media (max-width:980px) { .grid2 { grid-template-columns:1fr } }
tbody tr.cold td { background:#2a1c1e } tbody tr.chg td { background:#1c2532 }
.who { color:var(--dim); font-size:12.5px }
.who code { font-family:var(--mono); font-size:11.5px }
ol.say { margin:6px 0 0; padding-left:22px } ol.say li { margin:9px 0 }
ol.say b { color:var(--ink) }
.threads { display:flex; flex-wrap:wrap; gap:10px; margin:10px 0 2px }
.chip { background:#1b2029; border:1px solid var(--line); border-radius:999px;
        padding:3px 11px; font-size:12.5px; color:var(--dim) }
.chip b { color:var(--ink) }
"""


def _thread_charts(block, color):
    """The four charts of one conversation: what it sent, how long the model took to
    start, and how fast it read and wrote."""
    rows = block["rows"]
    cats = [str(r["seq"]) for r in rows]
    cold = [i for i, r in enumerate(rows) if r["cold"]]

    def tips(fmt):
        out = []
        for r in rows:
            try:
                out.append(fmt(r))
            except (TypeError, ValueError):
                out.append("round %s: not measured" % r["seq"])
        return out

    charts = [
        ("Prompt tokens per round",
         bars(cats, [[r["cached"], r["new"]] for r in rows], [REUSE, NEW],
              titles=tips(lambda r: "round %s: %s prompt tokens = %s already known + %s new"
                          % (r["seq"], f"{r['prompt']:,}", f"{r['cached']:,}", f"{r['new']:,}")),
              cold=cold),
         legend([(REUSE, "the model had already seen it"), (NEW, "new: read again this round"),
                 (BAD, "cold round: nothing was cached (red outline)")])),
        ("Time to the first answer token (seconds)",
         bars(cats, [[(r["ttft"] or 0) / 1000.0] for r in rows], [color],
              fmt=lambda v: "%.1f" % v,
              titles=tips(lambda r: "round %s: %s to first token, the whole round %s"
                          % (r["seq"], _secs(r["ttft"]), _secs(r["ms"]))), cold=cold),
         legend([(color, "seconds from sending the request to the first answer token")])),
        ("Reading speed: prompt tokens per second",
         bars(cats, [[r["prefill"] or 0] for r in rows], [ACCENT],
              titles=tips(lambda r: "round %s: %s prompt tokens in %s = %s tokens per second"
                          % (r["seq"], f"{r['prompt']:,}", _secs(r["ttft"]), _rate(r["prefill"]))),
              cold=cold),
         legend([(ACCENT, "prompt tokens divided by the time to the first answer token")])),
        ("Writing speed: answer tokens per second",
         bars(cats, [[r["decode"] or 0] for r in rows], [REUSE],
              titles=tips(lambda r: "round %s: %s answer tokens in %s = %s tokens per second"
                          % (r["seq"], f"{r['out']:,}",
                             _secs((r["ms"] or 0) - (r["ttft"] or 0)), _rate(r["decode"])))),
         legend([(REUSE, "answer tokens divided by the time after the first one"),
                 ("#20242c", "no bar: the answer came in one piece, so the two cannot be split")])),
    ]
    html = []
    for title, svg, note in charts:
        html.append('<div class="card"><div class="cardhead"><h3>%s</h3></div>%s%s</div>'
                    % (esc(title), svg, note))
    return html


def _thread_table(block):
    rows = ['<table><thead><tr><th class="l">round</th><th>time</th><th>prompt</th>'
            '<th>cached</th><th>new</th><th>out</th><th>first token</th><th>round</th>'
            '<th>read tok/s</th><th>write tok/s</th><th>tools</th><th>finish</th>'
            '<th class="l">tools it called</th></tr></thead><tbody>']
    for r in block["rows"]:
        cls = ' class="cold"' if r["cold"] else (' class="chg"' if r["tools_changed"] else "")
        calls = ", ".join(r["calls"][:4])
        if len(r["calls"]) > 4:
            calls += " +%d" % (len(r["calls"]) - 4)
        rows.append('<tr%s><td class="l num">#%s</td><td class="dim">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="dim">%s</td><td class="l dim">%s</td></tr>'
                    % (cls, r["seq"], esc(r["ts"]), _num(r["prompt"]), _num(r["cached"]),
                       _num(r["new"]), _num(r["out"]), _secs(r["ttft"]), _secs(r["ms"]),
                       _num(r["prefill"]) if r["prefill"] else "&mdash;",
                       _num(r["decode"], 1) if r["decode"] else "&mdash;",
                       _num(r["tools"]), esc(r["reason"] or ""), esc(calls)))
    tm = {"rounds": block["timed_rounds"], "prefill": block["prefill"],
          "decode": block["decode"], "whole": block["whole"],
          "whole_rounds": block["whole_rounds"]}
    rows.append('<tr class="foot"><td class="l">%s rounds</td><td></td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td><td colspan="2">%s</td>'
                '<td class="num">%s</td><td colspan="2"></td></tr>'
                % (block["count"], f"{block['prompt']:,}", f"{block['cached']:,}",
                   f"{block['new']:,}", f"{block['out']:,}",
                   _secs(block["ttft"]["med"]) if block["ttft"] else "&mdash;",
                   _secs(block["round_ms"]["med"]) if block["round_ms"] else "&mdash;",
                   ctxpage._thread_tps(tm), _num(block["tools_max"])))
    rows.append("</tbody></table>")
    return "".join(rows)


def _thread_card(block, color, charted):
    head = ('<div class="cardhead"><h3>%s</h3><span class="who" title="%s">%s</span>'
            '<span class="who">rounds #%s&ndash;#%s &middot; %s to %s</span></div>'
            % (esc(block["name"]), esc(block["client"]), esc(block["label"]),
               block["first"], block["last"], esc(block["first_ts"]), esc(block["last_ts"])))
    cards = "".join([
        kpi("rounds", f"{block['count']}", "%s timed" % block["timed_rounds"]),
        kpi("prompt tokens", f"{block['prompt']:,}",
            "%s per round" % f"{block['avg_prompt']:,.0f}"),
        kpi("cache reused", ("%.1f%%" % block["cache_pct"]) if block["cache_pct"] is not None
            else "&mdash;", "%s new tokens" % f"{block['new']:,}", "grn"),
        kpi("model time", ("%.0f s" % block["wall"]) if block["wall"] else "&mdash;",
            "%s rounds measured" % block["timed_rounds"]),
        kpi("first token", _secs(block["ttft"]["med"]) if block["ttft"] else "&mdash;",
            ("slowest %s" % _secs(block["ttft"]["max"])) if block["ttft"] else "not timed"),
        kpi("read / write", ctxpage._thread_tps({
            "rounds": block["timed_rounds"], "prefill": block["prefill"],
            "decode": block["decode"], "whole": block["whole"],
            "whole_rounds": block["whole_rounds"]}),
            ("cached rounds read at %s tok/s" % _rate(block["warm_prefill"]["med"]))
            if block["warm_prefill"].get("med") else ""),
        kpi("tools sent", _num(block["tools"]),
            ("%s tool definitions" % block["tools"]) if block["tools"] else "none"),
        kpi("standing prompt", _num(block["standing"]),
            "system + tools, every round" if block["standing"] else "not estimated"),
    ])
    parts = ['<div class="card">%s<div class="kpis">%s</div>'
             '<p class="legend">%s</p></div>'
             % (head, cards, _thread_note(block))]
    if charted:
        parts.extend(_thread_charts(block, color))
        parts.append('<div class="card"><div class="cardhead"><h3>Round by round</h3></div>%s'
                     '<p class="legend">A <b>red row</b> sent nothing cached: the model read the '
                     'whole prompt again. A <b>blue row</b> is the round where the client changed '
                     'its tool definitions.</p></div>' % _thread_table(block))
    else:
        parts.append('<div class="card"><div class="cardhead"><h3>Round by round</h3></div>%s</div>'
                     % _thread_table(block))
    return "".join(parts)


def _thread_note(block):
    """One line under a thread's figures: the cache resets and tool changes it made."""
    bits = []
    if block["resets"]:
        bits.append("cache reset at " + ", ".join("#%s" % s for s in block["resets"]))
    if block["tool_changes"]:
        bits.append("tool list changed at " + ", ".join(
            "#%s (%s&rarr;%s)" % (c["seq"], c["from"], c["to"]) for c in block["tool_changes"]))
    if block["root_changes"]:
        bits.append("root prompt changed at " + ", ".join("#%s" % s for s in block["root_changes"]))
    if block["finishes"]:
        bits.append("finished: " + ", ".join("%s &times;%s" % (esc(k), v)
                                             for k, v in sorted(block["finishes"].items())))
    if block["call_names"]:
        bits.append("called %s tool time%s across %s tools"
                    % (block["calls"], "" if block["calls"] == 1 else "s", len(block["call_names"])))
    if not bits:
        bits.append("nothing changed mid-conversation: the same standing prompt and the same "
                    "tool definitions every round")
    return " &middot; ".join(bits)


def render_html(data):
    """The analysis of one source as one self-contained page."""
    overall = data["overall"]
    blocks = data["threads"]
    html = ['<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Capture analysis &middot; %s</title>'
            '<style>%s%s</style></head><body><header><h1>Capture analysis</h1>'
            '<div class="sub" title="%s">%s</div></header><main>'
            % (esc(data["label"]), ctxpage.CSS, EXTRA_CSS, esc(data["source"]),
               " &middot; ".join([part for part in [
                   esc(data["label"]),
                   "%s rounds" % f"{data['rounds_total']:,}",
                   "%s conversations" % len(blocks),
                   "model %s" % esc(data["model"] or "unknown"),
                   "%s rounds hidden from the view" % data["hidden"] if data["hidden"] else "",
               ] if part]))]

    html.append('<div class="part"><h2><span class="pn">1</span>Whole source'
                '<span class="what">every round the view shows, all conversations together</span></h2>'
                '<div class="kpis">%s</div></div>' % "".join([
                    kpi("rounds", f"{overall['rounds']:,}",
                        "%s of them timed" % overall["timed"]),
                    kpi("conversations", f"{overall['threads']:,}",
                        "each one broken out below"),
                    kpi("prompt tokens", f"{overall['prompt']:,}",
                        "sent over every round"),
                    kpi("cache reused", ("%.1f%%" % overall["cache_pct"])
                        if overall["cache_pct"] is not None else "&mdash;",
                        "%s tokens read again" % f"{overall['new']:,}", "grn"),
                    kpi("answer tokens", f"{overall['out']:,}",
                        "%s chars of thinking" % f"{overall['thinking']:,}"),
                    kpi("model time", "%.0f s" % overall["wall"],
                        "the rounds the proxy timed"),
                    kpi("cache resets", f"{overall['cold']:,}",
                        "cold rounds after the first of a conversation",
                        "amb" if overall["cold"] else ""),
                    kpi("tool changes", f"{overall['tool_changes']:,}",
                        "rounds that re-sent a different tool set",
                        "amb" if overall["tool_changes"] else ""),
                ]))

    html.append('<div class="part"><h2><span class="pn">2</span>What this says'
                '<span class="what">the figures, read out loud</span></h2><div class="card"><ol class="say">')
    for item in data["findings"]:
        html.append("<li><b>%s</b> &mdash; %s</li>" % (esc(item["lead"]), esc(item["text"])))
    html.append("</ol></div></div>")

    html.append('<div class="part"><h2><span class="pn">3</span>Conversations'
                '<span class="what">one block per thread, the biggest first</span></h2>'
                '<div class="threads">%s</div></div>'
                % "".join('<span class="chip"><b>%s</b> &middot; %s &middot; %s rounds &middot; %s tok'
                          % (esc(b["name"]), esc(b["label"]), b["count"], f"{b['prompt']:,}")
                          for b in blocks))

    charted = 0
    for i, block in enumerate(blocks):
        color = PALETTE[i % len(PALETTE)]
        take = charted < MAX_CHARTED and block["count"] >= 2
        if take:
            charted += 1
        html.append('<div class="part"><h2><span class="pn">%s</span>%s'
                    '<span class="what" title="%s">%s</span></h2>%s</div>'
                    % (i + 1, esc(block["name"]), esc(block["client"]), esc(block["label"]),
                       _thread_card(block, color, take)))

    if len(blocks) >= 2:
        html.append('<div class="part"><h2><span class="pn">%s</span>Side by side'
                    '<span class="what">where the two clients part company</span></h2>'
                    % (len(blocks) + 1))
        html.append('<div class="grid2">')
        html.append('<div class="card"><div class="cardhead"><h3>What one round is made of</h3></div>%s%s'
                    '<p class="legend">Estimated tokens: the standing text that goes out every '
                    'round, against the conversation that grows round by round. The standing part '
                    'is what a client could still make smaller.</p></div>'
                    % (hstack(["%s, per round" % b["label"] for b in blocks],
                              [[b["standing"] or 0, b["history_avg"] or 0] for b in blocks],
                              ["#6b7280", "#334155"]),
                       legend([("#6b7280", "system text + tool definitions"),
                               ("#334155", "conversation history, average round")])))
        seqs = sorted({r["seq"] for b in blocks for r in b["rows"]})
        html.append('<div class="card"><div class="cardhead"><h3>Tool definitions sent, per round</h3></div>%s%s'
                    '<p class="legend">A step down or up mid-conversation is a client changing its '
                    'tool set. Everything cached behind those definitions has to be read again, '
                    'which is why that round re-prefills tens of thousands of tokens.</p></div>'
                    % (step(seqs, [(b["name"], [(next((r["tools"] for r in b["rows"]
                                                       if r["seq"] == s), None)) for s in seqs])
                                   for b in blocks],
                            [PALETTE[i % len(PALETTE)] for i in range(len(blocks))]),
                       legend([(PALETTE[i % len(PALETTE)], b["name"])
                               for i, b in enumerate(blocks)])))
        html.append("</div>")
        html.append('<div class="card"><div class="cardhead"><h3>The same figures, in one table</h3></div>'
                    '<table><thead><tr><th class="l">conversation</th><th class="l">client</th>'
                    '<th>rounds</th><th>prompt</th><th>cached</th><th>new</th><th>out</th>'
                    '<th>cache %</th><th>model time</th><th>first token median</th>'
                    '<th>read tok/s</th><th>write tok/s</th><th>cached-round read</th>'
                    '<th>tools</th><th>standing</th><th>resets</th></tr></thead><tbody>')
        for block in blocks:
            html.append('<tr><td class="l"><b>%s</b></td><td class="l dim">%s</td>'
                        '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                        '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                        '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                        '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                        '<td class="num">%s</td><td class="num">%s</td></tr>'
                        % (esc(block["name"]), esc(block["label"]), block["count"],
                           f"{block['prompt']:,}", f"{block['cached']:,}", f"{block['new']:,}",
                           f"{block['out']:,}",
                           ("%.1f" % block["cache_pct"]) if block["cache_pct"] is not None else "&mdash;",
                           ("%.0f s" % block["wall"]) if block["wall"] else "&mdash;",
                           _secs(block["ttft"]["med"]) if block["ttft"] else "&mdash;",
                           _num(block["prefill"]), _num(block["decode"], 1),
                           _num(block["warm_prefill"].get("med")), _num(block["tools"]),
                           _num(block["standing"]), len(block["resets"])))
        html.append("</tbody></table></div></div>")

    html.append('<p class="legend">Every figure here is the one the Context page shows for the same '
                'round: the tokens the model reported, and the proxy\'s own clock for how long it '
                'took. A dash means the log does not say.</p>')
    html.append("</main></body></html>")
    return "".join(html)


def main():
    ap = argparse.ArgumentParser(
        description="analyse one capture file and write the Capture Analysis page")
    ap.add_argument("capture", help="a .ctxcap file, or a capture log folder")
    ap.add_argument("-o", "--out", default="", help="where to write the page "
                                                    "(default: <capture>-analysis.html beside it)")
    args = ap.parse_args()
    cap = ctxstore.Capture(args.capture)
    reqs, resps, order = ctxstore._events(cap)
    links = ctxlink.attach([reqs[s] for s in order], cap, resps)
    window, where = cap.window()
    data = analyze(cap, reqs, resps, order, order, links, window, where)
    out = args.out or (str(cap.path)[:-len(".ctxcap")] + "-analysis.html"
                       if str(cap.path).endswith(".ctxcap") else str(cap.path) + "-analysis.html")
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(render_html(data))
    print("wrote %s (%d rounds, %d conversations)"
          % (out, data["overall"]["rounds"], data["overall"]["threads"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
