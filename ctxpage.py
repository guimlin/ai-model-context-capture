"""Make a single self-contained HTML page of what your agents sent to Strata.

  python ctxpage.py                     writes report.html next to this script
  python ctxpage.py --out name.html     a different file
  python ctxpage.py --md report.md      the same content as Markdown
  python ctxpage.py --max-chars 40000   more (or less) text inline per block
  python ctxpage.py --last 200          only the newest N rounds in the table
  python ctxpage.py --max-context 32768 force the window instead of reading it

The page holds, for every round: Strata's own token counts (how much context it
read, how much of that its prompt cache already had, how much had to be read new,
answer tokens), the size of that context against the model's context window, the
exact context that was sent (system prompt, tool definitions, every message) and
everything the model answered - text, thinking, and tool calls with their
arguments. A picker at the top shows one chosen round at a time; the full list
stays below it. Nothing is fetched from the network; the file opens offline.
"""

import argparse
import glob
import html
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.environ.get("CTX_LOG_DIR") or os.path.join(HERE, "logs")
LOG = os.path.join(LOGDIR, "requests.jsonl")


def _read(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def find_context_window(forced=0):
    """The model's context window, as Strata was told to run it. Returns (tokens, source)."""
    if forced:
        return forced, "given on the command line"
    env = os.environ.get("CTX_MAX_CONTEXT")
    if env and env.isdigit():
        return int(env), "the CTX_MAX_CONTEXT variable"
    best, src = 0, ""
    for path in glob.glob(os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Strata", "install", "*.json")):
        cfg = _read(path)
        try:
            cfg = json.loads(cfg) if cfg else {}
        except Exception:
            continue
        args = cfg.get("args") or cfg.get("args_written") or []
        if "--max-context" in args:
            i = args.index("--max-context")
            if i + 1 < len(args) and str(args[i + 1]).isdigit():
                n = int(args[i + 1])
                if n > best:
                    best, src = n, os.path.basename(path)
    return best, ("Strata's run config %s" % src) if best else ""


def load():
    if not os.path.exists(LOG):
        raise SystemExit("no log yet: %s\nStart the logger first (Start-ContextMonitor.ps1)." % LOG)
    reqs, resps, order = {}, {}, []
    for line in _read(LOG).splitlines():
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


def body_of(entry):
    raw = _read(os.path.join(LOGDIR, entry.get("payload_file") or ""))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def usage_of(resp):
    u = (resp or {}).get("usage") or {}
    prompt = u.get("prompt_tokens") or u.get("input_tokens")
    out = u.get("completion_tokens") or u.get("output_tokens")
    details = u.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = u.get("cached_tokens")
    return prompt, cached, out


def parts_of(body):
    """Split one request body into (api, system_text, tools, messages) for display."""
    if not isinstance(body, dict):
        return "?", "", [], []
    tools = body.get("tools") or []
    if ("system" in body and "messages" in body) or "anthropic_version" in body:
        api = "anthropic-messages"
        system = body.get("system")
        if isinstance(system, str):
            system_text = system
        else:
            system_text = "\n\n".join(
                (s.get("text") if isinstance(s, dict) else str(s)) or "" for s in (system or []))
        msgs = [(m.get("role", "?"), m.get("content")) for m in (body.get("messages") or [])
                if isinstance(m, dict)]
        return api, system_text, tools, msgs
    if "messages" in body:
        api = "openai-chat"
        msgs, system_bits = [], []
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "?")
            if role in ("system", "developer"):
                system_bits.append(_flat(m.get("content")))
            else:
                msgs.append((role, m.get("content")))
                if m.get("tool_calls"):
                    for j, tc in enumerate(m["tool_calls"]):
                        fn = tc.get("function") or {}
                        msgs.append(("assistant tool call %d: %s" % (j, fn.get("name") or "?"),
                                     fn.get("arguments")))
        return api, "\n\n".join(system_bits), tools, msgs
    if "input" in body or "instructions" in body:
        api = "openai-responses"
        msgs = [(m.get("role", "?"), m.get("content")) for m in (body.get("input") or [])
                if isinstance(m, dict)]
        return api, _flat(body.get("instructions")), tools, msgs
    return "other", "", tools, [(k, v) for k, v in body.items() if k not in ("model", "stream")]


def _flat(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("image", "image_url"):
                    out.append("[image]")
                elif part.get("type") == "tool_result":
                    out.append("[tool result] " + _flat(part.get("content")))
                elif part.get("type") == "tool_use":
                    out.append("[tool use %s] %s" % (part.get("name"),
                                                     json.dumps(part.get("input", {}), ensure_ascii=False)))
                else:
                    out.append(str(part.get("text") or json.dumps(part, ensure_ascii=False)))
        return "\n".join(out)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=2)
    return str(content)


def _round_view(req, resp):
    """One round, ready to display: Strata's counts plus the context split into pieces."""
    prompt, cached, out = usage_of(resp)
    est = req.get("est_prompt_tokens") or 0
    api, system_text, tools, msgs = parts_of(body_of(req))
    pieces = []
    if system_text:
        pieces.append(("system prompt", system_text))
    if tools:
        listing = []
        for t in tools:
            fn = (t.get("function") or t) if isinstance(t, dict) else {}
            dump = json.dumps(t, ensure_ascii=False, indent=2)
            listing.append("### %s (%s chars)\n%s" % (fn.get("name") or "?", f"{len(dump):,}", dump))
        pieces.append(("%d tool definition%s" % (len(tools), "" if len(tools) == 1 else "s"),
                       "\n\n".join(listing)))
    for i, (role, content) in enumerate(msgs):
        label = "#%d %s" % (i, role)
        if isinstance(content, list):
            label += " (%d parts)" % len(content)
        pieces.append((label, _flat(content)))
    chars = sum(len(t) for _, t in pieces)
    # how many characters one token really was for THIS request, from Strata's own count
    cplit = (chars / prompt) if prompt and chars else 0.0
    return {"prompt": prompt, "cached": cached or 0, "out": out, "est": est, "api": api,
            "pieces": pieces, "chars": chars, "cplit": cplit,
            "kb": (req.get("body_bytes") or 0) / 1024.0, "finish": (resp or {}).get("finish_reason")}


def _tok(chars, cplit):
    if cplit:
        return "\u2248%s tok" % f"{max(1, round(chars / cplit)):,}"
    return "\u2248%s tok (guess)" % f"{max(1, round(chars / 3.5)):,}"


CSS = """
:root { --bg:#0f1115; --panel:#161a21; --line:#252b35; --ink:#e6e9ef; --dim:#98a2b3;
        --accent:#5aa9ff; --new:#ffb454; --reuse:#3fb27f; --bad:#ff6b6b; --mono:ui-monospace,Consolas,monospace }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,Segoe UI,Roboto,sans-serif }
header { padding:24px 30px 16px; border-bottom:1px solid var(--line) }
h1 { margin:0 0 6px; font-size:22px } .sub { color:var(--dim); font-size:13px }
main { padding:20px 30px 60px; max-width:1560px; margin:0 auto }
h2 { font-size:16px; margin:26px 0 10px }
table { border-collapse:collapse; width:100%; font-size:13.5px }
th,td { padding:6px 9px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap }
th { color:var(--dim); font-weight:600; font-size:11.5px; text-transform:uppercase; letter-spacing:.04em }
td.l,th.l { text-align:left }
.num { font-family:var(--mono) } .num.strong { color:var(--ink); font-weight:600 } .dim { color:var(--dim) }
tbody tr { cursor:pointer } tbody tr:hover td { background:#1a1f27 }
tbody tr.picked td { background:#1e2733; box-shadow:inset 3px 0 0 var(--accent) }
.bar { display:flex; height:12px; border-radius:3px; overflow:hidden; min-width:150px; background:#20242c;
       border:1px solid #2b313c }
.bar i { display:block; height:100%; min-width:0 } .reuse { background:var(--reuse) } .new { background:var(--new) }
.bar i.est { background:#6b7280 } .over { background:var(--bad) }
tr.foot td { border-top:2px solid var(--line); border-bottom:none; color:var(--dim); font-weight:600 }
.legend { color:var(--dim); font-size:12.5px; line-height:1.5 } .legend b { color:var(--reuse) }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin:16px 0 }
.cardhead { display:flex; flex-wrap:wrap; gap:14px; align-items:baseline; margin-bottom:8px }
.seq { font-family:var(--mono); font-size:18px; color:var(--accent) }
.meta { color:var(--dim); font-size:13px }
.kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin:10px 0 14px }
.kv div { border:1px solid var(--line); border-radius:9px; padding:8px 11px }
.kv span { display:block; color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.05em }
.kv b { font-family:var(--mono); font-size:16px; font-weight:500 }
details { border:1px solid var(--line); border-radius:9px; margin:7px 0; background:#12161c }
summary { cursor:pointer; padding:9px 12px; color:var(--ink); font-size:13.5px }
summary .role { font-weight:600 } summary .size { color:var(--dim); font-family:var(--mono); font-size:12px; margin-left:8px }
pre { margin:0; padding:12px 14px; border-top:1px solid var(--line); white-space:pre-wrap; word-break:break-word;
      font-family:var(--mono); font-size:12.8px; color:#cfd6e4; max-height:520px; overflow:auto }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:16px } @media (max-width:1150px){ .cols{grid-template-columns:1fr} }
h3 { margin:0 0 6px; font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--dim) }
h3 .tok { color:var(--ink); font-family:var(--mono); font-size:12px; text-transform:none; letter-spacing:0 }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; border:1px solid var(--line); color:var(--dim) }
.badge.ok { color:var(--reuse); border-color:#274b3a } .badge.warn { color:var(--new); border-color:#4b3b22 }
.badge.err { color:var(--bad); border-color:#4b2626 }
.note { color:var(--dim); font-size:12.5px; margin-top:10px }
a { color:var(--accent) }
.picker { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line);
          padding:10px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap }
.picker select,.picker button { background:#1b2029; color:var(--ink); border:1px solid var(--line);
          border-radius:8px; padding:7px 11px; font:inherit; cursor:pointer }
.picker select { min-width:300px; font-family:var(--mono); font-size:13px }
#focus:empty { display:none } #focus .card { border-color:#33455c; margin-top:12px }
#focus .card.focused { box-shadow:0 0 0 1px var(--accent) }
#hint { color:var(--dim); font-size:12.5px }
.num.over { color:var(--bad) }
.pager { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:10px 0 8px }
.pager button { background:#1b2029; color:var(--ink); border:1px solid var(--line); border-radius:8px;
                padding:6px 12px; font:inherit; cursor:pointer }
.pager button:disabled { opacity:.35; cursor:default }
#pinfo { font-family:var(--mono); font-size:13px }
"""

JS = """
var pick = document.getElementById('pick'), focus = document.getElementById('focus'),
    ftitle = document.getElementById('ftitle'), hint = document.getElementById('hint'),
    rows = [], order = [];
var size = PAGE_SIZE, page = 0;
Array.prototype.forEach.call(document.querySelectorAll('tbody tr[data-seq]'), function(r){
  rows.push(r); order.push(r.getAttribute('data-seq')); });
function pages(){ return Math.max(1, Math.ceil(rows.length / size)); }
function drawPage(){
  page = Math.max(0, Math.min(page, pages() - 1));
  rows.forEach(function(r, i){ r.style.display = (i >= page*size && i < (page+1)*size) ? '' : 'none'; });
  var first = order[page*size], last = order[Math.min(rows.length - 1, (page+1)*size - 1)];
  document.getElementById('pinfo').textContent = 'page ' + (page + 1) + ' of ' + pages()
    + '  \\u00b7  rounds ' + first + ' to ' + last + '  (of ' + rows.length + ')';
  document.getElementById('pprev').disabled = page <= 0;
  document.getElementById('pnext').disabled = page >= pages() - 1;
}
// prev / next move a whole page of rounds, never a single round
document.getElementById('pprev').addEventListener('click', function(){ page--; drawPage(); });
document.getElementById('pnext').addEventListener('click', function(){ page++; drawPage(); });
function show(v){
  rows.forEach(function(r){ r.classList.toggle('picked', r.getAttribute('data-seq') === v); });
  pick.value = v || '';
  focus.innerHTML = '';
  ftitle.style.display = v ? '' : 'none';
  if(!v){ hint.textContent = 'pick a round, or click a row: its details open in the window below the table'; return; }
  var i = order.indexOf(v);
  if(i >= 0 && Math.floor(i / size) !== page){ page = Math.floor(i / size); drawPage(); }
  var card = document.getElementById('r' + v);
  if(!card) { hint.textContent = 'round ' + v + ' is not in this file (its text was trimmed out)'; return; }
  var copy = card.cloneNode(true);
  copy.removeAttribute('id');            // keep ids unique while the full list stays below
  copy.classList.add('focused');
  focus.appendChild(copy);
  hint.textContent = 'round ' + v + ' of ' + order.length
    + '  \\u00b7  \\u2190 \\u2192 move ' + size + ' rounds at a time'
    + '  \\u00b7  shift + \\u2190 \\u2192 one round'
    + (i > 0 ? '  \\u00b7  round before: ' + order[i-1] : '')
    + (i >= 0 && i < order.length-1 ? '  \\u00b7  round after: ' + order[i+1] : '');
  ftitle.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
pick.addEventListener('change', function(){ show(pick.value); });
document.querySelector('tbody').addEventListener('click', function(e){
  var row = e.target.closest ? e.target.closest('tr[data-seq]') : null;
  if(row) show(row.getAttribute('data-seq'));
});
function step(d){
  var i = order.indexOf(pick.value);
  if(i < 0) return show(order[d > 0 ? page*size : page*size + size - 1]);
  return show(order[Math.max(0, Math.min(order.length-1, i + d))]);
}
document.addEventListener('keydown', function(e){
  if(e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
  if(e.key === 'ArrowLeft'  || e.key === 'PageUp')  { page--; drawPage(); e.preventDefault(); return; }
  if(e.key === 'ArrowRight' || e.key === 'PageDown'){ page++; drawPage(); e.preventDefault(); return; }
  if(e.shiftKey && e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
  if(e.shiftKey && e.key === 'ArrowRight'){ step(1);  e.preventDefault(); }
});
drawPage();
show('');
"""


def esc(s):
    return html.escape(s or "")


def block(title, text, size_note, open_it=False):
    return ('<details%s><summary><span class="role">%s</span>'
            '<span class="size">%s</span></summary><pre>%s</pre></details>'
            % (" open" if open_it else "", esc(title), esc(size_note), esc(text) or "(empty)"))


def render_html(reqs, resps, order, max_chars, last, window, window_src, page_size=10):
    views = {s: _round_view(reqs[s], resps.get(s)) for s in order}
    exact = [v["prompt"] for v in views.values() if v["prompt"]]
    biggest_seq = max(order, key=lambda s: views[s]["prompt"] or views[s]["est"])
    biggest = views[biggest_seq]["prompt"] or views[biggest_seq]["est"]

    # the window is the real scale; the biggest round only gets a marker on the bar
    scale = window or biggest
    scale_what = ("the model's context window (%s tokens, from %s)" % (f"{window:,}", window_src)
                  if window else "the biggest round (%s tokens; I could not find the window, so "
                                                 "pass --max-context)" % f"{biggest:,}")

    shown_order = order[-last:] if last and len(order) > last else order
    rows, cards, options = [], [], []
    totals = {"prompt": 0, "cached": 0, "new": 0, "out": 0}
    prev_prompt = None

    for seq in shown_order:
        req, resp, v = reqs[seq], resps.get(seq), views[seq]
        prompt, cached, out, est = v["prompt"], v["cached"], v["out"], v["est"]
        shown = prompt or est
        new = (prompt - cached) if prompt else est
        if prompt:
            totals["prompt"] += prompt
            totals["cached"] += cached
            totals["new"] += new
            totals["out"] += out or 0
        grow = "" if prev_prompt is None or not prompt else ("%+d" % (prompt - prev_prompt))
        if prompt:
            prev_prompt = prompt
        status = (resp or {}).get("status")
        badge = "ok" if status == 200 else ("err" if status else "warn")
        calls = (resp or {}).get("tool_calls") or []
        pct_win = 100.0 * shown / scale if scale else 0.0
        pct_txt = ("%.1f%%" % pct_win) if pct_win >= 0.05 or pct_win == 0 else "%.2f%%" % pct_win
        over = pct_win > 100.0
        # bar: reused + new, drawn against the window; the biggest round is a tick mark
        seg = ""
        if prompt:
            seg += '<i class="reuse" style="width:%.2f%%" title="cache reused: %s tokens"></i>' % (
                min(100.0, 100.0 * cached / scale), f"{cached:,}")
            seg += '<i class="new" style="width:%.2f%%" title="read new: %s tokens"></i>' % (
                min(100.0, 100.0 * new / scale), f"{new:,}")
        else:
            seg += '<i class="est" style="width:%.2f%%" title="estimated context"></i>' % min(100.0, pct_win)
        if over:
            seg = '<i class="over" style="width:100%%" title="over the context window!"></i>'
        bar = '<div class="bar" title="%s tokens of %s">%s</div>' % (f"{shown:,}", f"{scale:,}", seg)
        flag = ' <span class="badge ok">biggest</span>' if seq == biggest_seq else ""
        rows.append(
            '<tr data-seq="%s"><td class="l num">%s%s</td><td class="l">%s</td><td class="l">%s</td>'
            '<td class="num strong">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num%s">%s</td>'
            '<td class="l">%s</td><td class="l"><span class="badge %s">%s</span></td></tr>'
            % (seq, seq, flag,
               esc((req.get("ts") or "")[11:19]), esc(v["api"]),
               f"{shown:,}" + ("" if prompt else " est"),
               f"{cached:,}" if prompt else "-", f"{new:,}", f"{out:,}" if out else "-",
               " over" if over else "", pct_txt,
               bar, badge, status or "no answer"))
        options.append('<option value="%s">#%s  %s  %s tok  %s  %s</option>' % (
            seq, pad4(seq), esc((req.get("ts") or "")[11:19]), f"{shown:,}".rjust(7),
            esc(v["api"]), esc(str(status or "no answer"))))

        ctx_html = []
        for title, text in v["pieces"]:
            ctx_html.append(block(title, text[:max_chars],
                                  "%s chars, %s" % (f"{len(text):,}", _tok(len(text), v["cplit"])),
                                  open_it=len(text) < 1200))
        if v["pieces"]:
            if v["cplit"]:
                ctx_html.append('<p class="note">the tokens on these blocks share out Strata\'s own count of '
                                '%s tokens for this request by size, so they add up to it. This request measured '
                                '~%.1f characters per token - that ratio moves with the text (repeated boilerplate '
                                'compresses hard, code less), so character guesses alone are not reliable.</p>'
                                % (f"{prompt:,}", v["cplit"]))
            else:
                ctx_html.append('<p class="note">no token count came back for this round, so these block tokens '
                                'are guesses at ~3.5 characters per token.</p>')

        answer_html = []
        rfile = _read(os.path.join(LOGDIR, (resp or {}).get("response_file") or ""))
        chunks = []
        if rfile:
            for chunk in rfile.split("\n\n=== "):
                head, _, rest = chunk.replace("=== ", "", 1).partition(" ===\n")
                chunks.append((head or "answer", rest))
        # characters per answer token, measured from Strata's own completion count
        ans_chars = sum(len(t) for _, t in chunks)
        aplit = (ans_chars / out) if out and ans_chars else 0.0
        for head, rest in chunks:
            answer_html.append(block(head, rest[:max_chars],
                                     "%s chars, %s" % (f"{len(rest):,}", _tok(len(rest), aplit)),
                                     open_it=len(rest) < 1500))
        if chunks:
            if out:
                answer_html.append('<p class="note">Strata counted <b>%s</b> answer tokens for this round; the '
                                   'tokens on these blocks share that count out by size, and this round measured '
                                   '~%.1f characters per answer token. If the model thought before answering, some '
                                   'of those tokens may sit in the thinking block rather than in the count.</p>'
                                   % (f"{out:,}", aplit or 3.5))
            else:
                answer_html.append('<p class="note">no answer token count came back for this round, so these block '
                                   'tokens are guesses at ~3.5 characters per token.</p>')
        elif resp:
            answer_html.append(block("answer text", ((resp.get("completion_preview") or "")[:max_chars]),
                                     "%s chars, %s (full text was not stored for this round)"
                                     % (f"{resp.get('completion_chars', 0):,}",
                                        _tok(resp.get("completion_chars", 0) or 0,
                                             ((resp.get("completion_chars") or 0) / out) if out else 0.0)),
                                     True))
        room = (scale - shown) if window else 0
        ctx_head = '<span class="tok">' + (f"{prompt:,} tok" if prompt else "~%s est tok" % f"{est:,}") \
            + " &middot; " + f"{v['chars']:,}" + " chars</span>"
        ans_head = '<span class="tok">' + (f"{out:,} tok" if out else "not counted") \
            + " &middot; " + f"{ans_chars:,}" + " chars</span>"
        cards.append(
            ('<section class="card" id="r%s"><div class="cardhead"><span class="seq">#%s</span>'
            '<span class="meta">%s &middot; %s &middot; model %s &middot; %s %s%s</span></div>'
            '<div class="kv"><div><span>context this round</span><b>%s</b></div>'
            '<div><span>of the window</span><b>%s</b></div>'
            '<div><span>room left in window</span><b>%s</b></div>'
            '<div><span>cache reused</span><b style="color:var(--reuse)">%s</b></div>'
            '<div><span>read new</span><b style="color:var(--new)">%s</b></div>'
            '<div><span>answer tokens</span><b>%s</b></div>'
            '<div><span>growth vs previous</span><b>%s</b></div>'
            '<div><span>request size</span><b>%.1f KB</b></div>'
            '<div><span>tool calls back</span><b>%s</b></div></div>'
            '<div class="cols"><div><h3>the exact context that was sent ' + ctx_head + '</h3>%s</div>'
            '<div><h3>what the model answered ' + ans_head + '</h3>%s</div></div>'
            '<p class="note">finish: %s &middot; full request body: <code>%s</code>%s</p></section>')
            % (seq, seq, esc((req.get("ts") or "").replace("T", " ")[:19]), esc(v["api"]),
               esc(req.get("model") or "?"), esc(req.get("method") or ""), esc(req.get("path") or ""),
               (' &middot; ' + esc(v["finish"])) if v["finish"] else "",
               f"{shown:,}" + ("" if prompt else " (est)"), pct_txt,
               f"{room:,} tok" if window else "-",
               f"{cached:,}" if prompt else "-", f"{new:,}", f"{out:,}" if out else "-",
               grow or "-", v["kb"],
               str(len(calls)) + (": " + ", ".join(esc(c.get("name", "?")) for c in calls[:6]) if calls else ""),
               "\n".join(ctx_html), "\n".join(answer_html) or "<p class='note'>no answer recorded yet</p>",
               esc(v["finish"] or "not recorded"), esc(req.get("payload_file") or "-"),
               (" &middot; answer: <code>%s</code>" % esc(resp.get("response_file"))) if resp and resp.get("response_file") else ""),
        )

    foot = ('<tr class="foot"><td class="l">total</td><td class="l"></td><td class="l">%d of %d rounds</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num"></td><td class="l"></td><td class="l"></td></tr>'
            % (len(shown_order), len(order),
               f"{totals['prompt']:,}" if totals["prompt"] else "-",
               f"{totals['cached']:,}" if totals["cached"] else "-",
               f"{totals['new']:,}" if totals["new"] else "-",
               f"{totals['out']:,}" if totals["out"] else "-"))

    gen = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    pages = (len(shown_order) + page_size - 1) // page_size
    window_line = ("context window %s tokens (%s)" % (f"{window:,}", window_src)) if window else (
        "context window unknown - start the report with --max-context N, or it scales on the biggest round")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strata context report</title><style>%s</style></head><body>
<header><h1>What your agents sent to Strata</h1>
<div class="sub">%d rounds logged &middot; %s &middot; %s prompt tokens over these rounds &middot;
<b style="color:var(--reuse)">%s</b> reused from the prompt cache &middot;
<b style="color:var(--new)">%s</b> read new &middot; %s answer tokens</div>
<div class="sub legend">bars are drawn against %s: green is what the prompt cache already had, orange is what
had to be read new, grey is an estimate. The round with the largest context gets a <span class="badge ok">biggest</span>
badge. A bar that turns red would mean a context over the window. The total row sums the rounds; it is not one context.</div>
</header><main>
<div class="picker"><select id="pick"><option value="">pick a round to open below\u2026</option>%s</select>
<span id="hint"></span></div>
<h2>The %d rounds of this page</h2>
<div class="pager"><button id="pprev">\u2190 %d earlier rounds</button><span id="pinfo"></span>
<button id="pnext">%d later rounds \u2192</button>
<span class="dim" style="font-size:12.5px">%d rounds per page (%d pages) \u00b7 \u2190 \u2192 move a whole page,
shift + \u2190 \u2192 one round</span></div>
<table><thead><tr><th class="l">#</th><th class="l">time</th><th class="l">api</th>
<th>context this round</th><th>cache reused</th><th>read new</th><th>answer</th>
<th>%% of window</th><th class="l">this round inside the window</th>
<th class="l">result</th></tr></thead>
<tbody>%s</tbody>%s</table>
<h2 id="ftitle" style="display:none">Selected round</h2>
<div id="focus"></div>
<h2>All rounds, oldest to newest</h2>
%s
<p class="note">Generated %s from <code>%s</code>. Long blocks are cut at %s characters; the full text
stays in the payload files listed under each round.</p>
</main><script>%s</script></body></html>""" % (
        CSS, len(order), esc(window_line), f"{totals['prompt']:,}" if totals["prompt"] else "-",
        f"{totals['cached']:,}" if totals["cached"] else "-", f"{totals['new']:,}" if totals["new"] else "-",
        f"{totals['out']:,}" if totals["out"] else "-", esc(scale_what), "\n".join(options),
        page_size, page_size, page_size, page_size, pages,
        "\n".join(rows), foot, "\n".join(cards), gen, esc(LOG), f"{max_chars:,}",
        JS.replace("PAGE_SIZE", str(page_size)))


def pad4(n):
    return str(n).rjust(4, "0")


def render_md(reqs, resps, order, max_chars, window):
    lines = ["# What your agents sent to Strata", ""]
    totals = {"prompt": 0, "cached": 0, "new": 0, "out": 0}
    for seq in order:
        v = _round_view(reqs[seq], resps.get(seq))
        if v["prompt"]:
            totals["prompt"] += v["prompt"]
            totals["cached"] += v["cached"]
            totals["new"] += v["prompt"] - v["cached"]
            totals["out"] += v["out"] or 0
    head = "%d rounds, %s prompt tokens (%s reused from cache, %s read new), %s answer tokens." % (
        len(order), f"{totals['prompt']:,}", f"{totals['cached']:,}", f"{totals['new']:,}", f"{totals['out']:,}")
    if window:
        head += " Context window %s tokens." % f"{window:,}"
    lines.append(head)
    for seq in order:
        req, resp, v = reqs[seq], resps.get(seq), _round_view(reqs[seq], resps.get(seq))
        shown = v["prompt"] or v["est"]
        lines += ["", "## round %s - %s, %s" % (seq, req.get("ts"), v["api"]),
                  "- context this round: %s%s, cache reused: %s, read new: %s, answer: %s, finish: %s"
                  % (f"{shown:,}", " (estimated)" if not v["prompt"] else "",
                     f"{v['cached']:,}" if v["prompt"] else "-",
                     f"{(v['prompt'] or 0) - v['cached']:,}", f"{v['out'] or 0:,}", v["finish"] or "-")]
        if window and shown:
            lines[-1] += ", %.1f%% of the window" % (100.0 * shown / window)
        for title, text in v["pieces"]:
            lines += ["", "### %s" % title, "```", text[:max_chars], "```"]
        answer = _read(os.path.join(LOGDIR, (resp or {}).get("response_file") or ""))
        if answer:
            lines += ["", "### the model answered", "```", answer[:max_chars], "```"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="render the context report page")
    ap.add_argument("--out", default=os.path.join(HERE, "report.html"))
    ap.add_argument("--md", help="also write a Markdown copy to this path")
    ap.add_argument("--max-chars", type=int, default=12000)
    ap.add_argument("--last", type=int, default=0, help="show only the newest N rounds in the table (0 = all)")
    ap.add_argument("--page-size", type=int, default=10, help="rounds per page in the table (min 1)")
    ap.add_argument("--max-context", type=int, default=0, help="the model's context window; 0 = find it")
    args = ap.parse_args()
    reqs, resps, order = load()
    if not order:
        raise SystemExit("the log has no requests yet")
    window, src = find_context_window(args.max_context)
    shown = order[-args.last:] if args.last and len(order) > args.last else order
    pages = (len(shown) + max(1, args.page_size) - 1) // max(1, args.page_size)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(reqs, resps, order, args.max_chars, args.last, window, src,
                            max(1, args.page_size)))
    print("wrote %s (%d rounds, %d page%s of %d, window %s tokens)" % (
        args.out, len(order), pages, "" if pages == 1 else "s", max(1, args.page_size),
        f"{window:,}" if window else "unknown"))
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(render_md(reqs, resps, order, args.max_chars, window))
        print("wrote %s" % args.md)


if __name__ == "__main__":
    main()
