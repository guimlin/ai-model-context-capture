"""Put a capture in one file, and open a capture someone handed you.

  python ctxsave.py save                       the whole log into one .ctxcap file
  python ctxsave.py save --threads A,B         only those conversations
  python ctxsave.py save --last 200            only the newest 200 rounds
  python ctxsave.py save --since 2026-10-20    that day onwards
  python ctxsave.py save --no-payloads         counts only, no prompt text
  python ctxsave.py info FILE.ctxcap           what is inside a capture
  python ctxsave.py open FILE.ctxcap           build the page from it and open it
  python ctxsave.py extract FILE.ctxcap --to D unpack it into a log folder

Everything else reads a capture directly, with --log:

  python ctxview.py sessions --log FILE.ctxcap
  python ctxpage.py --log FILE.ctxcap --out report.html
"""

import argparse
import json
import os
import subprocess
import sys
import time

import ctxlink
import ctxstore

HERE = os.path.dirname(os.path.abspath(__file__))


def _window_for(cap, forced=0):
    """The context window to write into the manifest, so a capture says how big
    the window was when it was taken, not when it is opened."""
    if forced:
        return forced, "given on the command line with --window"
    if cap.kind == "file":
        size, src = cap.window()
        return (size, src or "recorded in this capture") if size else (None, None)
    try:
        import ctxpage
        return ctxpage.find_context_window()
    except Exception:
        return None, None


def _selection(args):
    bits = []
    if args.rounds:
        bits.append("rounds " + args.rounds)
    if args.since:
        bits.append("from " + args.since)
    if args.until:
        bits.append("until " + args.until)
    if args.threads:
        bits.append("threads " + args.threads)
    if args.last:
        bits.append("newest %d" % args.last)
    return "; ".join(bits) or "everything in the log"


def cmd_save(args):
    source = ctxstore.Capture(args.log)
    reqs, resps, order = ctxstore._events(source)
    chosen = ctxstore.choose(reqs, order, last=args.last, rounds=args.rounds, since=args.since,
                             until=args.until, threads=args.threads, cap=source)
    if not chosen:
        sys.exit("nothing matches that selection (%s). The log holds seq %s..%s."
                 % (_selection(args), order[0], order[-1]))
    out = args.out or os.path.join("captures", "capture-" + time.strftime("%Y%m%d-%H%M%S")
                                   + ctxstore.SUFFIX)
    window, window_src = _window_for(source, args.window)
    manifest = ctxstore.save(source, out, chosen, window=window, window_src=window_src or "",
                             note=args.note, with_payloads=not args.no_payloads,
                             selection=_selection(args))
    size = os.path.getsize(out)
    payload_files = sorted(set(_names(source, chosen)))
    print("wrote %s" % os.path.abspath(out))
    print("  %d rounds (seq %s..%s), %d answers, %d payload files"
          % (manifest["rounds"], manifest["first_seq"], manifest["last_seq"],
             manifest["with"]["answers"], 0 if args.no_payloads else len(payload_files)))
    print("  %s of prompt text, %.1f MB in the file (%d%% of the source)"
          % (_human(manifest["with"]["payload_bytes"]), size / 1048576.0,
             100 * size / max(1, _source_bytes(source, payload_files, not args.no_payloads))))
    if window:
        print("  context window written in: %s tokens (%s)" % (f"{window:,}", window_src))
    if manifest.get("missing"):
        print("  %d payload files were already gone from the log: %s"
              % (len(manifest["missing"]), ", ".join(manifest["missing"][:5])))
    check = ctxstore.Capture(out)
    c_reqs, c_resps, c_order = ctxstore._events(check)
    links = ctxlink.attach([c_reqs[s] for s in c_order], check, c_resps)
    print("  opened it again: %d rounds in %d conversations (%s)"
          % (len(c_order), len(links["_threads"]),
             ", ".join("%s %d" % (t["name"], len(t["rounds"])) for t in links["_threads"])))
    if len(c_order) != len(chosen):
        print("  WARNING: the file holds %d rounds but %d were selected" % (len(c_order), len(chosen)))
    print("open it any time with:  python ctxsave.py open %s" % out)


def _names(source, chosen):
    reqs, resps, _ = ctxstore._events(source)
    found = []
    for seq in chosen:
        for key, record in (("payload_file", reqs.get(seq)), ("response_file", resps.get(seq))):
            if (record or {}).get(key):
                found.append(record[key])
    return found


def _source_bytes(source, names, payloads):
    """What the same material weighs where it came from, so the file size means something."""
    if not payloads:
        return len(source.events().encode("utf-8"))
    total = 0
    for name in names:
        text = source.payload(name)
        total += len(text.encode("utf-8")) if text else 0
    return total


def _human(n):
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d bytes" % n


def _threads(cap):
    reqs, resps, order = ctxstore._events(cap)
    return reqs, resps, order, ctxlink.attach([reqs[s] for s in order], cap, resps)


def cmd_info(args):
    cap = ctxstore.Capture(args.file)
    reqs, resps, order, links = _threads(cap)
    manifest = cap.manifest or {}
    print(cap.path)
    print("  saved     %s  %s" % (manifest.get("created"),
                                  ("from " + manifest.get("source", "")) if manifest.get("source") else ""))
    if manifest.get("note"):
        print("  note      %s" % manifest["note"])
    print("  selection %s" % manifest.get("selection", "-"))
    print("  rounds    %s  (seq %s..%s)  %s to %s"
          % (manifest.get("rounds"), manifest.get("first_seq"), manifest.get("last_seq"),
             manifest.get("first_ts"), manifest.get("last_ts")))
    got = manifest.get("with", {})
    print("  inside    %d requests, %d answers, %s prompt tokens (%s reused, %s answers)"
          % (got.get("requests", 0), got.get("answers", 0), f"{got.get('prompt_tokens', 0):,}",
             f"{got.get('cached_tokens', 0):,}", f"{got.get('answer_tokens', 0):,}"))
    print("  payloads  %s stored, %s" % ("yes" if manifest.get("payloads") else "no",
                                         _human(got.get("payload_bytes", 0))))
    if manifest.get("window"):
        print("  window    %s tokens (%s)" % (f"{manifest['window']:,}", manifest.get("window_src")))
    print("  file size %s" % _human(os.path.getsize(cap.path)))
    print("%d conversations:" % len(links["_threads"]))
    for thread in links["_threads"]:
        print("  thread %s  %d rounds (seq %d..%d)  root prompt %s chars  tools %d  %s"
              % (thread["name"], len(thread["rounds"]), thread["first"], thread["last"],
                 f"{thread['root_chars']:,}", thread["n_tools"],
                 links[thread["first"]]["client"] or "unknown client"))


def cmd_extract(args):
    cap = ctxstore.Capture(args.file)
    written = ctxstore.extract(cap, args.to)
    print("unpacked %s into %s (%d payload files)" % (os.path.basename(cap.path), args.to, written))
    print("the logger can now write on top of it:  .\\Start-ContextMonitor.ps1 ...")


def cmd_open(args):
    cap = ctxstore.Capture(args.file)
    stem = os.path.splitext(os.path.basename(cap.path))[0]
    out = args.out or os.path.join(os.path.dirname(cap.path), stem + "-report.html")
    cmd = [sys.executable, os.path.join(HERE, "ctxpage.py"), "--log", cap.path, "--out", out]
    if args.md:
        cmd += ["--md", os.path.splitext(out)[0] + ".md"]
    if args.max_chars:
        cmd += ["--max-chars", str(args.max_chars)]
    print("building the page from %s" % cap.path)
    if subprocess.call(cmd) != 0:
        sys.exit("the page did not build")
    if not args.no_open:
        try:
            os.startfile(out)          # Windows: open it in the default browser
        except AttributeError:
            print("page written to %s" % out)
    print("page: %s" % out)


def main():
    parser = argparse.ArgumentParser(description="save and open Strata context captures")
    sub = parser.add_subparsers(dest="command", required=True)

    save = sub.add_parser("save", help="take the log (or part of it) into one file")
    save.add_argument("--log", default=os.environ.get("CTX_LOG_DIR") or os.path.join(HERE, "logs"),
                      help="the log folder to read (default: .\\logs)")
    save.add_argument("--out", help="file to write (default: captures\\capture-TIMESTAMP%s)"
                               % ctxstore.SUFFIX)
    save.add_argument("--rounds", help="only these rounds, e.g. 1-20,25,51-54")
    save.add_argument("--last", type=int, default=0, help="only the newest N rounds")
    save.add_argument("--since", help="from this date or timestamp, e.g. 2026-10-20 or '2026-10-20 20:00'")
    save.add_argument("--until", help="up to this date or timestamp")
    save.add_argument("--threads", help="only these conversations, e.g. A,B (see ctxview sessions)")
    save.add_argument("--no-payloads", action="store_true",
                      help="keep the numbers but not the prompt and answer text")
    save.add_argument("--note", default="", help="a line of your own, shown when the capture is opened")
    save.add_argument("--window", type=int, default=0, help="record this context window in the file")
    save.set_defaults(func=cmd_save)

    info = sub.add_parser("info", help="what is inside a capture")
    info.add_argument("file")
    info.set_defaults(func=cmd_info)

    extract = sub.add_parser("extract", help="unpack a capture back into a log folder")
    extract.add_argument("file")
    extract.add_argument("--to", default="logs-from-capture")
    extract.set_defaults(func=cmd_extract)

    openup = sub.add_parser("open", help="build the round page from a capture and open it")
    openup.add_argument("file")
    openup.add_argument("--out", help="where to write the page")
    openup.add_argument("--md", action="store_true", help="also write the markdown report")
    openup.add_argument("--max-chars", type=int, default=0, help="inline text per block")
    openup.add_argument("--no-open", action="store_true", help="do not open it in the browser")
    openup.set_defaults(func=cmd_open)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
