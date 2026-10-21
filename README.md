# Watching the context that goes to Strata

A local logging proxy plus report tools for seeing exactly what each coding agent
sends to a local Strata model on every round - the full prompt, the full answer,
and the token statistics (Strata's own counts: context read, prompt-cache reuse,
new tokens, answer tokens, share of the context window).

```powershell
.\Start-ContextMonitor.ps1                       # finds Strata's port, starts the logger
# point your agent at  http://127.0.0.1:8788/v1  (Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:8788)
python ctxview.py list                           # rounds, live numbers
python ctxpage.py --md report.md                 # report.html: one page per 10 rounds, pick any round
```

Needs only Python 3 (standard library) and PowerShell. Nothing leaves the
machine: the proxy talks to Strata on loopback only.

## What is running on this PC

The Strata **app** is running, and it listens on a **random loopback port that
changes every time it starts** - today `http://127.0.0.1:47831/v1` (its own
internal server answers on a second port, 18500, with an API key). So nothing
here hard-codes 8080.

Find the port at any moment:

    .\find-strata-port.ps1            # prints the port
    .\find-strata-port.ps1 -Detailed  # port, model, process, pid

(The `run-iq3_s.bat` route from the Terminal is the fixed-port alternative:
`C:\Users\v-gulin\AppData\Local\Strata\install\strata-iq3_s.json`, port 8080.)

## 1. Strata's built-in request inspector - off, and you probably want it on

Your build can keep the last 100 requests **with their full prompts and answers**
and show them on a page: the `api_monitor` option. Right now it is off, which is
why `http://127.0.0.1:47831/api/requests` answers 404.

Turn it on, then restart the model:

- `"api_monitor": true` in the run config (`strata-iq3_s.json` for the Terminal
  route), or `--api-monitor` where the server is started, or the **Settings,
  Model settings** card in the web page;
- then open `http://127.0.0.1:<port>/api-monitor` (or the same page in the app):
  one row per agent request, and each row's **Input** is the exact context the
  agent sent - system prompt, whole history, tool results, tool definitions -
  beside Output, Reasoning, the raw API response, prompt tokens and timings;
- for scripts: `GET /api/requests` (list) and `GET /api/requests?id=<id>` (the
  full record).

Limits: last 100 requests, **memory only** (gone after a model restart), each
request cut at 262,144 characters. Costs a little RAM, nothing in speed.

The ordinary Monitor and `strata monitor` / `strata logs` never show prompt
text, by design. `GET /slots` gives the running request's `n_prompt_tokens` for
a context meter (over `n_ctx`).

## 2. This folder: a logger that keeps every request on disk

`context_proxy.py` sits between your agents and Strata and forwards everything
unchanged - streaming, tool calls, thinking levels - while writing **every**
request to disk:

- `logs\requests.jsonl`: one line per request and per answer (model, API shape,
  estimated system / history / tool-definition tokens, the real `usage` from
  Strata, finish reason). Keys are masked.
- `logs\payloads\req-NNNNNN.json`: the full request body, so you can read the
  exact context back.

```powershell
# 1. start it (it finds Strata's current port by itself)
.\Start-ContextMonitor.ps1
.\Start-ContextMonitor.ps1 -Live            # also opens the live view
.\Start-ContextMonitor.ps1 -Upstream http://127.0.0.1:8080   # if you run Strata yourself

# 2. point the agent at the logger instead of at Strata
#    OpenAI-compatible apps (OpenCode, Cursor, anything with a custom base URL)
        http://127.0.0.1:8788/v1
#    Claude Code
        $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8788"
#    Codex CLI
        model_base_url = "http://127.0.0.1:8788/v1/responses"

# 3. watch it, in another window
python ctxview.py live
```

Reading the log later:

```powershell
python ctxview.py list            # one line per request, with Strata's exact counts
python ctxview.py show 42         # the whole context of request 42, message by message
python ctxview.py diff 42 43      # what grew between two turns of the same agent
python ctxview.py stats           # biggest contexts, totals, prompt-cache reuse
```

### The page: every round, in a browser

```powershell
python ctxpage.py --md report.md          # writes report.html (and a Markdown copy)
python ctxpage.py --page-size 25          # rounds per page (default 10)
python ctxpage.py --max-chars 40000       # more text inline per block
python ctxpage.py --max-context 32768     # override the window instead of reading it
python ctxpage.py --last 200              # only the newest 200 rounds in the table
```

`report.html` is one offline file, and it holds **every** round in the log - the
log is never trimmed, so with 500 rounds the file has all 500 (use `--last` for a
shorter table). It reads your context window from Strata's own run config
(`--max-context 204800` in `strata-iq3_s.json`), or take `--max-context`; the
window itself is stated once in the header instead of repeating in a column.

The table always shows **the 10 rounds of one page**, and `← 10 earlier rounds` /
`10 later rounds →` move a whole page at a time (arrow keys do the same; shift +
arrows step a single round). Per round: **context this round** (Strata's count;
`est` only when no count came back), **cache reused**, **read new**, **answer**
tokens, **% of window**, a bar of that round inside the window (green = already in
the prompt cache, orange = read new; it turns red if a context ever exceeds the
window), and the HTTP result. The round with the largest context gets a `biggest`
badge; the last row totals the rounds (a sum, not one context).

From a 50-round test run (window 204,800 tokens, so 5 pages):

```
#    time     api          context this round  reused   new  answer  % of window
1    17:32:42 openai-chat  562                0        562  31      0.3%
10   17:33:19 openai-chat  7,212              7,107    105  37      3.5%
50   17:35:43 openai-chat  35,215 (biggest)   34,511   704  39      17.2%
```

**Picking a round.** Choose a round in the list at the top, or click a row: the
10-round table stays exactly where it is, and the chosen round opens in a
**Selected round** window just below the table, with **all** rounds still listed
underneath that. Choosing the empty entry at the top of the list closes the
window again. Picking a round that lives on another page switches the table to
that page.

Then one card per round: the same numbers plus growth since the previous round,
room left in the window, request size in KB and the tool calls back; and side by
side **the exact context that was sent** - system prompt, tool definitions, every
message including tool results - and **what the model answered**: text, thinking,
and tool calls with their arguments. Long blocks fold open and closed, and each
card names the payload file holding the uncut text.

Each round's blocks show their size in characters and their share of Strata's
token count for that round, so they add up to the round's total instead of being
separate guesses, and the card states the characters-per-token that round measured
(it ranged from ~7.9 for repeated boilerplate to ~2.5 for code, which is why
character guesses alone are not to be trusted).

The answers are stored while they stream, so restarting the model does not lose
them: `logs\payloads\resp-000042.txt` holds round 42's text, thinking and tool
calls; `logs\payloads\req-000042.json` holds its full request.

The same numbers on the command line: `ctxview.py stats` ends with `context
window: 204,800 tokens (Strata's run config strata-iq3_s.json); the biggest round
filled 17.2% of it, 169,585 tokens of room left`, and `ctxview.py diff 49 50` says
which part grew - system prompt, history, tool results, tool definitions - and how
much of the request was an unchanged prefix. Claude Code, Codex and plain
OpenAI-style requests are all recognised.


## Stopping and space

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*context_proxy.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

Every request body is kept in `logs\payloads`; close the logger and delete
`logs` to start over. Options: `--port 8788 --upstream <url> --dir logs
--store-max-bytes 2000000`. If Strata is not reachable the logger answers 502 and
logs that, so you notice.
