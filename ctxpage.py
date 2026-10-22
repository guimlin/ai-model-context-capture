"""Make a single self-contained HTML page of what your agents sent to Strata.

  python ctxpage.py                     writes report.html next to this script
  python ctxpage.py --out name.html     a different file
  python ctxpage.py --md report.md      the same content as Markdown
  python ctxpage.py --max-chars 40000   more (or less) text inline per block
  python ctxpage.py --last 200          only the newest N rounds in the table
  python ctxpage.py --max-context 32768 force the window instead of reading it
  python ctxpage.py --watch             keep rewriting the page as rounds arrive

No fixed round cap: the page holds every round in the log, and --page-size only
splits the table for display.

The page holds, for every round: Strata's own token counts (how much context it
read, how much of that its prompt cache already had, how much had to be read new,
answer tokens), the size of that context against the model's context window, the
exact context that was sent (system prompt, tool definitions, every message) and
everything the model answered - text, thinking, and tool calls with their
arguments. It reads as four numbered parts (issue #24): **1 Threads**, **2 Rounds**
(the table), **3 Round details** (the round you picked, kept out of the way until a
round is picked) and **4 All rounds**. A picker above the table shows one chosen
round at a time. Beside the picker, a **show only** filter keeps one conversation's
rounds in the table, in the picker and across the pages. Every block of text in
every round starts shut: the button at the top of part 4 opens or shuts them all at
once, and each round keeps its own button beside its copy icon. Nothing is fetched
from the network; the file opens offline.
"""

import argparse
import glob
import html
import json
import os
import time
from datetime import datetime

import ctxlink
import ctxstore

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.environ.get("CTX_LOG_DIR") or os.path.join(HERE, "logs")
CAP = None

# The copy icon the monitor page (ctxweb.py) uses: the same two overlapping rounded
# rectangles, so every copy button across the site carries the same icon (issue #21).
COPY_ICON = ('<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"'
             ' stroke-width="1.4" stroke-linecap="round" aria-hidden="true">'
             '<rect x="5.4" y="5.4" width="8.2" height="8.2" rx="1.8"/>'
             '<path d="M10.6 3.2H4.9a1.7 1.7 0 0 0-1.7 1.7v5.7"/></svg>')

# The expand-all / collapse-all icon of the round cards (issue #22): the same 16x16
# stroke style as the copy icon. Both sit in the button and CSS shows one of them:
# the corners reaching out while any block is shut, the corners pulled in once every
# block of the round is open.
EXPAND_ICON = ('<svg class="max" viewBox="0 0 16 16" width="13" height="13" fill="none"'
               ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
               ' stroke-linejoin="round" aria-hidden="true">'
               '<path d="M5.3 2H3.3A1.3 1.3 0 0 0 2 3.3v2"/>'
               '<path d="M14 5.3V3.3A1.3 1.3 0 0 0 12.7 2h-2"/>'
               '<path d="M2 10.7v2A1.3 1.3 0 0 0 3.3 14h2"/>'
               '<path d="M10.7 14h2a1.3 1.3 0 0 0 1.3-1.3v-2"/></svg>')
COLLAPSE_ICON = ('<svg class="min" viewBox="0 0 16 16" width="13" height="13" fill="none"'
                 ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
                 ' stroke-linejoin="round" aria-hidden="true">'
                 '<path d="M5.3 2v2A1.3 1.3 0 0 1 4 5.3H2"/>'
                 '<path d="M14 5.3h-2A1.3 1.3 0 0 1 10.7 4V2"/>'
                 '<path d="M2 10.7h2A1.3 1.3 0 0 1 5.3 12v2"/>'
                 '<path d="M10.7 14v-2A1.3 1.3 0 0 1 12 10.7h2"/></svg>')


def _read(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _cap(path=None):
    """Open the log once: a folder the logger writes into, or a saved capture file.
    Ask for a different source and it is opened instead, so one process - the web
    page, for instance - can move between the live log and saved capture files."""
    global CAP
    target = os.path.abspath(path or DEFAULT_LOG)
    if CAP is None or getattr(CAP, "path", None) != target:
        CAP = ctxstore.Capture(target)
    return CAP


def find_context_window(forced=0):
    """The model's context window, as Strata was told to run it. Returns (tokens, source)."""
    if forced:
        return forced, "given on the command line"
    if CAP is not None:
        size, where = CAP.window()
        if size:
            return size, "saved in this capture" + (", " + where if where else "")
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
    reqs, resps, order = {}, {}, []
    for line in CAP.events().splitlines():
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
    raw = CAP.payload(entry.get("payload_file"))
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
/* the four parts of the Context page (issue #24): threads, rounds, round details,
   all rounds - each one a labelled block, numbered in the order the page reads down */
.part { margin:34px 0 0 }
.part > h2 { display:flex; gap:9px; align-items:baseline; margin:0 0 10px; padding-bottom:7px;
             border-bottom:1px solid var(--line) }
.part > h2 .pn { flex:none; font-family:var(--mono); font-size:12px; color:var(--accent);
                 border:1px solid #33455c; border-radius:999px; padding:1px 8px }
.part > h2 .what { color:var(--dim); font-size:12.5px; font-weight:400 }
.parttools { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:0 0 14px }
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
.cardtools { margin-left:auto; display:inline-flex; gap:6px; align-items:center }
.copybtn, .togglebtn { background:#1b2029; color:var(--dim); border:1px solid var(--line);
           border-radius:8px; padding:5px 7px; cursor:pointer;
           display:inline-flex; align-items:center; justify-content:center }
.copybtn svg, .togglebtn svg { display:block }
.copybtn:hover, .togglebtn:hover { color:var(--ink); border-color:var(--accent) }
.copybtn.done { color:var(--reuse); border-color:#274b3a }
.copybtn.bad { color:var(--bad); border-color:#4b2626 }
/* the expand button shows the corners reaching out while any block is shut... */
.togglebtn svg.min { display:none }
/* ...and the corners pulled in once every block of the round is open */
.togglebtn.all svg.max { display:none }
.togglebtn.all svg.min { display:block }
/* the part-wide button of the all-rounds part carries the word as well as the icons */
.togglebtn.wide { gap:7px; padding:6px 11px }
.togglebtn .word { font-size:12.5px }
.roundfields { display:none }
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
.thread { white-space:nowrap }
.tname { font-weight:600; display:inline-block; max-width:210px; overflow:hidden;
         text-overflow:ellipsis; vertical-align:bottom }
.trel { color:var(--dim); font-size:11.5px; margin-left:5px; font-family:var(--mono) }
.line { margin:10px 0 0; font-size:13px; color:var(--dim); line-height:1.5 }
.line b { color:var(--ink) }
.line .warn { color:#ff8080; font-weight:600 }
.line code { font-size:11.5px }
table.threads { margin-bottom:18px }
table.threads td.l { vertical-align:top }
.rounds a { margin-right:5px; white-space:nowrap }
.dim { color:var(--dim) }
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
.picker .tfilter { display:flex; gap:7px; align-items:center }
.picker .tfilter label { color:var(--dim); font-size:11.5px; text-transform:uppercase; letter-spacing:.05em }
.picker select#tfilter { min-width:210px }
#focus:empty { display:none } #focus .card { border-color:#33455c; margin-top:12px }
#focus .card.focused { box-shadow:0 0 0 1px var(--accent) }
#hint { color:var(--dim); font-size:12.5px }
.uprompt { cursor:help }
.tname.hov { cursor:help; border-bottom:1px dotted #46536a }
#hoverbox { position:fixed; z-index:60; display:none; max-width:min(700px,86vw); max-height:56vh;
            overflow:auto; background:#10151d; border:1px solid #3a4657; border-radius:10px;
            padding:11px 13px; font-family:var(--mono); font-size:12.5px; line-height:1.5;
            color:#dbe2ee; white-space:pre-wrap; word-break:break-word; pointer-events:none;
            box-shadow:0 12px 34px rgba(0,0,0,.6) }
#hoverbox b { display:block; color:var(--dim); font-size:11px; text-transform:uppercase;
              letter-spacing:.05em; margin-bottom:6px }
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
    detail = document.getElementById('detail'),
    tfilter = document.getElementById('tfilter'),
    rows = [], order = [], allRows = [];
var size = PAGE_SIZE, page = 0;
Array.prototype.forEach.call(document.querySelectorAll('tbody tr[data-seq]'), function(r){
  allRows.push(r); });
function pages(){ return Math.max(1, Math.ceil(rows.length / size)); }
function drawPage(){
  page = Math.max(0, Math.min(page, pages() - 1));
  rows.forEach(function(r, i){ r.style.display = (i >= page*size && i < (page+1)*size) ? '' : 'none'; });
  if(!rows.length){
    document.getElementById('pinfo').textContent = 'no rounds match this filter';
    document.getElementById('pprev').disabled = true;
    document.getElementById('pnext').disabled = true;
    return;
  }
  var first = order[page*size], last = order[Math.min(rows.length - 1, (page+1)*size - 1)];
  document.getElementById('pinfo').textContent = 'page ' + (page + 1) + ' of ' + pages()
    + '  \\u00b7  rounds ' + first + ' to ' + last + '  (of ' + rows.length + ')';
  document.getElementById('pprev').disabled = page <= 0;
  document.getElementById('pnext').disabled = page >= pages() - 1;
}
// the thread filter keeps one conversation's rounds in the table, the picker and the
// paging; "all threads" puts every round back
function filterThread(){
  var want = tfilter ? tfilter.value : '';
  rows = []; order = [];
  allRows.forEach(function(r){
    if(!want || r.getAttribute('data-thread') === want){ rows.push(r); order.push(r.getAttribute('data-seq')); }
    else { r.style.display = 'none'; r.classList.remove('picked'); }
  });
  Array.prototype.forEach.call(pick.options, function(o){
    o.hidden = !!(want && o.value && o.getAttribute('data-thread') !== want);
  });
  var keep = pick.value;
  if(keep && order.indexOf(keep) < 0){ pick.value = ''; keep = ''; show(''); }
  page = 0;
  drawPage();
  // a round that survives the filter can land off the new first page, and its hint
  // still names the neighbours it had before the filter, so show it again
  if(keep){ show(keep); }
}
if(tfilter){ tfilter.addEventListener('change', filterThread); }
// prev / next move a whole page of rounds, never a single round
document.getElementById('pprev').addEventListener('click', function(){ page--; drawPage(); });
document.getElementById('pnext').addEventListener('click', function(){ page++; drawPage(); });
function show(v){
  rows.forEach(function(r){ r.classList.toggle('picked', r.getAttribute('data-seq') === v); });
  pick.value = v || '';
  focus.innerHTML = '';
  detail.style.display = v ? '' : 'none';
  if(!v){   hint.textContent = 'pick a round, or click a row: its details open in part 3, Round '
                          + 'details, below the table, with a copy button that writes every field of it as text';
    return; }
  var i = order.indexOf(v);
  if(i >= 0 && Math.floor(i / size) !== page){ page = Math.floor(i / size); drawPage(); }
  var card = document.getElementById('r' + v);
  if(!card) { hint.textContent = 'round ' + v + ' is not in this file (its text was trimmed out)'; return; }
  var copy = card.cloneNode(true);
  copy.removeAttribute('id');            // keep ids unique while the full list stays below
  copy.classList.add('focused');
  focus.appendChild(copy);
  hint.textContent = 'round ' + v + ' of ' + order.length
    + (tfilter && tfilter.value ? ' (thread ' + tfilter.value + ' only)' : '')
    + '  \\u00b7  \\u2190 \\u2192 move ' + size + ' rounds at a time'
    + '  \\u00b7  shift + \\u2190 \\u2192 one round'
    + (i > 0 ? '  \\u00b7  round before: ' + order[i-1] : '')
    + (i >= 0 && i < order.length-1 ? '  \\u00b7  round after: ' + order[i+1] : '');
  ftitle.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
pick.addEventListener('change', function(){ show(pick.value); });
document.getElementById('rounds').addEventListener('click', function(e){
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
  // shift first: e.key is still "ArrowLeft" / "ArrowRight" with shift held, so the
  // plain-arrow branches below would otherwise swallow it
  if(e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')){ step(e.key === 'ArrowLeft' ? -1 : 1); e.preventDefault(); return; }
  if(e.key === 'ArrowLeft'  || e.key === 'PageUp')  { page--; drawPage(); e.preventDefault(); return; }
  if(e.key === 'ArrowRight' || e.key === 'PageDown'){ page++; drawPage(); e.preventDefault(); return; }
});
// the copy button in every round card: every field the page holds about the round,
// then the context and the answer, as plain text ready to paste into a note or a
// file. The window below the table is a clone of a card, so the button is in it too;
// the listener is on the document, so it catches both.
function plain(node){ return node ? node.textContent.trim() : ''; }
function roundText(card){
  var out = [], fields = card.querySelector('pre.roundfields');
  if (fields) { out.push(plain(fields)); }
  Array.prototype.forEach.call(card.querySelectorAll('.cols > div'), function (col) {
    out.push('', plain(col.querySelector('h3')), '----------------------------------------');
    Array.prototype.forEach.call(col.querySelectorAll('details'), function (d) {
      out.push('', '## ' + plain(d.querySelector('.role')) + ' (' + plain(d.querySelector('.size')) + ')',
               plain(d.querySelector('pre')));
    });
  });
  return out.join('\\n');
}
function copyText(text, done){
  // navigator.clipboard needs a secure context; a file:// page or an unfocused frame
  // can refuse it, and then the old select-and-copy path still works
  function fallback(){
    var box = document.createElement('textarea');
    box.value = text; box.style.position = 'fixed'; box.style.top = '-2000px';
    document.body.appendChild(box); box.focus(); box.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(box);
    return ok;
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); },
                                             function () { done(fallback()); });
  } else { done(fallback()); }
}
document.addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('button.copybtn') : null;
  if (!btn) { return; }
  e.preventDefault();
  var card = btn.closest('.card');
  if (!card) { return; }
  copyText(roundText(card), function (ok) {
    // the icon stays put; the button just tints itself to say what happened
    btn.classList.add(ok ? 'done' : 'bad');
    setTimeout(function () {
      btn.classList.remove('done'); btn.classList.remove('bad');
    }, 1800);
  });
});
// the expand/collapse button left of the copy icon in every round card (issue #22):
// one click opens every block of that round, the next shuts them all. It reads what the
// blocks are actually doing, so opening or shutting one by hand keeps the button honest,
// and the listener sits on the document so it catches the card and its clone in the
// window below the table alike.
function blocksOf(card){ return card.querySelectorAll('details'); }
function everyBlockOpen(card){
  var list = blocksOf(card);
  if (!list.length) { return false; }
  for (var i = 0; i < list.length; i++) { if (!list[i].open) { return false; } }
  return true;
}
function paintToggle(btn, open){
  var label = open ? 'collapse every block in this round'
                   : 'expand every block in this round';
  btn.classList.toggle('all', open);   // CSS swaps the icon with it
  btn.title = label;
  btn.setAttribute('aria-label', label);
}
document.addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('button.togglebtn') : null;
  if (!btn || btn.classList.contains('expandall')) { return; }
  e.preventDefault();
  var card = btn.closest('.card');
  if (!card || !blocksOf(card).length) { return; }
  var open = !everyBlockOpen(card);
  Array.prototype.forEach.call(blocksOf(card), function (d) { d.open = open; });
  paintToggle(btn, open);
  // opening one round by hand moves the part-wide button too, so it never lies
  var part = btn.closest('.part');
  if (part) { paintPartButtons(part); }
});
// the expand-all / collapse-all button at the top of the all-rounds part (issue #24):
// one click opens every block of every round in that part, the next shuts them all. It
// reads the blocks rather than remembering a state, exactly like the button of a single
// round, and it only touches its own part - the selected round above keeps its own pair
// of buttons, and the rounds keep theirs however far each one is open.
function blocksIn(part){
  var list = [];
  Array.prototype.forEach.call(part.querySelectorAll('.card'), function (c) {
    Array.prototype.forEach.call(blocksOf(c), function (d) { list.push(d); });
  });
  return list;
}
function everyOpen(list){
  if (!list.length) { return false; }
  for (var i = 0; i < list.length; i++) { if (!list[i].open) { return false; } }
  return true;
}
function paintAllBtn(btn, open){
  var label = open ? 'collapse every block of every round in this part'
                   : 'expand every block of every round in this part';
  btn.classList.toggle('all', open);   // CSS swaps the icon with it
  btn.title = label;
  btn.setAttribute('aria-label', label);
  var word = btn.querySelector('.word');
  if (word) { word.textContent = open ? 'collapse all' : 'expand all'; }
}
function paintPartButtons(part){
  Array.prototype.forEach.call(part.querySelectorAll('button.expandall'), function (b) {
    paintAllBtn(b, everyOpen(blocksIn(part)));
  });
}
document.addEventListener('click', function (e) {
  var btn = e.target.closest ? e.target.closest('button.expandall') : null;
  if (!btn) { return; }
  e.preventDefault();
  var part = btn.closest('.part');
  if (!part) { return; }
  var list = blocksIn(part);
  if (!list.length) { return; }
  var open = !everyOpen(list);
  Array.prototype.forEach.call(list, function (d) { d.open = open; });
  paintAllBtn(btn, open);
});
// one floating box for every [data-full]: the whole prompt, only while you hover it
var hover = document.createElement('div');
hover.id = 'hoverbox';
document.body.appendChild(hover);
function placeHover(ev){
  var pad = 16, w = hover.offsetWidth, h = hover.offsetHeight;
  var x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > window.innerWidth - 10) { x = Math.max(10, ev.clientX - w - pad); }
  if (y + h > window.innerHeight - 10) { y = Math.max(10, ev.clientY - h - pad); }
  hover.style.left = x + 'px'; hover.style.top = y + 'px';
}
document.addEventListener('mouseover', function(e){
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
document.addEventListener('mousemove', function(e){
  if (hover.style.display === 'block') { placeHover(e); }
});
document.addEventListener('mouseout', function(e){
  var el = e.target.closest ? e.target.closest('[data-full]') : null;
  if (el && (!e.relatedTarget || !el.contains(e.relatedTarget))) { hover.style.display = 'none'; }
});
window.addEventListener('scroll', function(){ hover.style.display = 'none'; }, true);
filterThread();          // fills the table, the picker and the paging from all the rows
show('');
"""


def esc(s):
    return html.escape(s or "")


def block(title, text, size_note, open_it=False):
    return ('<details%s><summary><span class="role">%s</span>'
            '<span class="size">%s</span></summary><pre>%s</pre></details>'
            % (" open" if open_it else "", esc(title), esc(size_note), esc(text) or "(empty)"))


def toggle_button(blocks_html):
    """The expand-all / collapse-all button in the head of a round card (issue #22): an
    icon rather than a word, named by what the click is about to do. The page writes it
    already painted to the blocks the card holds; the script repaints it on every click."""
    shut = "<details>" in blocks_html
    label = "expand every block in this round" if shut else "collapse every block in this round"
    return ('<button class="togglebtn%s" type="button" title="%s" aria-label="%s">%s%s</button>'
            % ("" if shut else " all", label, label, EXPAND_ICON, COLLAPSE_ICON))


def expand_all_button():
    """The collapse / expand-all button at the top of the all-rounds part (issue #24).
    It carries the same icon pair as the button of a single round, plus the word, because
    it speaks for every round in the part rather than for one card. The page writes it
    already painted for a part whose rounds all start shut; the script repaints it on
    every click, and after any round opens or shuts itself."""
    label = "expand every block of every round in this part"
    return ('<button class="togglebtn expandall wide" type="button" title="%s" aria-label="%s">'
            '%s%s<span class="word">expand all</span></button>'
            % (label, label, EXPAND_ICON, COLLAPSE_ICON))


def prompt_cell(head, full, chars, label, limit=90):
    """The prompt a person typed: a short preview in the cell, and the whole of it in
    a floating box while the mouse is over it. System text and tool definitions never
    reach here - ctxlink.user_prompts has already left them out."""
    if not head:
        return '<span class="dim">no prompt from a person in this round</span>'
    cut = (' <span class="dim">(showing the first %s of %s chars)</span>'
           % (f"{len(full):,}", f"{chars:,}")) if len(full) < chars else ""
    return ('<span class="uprompt" data-label="%s" data-full="%s">%s%s</span>%s'
            % (esc(label), esc(full), esc(head[:limit]),
               "\u2026" if len(head) > limit else "", cut))


def _lineage(lk, threads):
    """One line saying where this round came from: thread, parent, root prompt, tools."""
    if not lk or not lk.get("thread"):
        return '<span class="dim">payload missing, cannot tell where this round came from</span>'
    bits = ['<b>thread %s</b>' % esc(lk["thread"])]
    if lk.get("parent") is None:
        bits.append("this conversation starts here: no earlier round carries any of its "
                    "%s messages" % lk["n_msgs"])
    elif lk.get("chained_by_id"):
        bits.append("continues #%s, chained by its response id" % lk["parent"])
    elif lk.get("added") == 0:
        bits.append("the same history as #%s, sent out again" % lk["parent"])
    else:
        bits.append("continues #%s: %s messages, %s shared, <b>%s new</b>"
                    % (lk["parent"], lk["n_msgs"], lk["shared"], lk["added"]))
    if lk.get("root_changed"):
        bits.append('<span class="warn">the root prompt changed on this round</span>')
    elif lk.get("parent") is not None:
        bits.append("same root prompt as the round it continues")
    if lk.get("tools_changed"):
        bits.append("the tool definitions changed on this round")
    others = [t["name"] for t in threads
              if t["root_fp"] and t["root_fp"] == lk["root_fp"] and t["name"] != lk["thread"]]
    if others:
        bits.append("the same root prompt as thread " + ", ".join(esc(o) for o in others))
    if lk.get("same_history"):
        bits.append('<span class="warn">this exact history went out again in %s</span>'
                    % ", ".join("#%s" % s for s in lk["same_history"]))
    if lk.get("forked") and len(lk["forked"]) > 1:
        bits.append("more than one round continues from here: "
                    + ", ".join("#%s" % s for s in lk["forked"]))
    bits.append("root prompt %s chars, fingerprint <code>%s</code>"
                % (f"{lk['root_chars']:,}", esc(lk["root_fp"] or "none")))
    return " &middot; ".join(bits)


def round_fields(seq, req, resp, v, lk, window, window_src, scale, prev_prompt, ans_chars,
                 biggest=False):
    """Every figure the page holds about one round, as (label, value) pairs in the order
    the copy button writes them out. The card shows most of them; the copy carries them
    all, including what only the rounds table has - the thread, what the person asked this
    round, the HTTP result, who sent the round, the window it was measured against."""
    prompt, cached, out, est = v["prompt"], v["cached"], v["out"], v["est"]
    shown = prompt or est
    new = (prompt - cached) if prompt else est
    calls = (resp or {}).get("tool_calls") or []
    rows = []

    def add(label, value):
        if value not in (None, "", "-"):
            rows.append((label, str(value)))

    add("round", "#%s" % seq)
    add("thread", lk.get("thread"))
    if lk.get("parent") is None:
        add("in the conversation", "thread %s starts here, %s messages"
            % (lk.get("thread", "?"), lk.get("n_msgs")))
    elif lk.get("chained_by_id"):
        add("in the conversation", "continues #%s, chained by its response id, %s messages"
            % (lk["parent"], lk.get("n_msgs")))
    elif lk.get("added") == 0:
        add("in the conversation", "the same history as #%s, sent out again" % lk["parent"])
    else:
        add("in the conversation", "continues #%s: %s messages, %s shared, %s new"
            % (lk["parent"], lk.get("n_msgs"), lk.get("shared"), lk.get("added")))
    asked = lk.get("user_head") or ""
    if asked:
        add("asked this round", asked + ("" if (lk.get("user_chars") or 0) <= len(asked)
                                         else " ... (of %s chars in all)" % f"{lk['user_chars']:,}"))
    add("who sent it", lk.get("client"))
    add("time", (req.get("ts") or "").replace("T", " ")[:19])
    add("api", v["api"])
    add("model", req.get("model"))
    add("request", ("%s %s" % (req.get("method") or "", req.get("path") or "")).strip())
    add("http result", (resp or {}).get("status") or "no answer recorded")
    add("finish", v["finish"] or "not recorded")
    add("context this round", "%s tokens%s" % (
        f"{shown:,}", "" if prompt else " (estimated: Strata sent no count back)"))
    add("context window", ("%s tokens (%s)" % (f"{window:,}", window_src or "not stated"))
        if window else "not known: the page scaled this round on the biggest round")
    pct = (100.0 * shown / scale) if scale else 0.0
    add("percent of window", "%.1f%%" % pct)
    if pct > 100.0:
        add("over the context window", "yes, by %s tokens" % f"{shown - scale:,}")
    add("room left in window", f"{window - shown:,} tokens" if window else "")
    add("cache reused", f"{cached:,} tokens" if prompt else "no token count came back for this round")
    add("read new", f"{new:,} tokens")
    add("answer tokens", f"{out:,}" if out else "not counted")
    add("answer characters", f"{ans_chars:,} chars")
    add("growth vs previous round", "%+d" % (prompt - prev_prompt)
        if prompt and prev_prompt is not None else "")
    add("context characters", f"{v['chars']:,} chars")
    if v["cplit"]:
        add("characters per token", "~%.1f, measured against Strata's own count" % v["cplit"])
    add("messages sent", lk.get("n_msgs"))
    add("tool definitions", "%d, fingerprint %s" % (lk["n_tools"], (lk.get("tools_fp") or "")[:8])
        if lk.get("n_tools") else "none")
    add("root prompt", "%s chars, fingerprint %s"
        % (f"{lk.get('root_chars', 0):,}", (lk.get("root_fp") or "none")[:8]))
    add("changed on this round", " and ".join(
        what for what, on in (("the root prompt", lk.get("root_changed")),
                              ("the tool definitions", lk.get("tools_changed"))) if on))
    add("same history also went out in", ", ".join("#%s" % s for s in lk.get("same_history") or []))
    forked = lk.get("forked") or []
    add("more than one round continues from here",
        ", ".join("#%s" % s for s in forked) if len(forked) > 1 else "")
    add("request size", "%.1f KB" % v["kb"])
    add("tool calls back", "%d: %s" % (len(calls), ", ".join(c.get("name", "?") for c in calls[:8]))
        if calls else "none")
    add("biggest context of these rounds", "yes" if biggest else "")
    add("request payload file", req.get("payload_file"))
    add("answer payload file", (resp or {}).get("response_file"))
    return rows


def render_html(reqs, resps, order, max_chars, last, window, window_src, page_size=10):
    views = {s: _round_view(reqs[s], resps.get(s)) for s in order}
    links = ctxlink.attach([reqs[s] for s in order], CAP, resps)
    threads = links.get("_threads", [])
    exact = [v["prompt"] for v in views.values() if v["prompt"]]
    # an empty view is legitimate: everything in this source is hidden from the page
    biggest_seq = max(order, key=lambda s: views[s]["prompt"] or views[s]["est"]) if order else None
    biggest = (views[biggest_seq]["prompt"] or views[biggest_seq]["est"]) if biggest_seq else 0

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
        prev = prev_prompt
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
        lk = links.get(seq) or {}
        if lk.get("parent") is None:
            rel = "new"
        elif lk.get("added") == 0:
            rel = "again"
        elif lk.get("chained_by_id"):
            rel = "id"
        else:
            rel = "+%s" % lk.get("added")
        asked = lk.get("user_prompt") or ""
        thread_cell = ('<span class="tname%s"%s>%s</span>'
                       '<span class="trel" title="messages added since the round it continues">%s</span>'
                       % (" hov" if asked else "",
                          (' data-label="round %s, thread %s: what the person asked this round" '
                           'data-full="%s"' % (seq, esc(lk.get("thread") or "?"), esc(asked))
                           if asked else ""),
                          esc(lk.get("thread") or "?"), esc(rel)))
        rows.append(
            '<tr data-seq="%s" data-thread="%s"><td class="l num">%s%s</td><td class="l thread">%s</td>'
            '<td class="l">%s</td><td class="l">%s</td>'
            '<td class="num strong">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num%s">%s</td>'
            '<td class="l">%s</td><td class="l"><span class="badge %s">%s</span></td></tr>'
            % (seq, esc(lk.get("thread") or "?"), seq, flag, thread_cell,
               esc((req.get("ts") or "")[11:19]), esc(v["api"]),
               f"{shown:,}" + ("" if prompt else " est"),
               f"{cached:,}" if prompt else "-", f"{new:,}", f"{out:,}" if out else "-",
               " over" if over else "", pct_txt,
               bar, badge, status or "no answer"))
        options.append('<option value="%s" data-thread="%s">%s #%s  %s  %s tok  %s  %s</option>' % (
            seq, esc(lk.get("thread") or "?"), esc((lk.get("thread") or "?")), pad4(seq),
            esc((req.get("ts") or "")[11:19]),
            f"{shown:,}".rjust(7), esc(v["api"]), esc(str(status or "no answer"))))

        ctx_html = []
        for title, text in v["pieces"]:
            # every block starts shut (issue #24): the all-rounds part is a list of
            # rounds to scan, not a wall of text, so opening is always the reader's call
            ctx_html.append(block(title, text[:max_chars],
                                  "%s chars, %s" % (f"{len(text):,}", _tok(len(text), v["cplit"]))))
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
        rfile = CAP.payload((resp or {}).get("response_file"))
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
                                     "%s chars, %s" % (f"{len(rest):,}", _tok(len(rest), aplit))))
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
                                             ((resp.get("completion_chars") or 0) / out) if out else 0.0))))
        room = (scale - shown) if window else 0
        ctx_head = '<span class="tok">' + (f"{prompt:,} tok" if prompt else "~%s est tok" % f"{est:,}") \
            + " &middot; " + f"{v['chars']:,}" + " chars</span>"
        ans_head = '<span class="tok">' + (f"{out:,} tok" if out else "not counted") \
            + " &middot; " + f"{ans_chars:,}" + " chars</span>"
        fields = round_fields(seq, req, resp, v, lk, window, window_src, scale, prev,
                              ans_chars, seq == biggest_seq)
        field_text = esc("\n".join("%s: %s" % pair for pair in fields))
        cards.append(
            ('<section class="card" id="r%s"><div class="cardhead"><span class="seq">#%s</span>'
            '<span class="meta">%s &middot; %s &middot; model %s &middot; %s %s%s</span>'
            '<div class="cardtools">'
            + toggle_button("".join(ctx_html) + "".join(answer_html)) +
            '<button class="copybtn" type="button" title="Copy every field of this round as plain text">'
            + COPY_ICON + '</button></div></div>'
            '<p class="line">%s</p>'
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
            '<p class="note">finish: %s &middot; full request body: <code>%s</code>%s</p>'
            '<pre class="roundfields" hidden>%s</pre></section>')
            % (seq, seq, esc((req.get("ts") or "").replace("T", " ")[:19]), esc(v["api"]),
               esc(req.get("model") or "?"), esc(req.get("method") or ""), esc(req.get("path") or ""),
               (' &middot; ' + esc(v["finish"])) if v["finish"] else "",
               _lineage(lk, threads),
               f"{shown:,}" + ("" if prompt else " (est)"), pct_txt,
               f"{room:,} tok" if window else "-",
               f"{cached:,}" if prompt else "-", f"{new:,}", f"{out:,}" if out else "-",
               grow or "-", v["kb"],
               str(len(calls)) + (": " + ", ".join(esc(c.get("name", "?")) for c in calls[:6]) if calls else ""),
               "\n".join(ctx_html), "\n".join(answer_html) or "<p class='note'>no answer recorded yet</p>",
               esc(v["finish"] or "not recorded"), esc(req.get("payload_file") or "-"),
               (" &middot; answer: <code>%s</code>" % esc(resp.get("response_file"))) if resp and resp.get("response_file") else "",
               field_text),
        )

    foot = ('<tr class="foot"><td class="l">total</td><td class="l">%d thread%s</td><td class="l"></td>'
            '<td class="l">%d of %d rounds</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num"></td><td class="l"></td><td class="l"></td></tr>'
            % (len(threads), "" if len(threads) == 1 else "s", len(shown_order), len(order),
               f"{totals['prompt']:,}" if totals["prompt"] else "-",
               f"{totals['cached']:,}" if totals["cached"] else "-",
               f"{totals['new']:,}" if totals["new"] else "-",
               f"{totals['out']:,}" if totals["out"] else "-"))

    thread_rows = []
    for th in threads:
        rlist = th["rounds"]
        pin = cach = pout = 0
        exact = False
        peak, peak_seq = 0, rlist[0]
        for s in rlist:
            vv = views[s]
            size = vv["prompt"] or vv["est"] or 0
            if vv["prompt"]:
                exact = True
                pin += vv["prompt"]
                cach += vv["cached"]
                pout += vv["out"] or 0
            if size >= peak:
                peak, peak_seq = size, s
        notes = []
        if th["rounds_with_root_change"]:
            notes.append("root prompt changed at "
                         + ", ".join("#%d" % s for s in th["rounds_with_root_change"]))
        if th["rounds_with_tool_change"]:
            notes.append("tool definitions changed at "
                         + ", ".join("#%d" % s for s in th["rounds_with_tool_change"]))
        if th.get("repeats"):
            notes.append("same history sent again: " + ", ".join("#%d" % s for s in th["repeats"]))
        for s in th["forks"]:
            notes.append("#%d was continued by more than one round (%s)"
                         % (s, ", ".join("#%d" % c for c in links[s]["forked"])))
        same = [t["name"] for t in threads if t["root_fp"] and t["root_fp"] == th["root_fp"]
                and t["name"] != th["name"]]
        if same:
            notes.append("same root prompt as thread " + ", ".join(same))
        first = rlist[0]
        thread_rows.append(
            '<tr id="thread%s"><td class="l strong">%s</td><td class="num">%s</td>'
            '<td class="l">%s &rarr; %s</td><td class="l">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="l">%s<div class="dim">root prompt <code>%s</code>, %s chars</div></td>'
            '<td class="l">%s</td></tr>' % (
                th["id"], esc(th["name"]),
                "%d <span class=\"dim\">(#%s-%s)</span>" % (len(rlist), rlist[0], rlist[-1]),
                pad4(first), pad4(rlist[-1]), esc(links[first]["client"]),
                f"{views[first]['prompt'] or views[first]['est'] or 0:,}",
                "%s (#%d)" % (f"{peak:,}", peak_seq),
                (f"{pin:,}" + (", %s reused" % f"{cach:,}" if exact else "")) if exact else "-",
                prompt_cell(th["open_head"], th["open_prompt"], th["open_chars"],
                            "thread %s: the first thing a person asked here" % th["name"]),
                esc(th["root_fp"][:8] or "none"), f"{th['root_chars']:,}",
                esc("; ".join(notes) or "steady: same root prompt and tools the whole way")))

    # the thread filter: one option per conversation that has rounds in this table,
    # counted over the rounds actually shown (a --last cut can drop a thread's rounds)
    in_table = {}
    for seq in shown_order:
        in_table.setdefault((links.get(seq) or {}).get("thread") or "?", []).append(seq)
    filter_opts = ['<option value="">all %d thread%s</option>'
                   % (len(in_table), "" if len(in_table) == 1 else "s")]
    for name, seqs in in_table.items():
        filter_opts.append('<option value="%s">thread %s \u00b7 %d round%s (#%s\u2013%s)</option>' % (
            esc(name), esc(name), len(seqs), "" if len(seqs) == 1 else "s", seqs[0], seqs[-1]))

    gen = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    pages = (len(shown_order) + page_size - 1) // page_size
    window_line = ("context window %s tokens (%s)" % (f"{window:,}", window_src)) if window else (
        "context window unknown - start the report with --max-context N, or it scales on the biggest round")
    if CAP is not None and CAP.kind == "file":
        window_line += "  -  opened from the capture saved %s" % (CAP.manifest.get("created") or "?")
        if CAP.note():
            window_line += " - note: " + CAP.note()
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
<section class="part" id="part-threads">
<h2><span class="pn">1</span>Threads<span class="what">which rounds make up one conversation</span></h2>
<div class="sub legend">rounds are grouped by what they sent: a round belongs to an earlier round's
conversation when its history starts with that round's entire history, and the newest such match wins,
so a retry of a turn hangs off the same round instead of starting a conversation of its own. The root
(system) prompt and the tool definitions are hashed too - that is how you see a root prompt change
half way through a session, or two conversations running on the same root prompt. The text in the
<b>user prompt</b> column is the first thing a person typed in that conversation, never the system
prompt or the tool definitions; hover over it and a floating box holds the whole of it. Each
conversation is called by a few words out of that first ask, which is also what the thread filter
offers.</div>
<table class="threads"><thead><tr><th class="l">thread</th><th class="num">rounds</th>
<th class="l">first &rarr; last</th><th class="l">who sent it</th>
<th class="num">context, first</th><th class="num">peak</th><th class="num">tokens in</th>
<th class="l">user prompt &middot; hover for all of it</th><th class="l">what changed</th></tr></thead>
<tbody>%s</tbody></table>
</section>
<div class="picker"><select id="pick"><option value="">pick a round to open below\u2026</option>%s</select>
<span class="tfilter"><label for="tfilter">show only</label><select id="tfilter">%s</select></span>
<span id="hint"></span></div>
<section class="part" id="part-rounds">
<h2><span class="pn">2</span>Rounds<span class="what">the %d rounds of this page</span></h2>
<div class="pager"><button id="pprev">\u2190 %d earlier rounds</button><span id="pinfo"></span>
<button id="pnext">%d later rounds \u2192</button>
<span class="dim" style="font-size:12.5px">%d rounds per page (%d pages) \u00b7 \u2190 \u2192 move a whole page,
shift + \u2190 \u2192 one round</span></div>
<table id="rounds"><thead><tr><th class="l">#</th><th class="l">thread</th><th class="l">time</th><th class="l">api</th>
<th>context this round</th><th>cache reused</th><th>read new</th><th>answer</th>
<th>%% of window</th><th class="l">this round inside the window</th>
<th class="l">result</th></tr></thead>
<tbody>%s</tbody>%s</table>
</section>
<section class="part" id="detail" style="display:none">
<h2 id="ftitle"><span class="pn">3</span>Round details<span class="what">the round you picked, in full</span></h2>
<div id="focus"></div>
</section>
<section class="part" id="part-all">
<h2><span class="pn">4</span>All rounds<span class="what">every round on this page, oldest to newest</span></h2>
<div class="parttools">%s<span class="dim">every round starts shut &middot; this button opens every block of
every round here, and the next click shuts them again &middot; each round keeps its own button in its head</span></div>
%s
</section>
<p class="note">Generated %s from <code>%s</code>. Long blocks are cut at %s characters; the full text
stays in the payload files listed under each round.</p>
</main><script>%s</script></body></html>""" % (
        CSS, len(order), esc(window_line), f"{totals['prompt']:,}" if totals["prompt"] else "-",
        f"{totals['cached']:,}" if totals["cached"] else "-", f"{totals['new']:,}" if totals["new"] else "-",
        f"{totals['out']:,}" if totals["out"] else "-", esc(scale_what),
        "\n".join(thread_rows), "\n".join(options), "\n".join(filter_opts),
        page_size, page_size, page_size, page_size, pages,
        "\n".join(rows), foot, expand_all_button(), "\n".join(cards), gen, esc(CAP.label), f"{max_chars:,}",
        JS.replace("PAGE_SIZE", str(page_size)))


def pad4(n):
    return str(n).rjust(4, "0")


def render_md(reqs, resps, order, max_chars, window):
    lines = ["# What your agents sent to Strata", ""]
    links = ctxlink.attach([reqs[s] for s in order], CAP, resps)
    threads = links.get("_threads", [])
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
    lines += ["", "## %d conversations in this log" % len(threads), "",
              "A round belongs to an earlier round's conversation when its history starts with that "
              "round's entire history, so the newest match wins and a retried turn hangs off the same "
              "round. The root (system) prompt and the tool definitions are hashed separately, which is "
              "how a mid-session root-prompt change or two conversations sharing one root prompt show up.",
              ""]
    lines += ["| thread | rounds | first - last | who sent it | root prompt | tools | what changed |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for th in threads:
        notes = []
        if th["rounds_with_root_change"]:
            notes.append("root prompt changed at " + ", ".join("#%d" % s
                                                              for s in th["rounds_with_root_change"]))
        if th["rounds_with_tool_change"]:
            notes.append("tools changed at " + ", ".join("#%d" % s
                                                        for s in th["rounds_with_tool_change"]))
        if th.get("repeats"):
            notes.append("same history sent again: " + ", ".join("#%d" % s for s in th["repeats"]))
        same = [t["name"] for t in threads if t["root_fp"] and t["root_fp"] == th["root_fp"]
                and t["name"] != th["name"]]
        if same:
            notes.append("same root prompt as thread " + ", ".join(same))
        lines.append("| %s | %d | #%s - #%s | %s | %s chars, `%s` | %d | %s |" % (
            th["name"], len(th["rounds"]), th["rounds"][0], th["rounds"][-1],
            esc(links[th["rounds"][0]]["client"]), f"{th['root_chars']:,}",
            th["root_fp"][:8], th["n_tools"],
            esc("; ".join(notes) or "same root prompt and tools the whole way")))
    for seq in order:
        req, resp, v = reqs[seq], resps.get(seq), _round_view(reqs[seq], resps.get(seq))
        shown = v["prompt"] or v["est"]
        lk = links.get(seq) or {}
        lines += ["", "## round %s (thread %s) - %s, %s"
                  % (seq, lk.get("thread", "?"), req.get("ts"), v["api"]),
                  "- context this round: %s%s, cache reused: %s, read new: %s, answer: %s, finish: %s"
                  % (f"{shown:,}", " (estimated)" if not v["prompt"] else "",
                     f"{v['cached']:,}" if v["prompt"] else "-",
                     f"{(v['prompt'] or 0) - v['cached']:,}", f"{v['out'] or 0:,}", v["finish"] or "-")]
        if window and shown:
            lines[-1] += ", %.1f%% of the window" % (100.0 * shown / window)
        if lk.get("parent") is None:
            lines.append("- conversation: thread %s starts here (%s messages, root prompt %s chars `%s`)"
                         % (lk.get("thread", "?"), lk.get("n_msgs"), f"{lk.get('root_chars', 0):,}",
                            (lk.get("root_fp") or "none")[:8]))
        else:
            lines.append("- conversation: thread %s continues #%s (%s messages, %s added, root prompt "
                         "%s chars `%s`%s%s)"
                         % (lk.get("thread", "?"), lk["parent"], lk.get("n_msgs"),
                            ("chained by response id" if lk.get("chained_by_id") else lk.get("added")),
                            f"{lk.get('root_chars', 0):,}", (lk.get("root_fp") or "none")[:8],
                            ", ROOT PROMPT CHANGED HERE" if lk.get("root_changed") else "",
                            ", tools changed here" if lk.get("tools_changed") else ""))
        if lk.get("same_history"):
            lines.append("- this exact history also went out in: %s"
                         % ", ".join("#%d" % s for s in lk["same_history"]))
        for title, text in v["pieces"]:
            lines += ["", "### %s" % title, "```", text[:max_chars], "```"]
        answer = CAP.payload((resp or {}).get("response_file"))
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
    ap.add_argument("--log", default=None, help="the log folder, or a saved capture file (.ctxcap)")
    ap.add_argument("--watch", action="store_true",
                    help="keep rewriting the page as new rounds arrive, instead of writing once")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between refreshes with --watch")
    args = ap.parse_args()
    _cap(args.log)

    def once():
        reqs, resps, order = load()
        if not order:
            return None
        window, src = find_context_window(args.max_context)
        shown = order[-args.last:] if args.last and len(order) > args.last else order
        pages = (len(shown) + max(1, args.page_size) - 1) // max(1, args.page_size)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(render_html(reqs, resps, order, args.max_chars, args.last, window, src,
                                max(1, args.page_size)))
        msg = "wrote %s (%d rounds, %d page%s of %d, window %s tokens)" % (
            args.out, len(order), pages, "" if pages == 1 else "s", max(1, args.page_size),
            f"{window:,}" if window else "unknown")
        if args.md:
            with open(args.md, "w", encoding="utf-8") as f:
                f.write(render_md(reqs, resps, order, args.max_chars, window))
            msg += "\nwrote %s" % args.md
        return msg

    if not args.watch:
        first = once()
        if first is None:
            raise SystemExit("the log has no requests yet")
        print(first)
        return

    if CAP.kind != "dir":
        raise SystemExit("--watch needs the live log folder; %s is a saved capture, "
                         "which does not grow." % CAP.path)
    print("watching %s, rewriting %s every %g seconds (Ctrl+C to stop)"
          % (CAP.path, args.out, args.interval))
    last = None
    while True:
        try:
            msg = once()
        except Exception as exc:            # the proxy may be mid-write on a log line
            msg = "read failed: %s" % exc
        if msg and msg != last:
            print(msg, flush=True)
            last = msg
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
