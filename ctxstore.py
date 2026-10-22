"""Save a capture to one file, and read the tools from that file later.

A capture is a plain zip with a different ending:

    manifest.json     what it is, when it was taken, which rounds are in it
    events.jsonl      the same lines the logger writes (requests and answers)
    payloads/...      the full request bodies and the full answers

Anything that reads the live log directory can read a capture instead:

    python ctxpage.py --log captures\\session.ctxcap --out report.html

and the page makes them: tab 2 "Capture files" saves the whole log, the newest N
rounds, a list of rounds, only some threads, or the rounds picked in tab 3 - with a
note, and with the choice of counts only, no prompt text. The choose and save
functions below are what its Save button calls.
"""

import json
import os
import shutil
import time
import zipfile

FORMAT = "strata-context-capture"
VERSION = 1
SUFFIX = ".ctxcap"


class Capture:
    """The log, from wherever it lives: a directory the logger writes into, or a
    capture file that someone saved earlier."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.zip = None
        self.manifest = None
        if os.path.isdir(self.path):
            self.kind = "dir"
            self.log = os.path.join(self.path, "requests.jsonl")
            if not os.path.isfile(self.log):
                raise SystemExit("no log yet in %s\nStart the logger first "
                                 "(Start-ContextMonitor.ps1)." % self.path)
        elif os.path.isfile(self.path):
            self.kind = "file"
            try:
                self.zip = zipfile.ZipFile(self.path)
                self.manifest = json.loads(self.zip.read("manifest.json").decode("utf-8"))
            except (zipfile.BadZipFile, KeyError, ValueError):
                raise SystemExit("%s is not a capture file. Make one in the page: run "
                                 "Start-ContextMonitor.ps1, tab 2 \"Capture files\", and "
                                 "press \"Save and open it\"." % self.path)
            if self.manifest.get("format") != FORMAT:
                raise SystemExit("%s is a capture written by an older tool, which this "
                                 "version cannot read. It is a zip file: unpack it into a "
                                 "folder and read that folder instead." % self.path)
        else:
            raise SystemExit("no log there: %s\nPoint --log at the log folder (logs) or at "
                             "a saved capture file (.ctxcap)." % self.path)

    @property
    def label(self):
        """What to call this log in a header or a message."""
        if self.kind == "dir":
            return os.path.join(self.path, "requests.jsonl")
        return "%s  (saved %s)" % (self.path, (self.manifest.get("created") or "?")[:19])

    def events(self):
        """The raw event lines, newest at the bottom."""
        if self.zip:
            return self.zip.read("events.jsonl").decode("utf-8", "ignore")
        with open(self.log, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()

    def payload(self, name):
        """The stored text of one request body or answer, or None if it is not there."""
        if not name:
            return None
        name = name.replace("\\", "/").lstrip("/")
        if self.zip:
            try:
                return self.zip.read(name).decode("utf-8", "ignore")
            except KeyError:
                # a capture made by hand, or moved around: fall back to the file name
                wanted = name.split("/")[-1]
                for member in self.zip.namelist():
                    if member.endswith("/" + wanted) or member == wanted:
                        return self.zip.read(member).decode("utf-8", "ignore")
                return None
        path = os.path.join(self.path, *name.split("/"))
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()
        except OSError:
            return None

    def window(self):
        """The context window recorded when the capture was taken, if any."""
        size = (self.manifest or {}).get("window")
        return (size, self.manifest.get("window_src")) if size else (None, None)

    def note(self):
        return (self.manifest or {}).get("note") or ""

    def close(self):
        """Let go of the file, so it can be renamed or rewritten on Windows."""
        if getattr(self, "zip", None) is not None:
            try:
                self.zip.close()
            except Exception:
                pass
            self.zip = None


def _events(cap, keep_other=False):
    """(requests, responses, order) for a capture, oldest round first. Rounds whose
    request was never a model call - the app's own page traffic, logged before this
    was filtered - are left out unless keep_other asks for them."""
    reqs, resps, order = {}, {}, []
    for line in cap.events().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "request":
            reqs[record["seq"]] = record
            order.append(record["seq"])
        elif record.get("type") == "response":
            resps[record["seq"]] = record
    if not keep_other:
        junk = set(seq for seq, rec in reqs.items() if not rec.get("api"))
        if junk:
            order = [seq for seq in order if seq not in junk]
            for seq in junk:
                reqs.pop(seq, None)
                resps.pop(seq, None)
    return reqs, resps, order


def _usage(usage):
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("cached_tokens") or 0
    return prompt, cached, out


def parse_rounds(text):
    """'1-20,25,51-54' -> {1..20, 25, 51..54}. Empty means everything."""
    wanted = set()
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            wanted.update(range(int(first or 1), int(last or first or 1) + 1))
        else:
            wanted.add(int(part))
    return wanted


def choose(reqs, order, last=0, rounds="", since="", until="", threads="", cap=None):
    """Which rounds a capture should hold, from the filters people pick: the newest N,
    round numbers, dates, whole conversations by name. A thread name that matches nothing
    raises ValueError saying what the log does hold."""
    keep = set(order)
    if rounds:
        wanted = parse_rounds(rounds)
        keep &= wanted
    if since:
        keep &= {s for s in keep if str(reqs[s].get("ts") or "") >= _stamp(since)}
    if until:
        keep &= {s for s in keep if str(reqs[s].get("ts") or "") <= _stamp(until, end=True)}
    if threads:
        import ctxlink
        links = ctxlink.attach([reqs[s] for s in order], cap, None)
        wanted = set()
        for thread in ctxlink.match_threads(links.get("_threads", []), threads):
            wanted.update(thread["rounds"])
        keep &= wanted
    chosen = sorted(keep)
    if last and len(chosen) > last:
        chosen = chosen[-last:]
    return chosen


def _stamp(text, end=False):
    """Accept '2026-10-20', '2026-10-20 20:00' or a full timestamp."""
    text = text.strip().replace(" ", "T")
    if end and len(text) <= 10:
        text += "T23:59:59"
    return text


def _lines_and_names(reqs, resps, keep):
    """The event lines and payload file names for a set of rounds, oldest first."""
    lines, names = [], []
    for seq in keep:
        request = reqs.get(seq)
        if request:
            if request.get("payload_file"):
                names.append(request["payload_file"])
            lines.append(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        answer = resps.get(seq)
        if answer:
            if answer.get("response_file"):
                names.append(answer["response_file"])
            lines.append(json.dumps(answer, ensure_ascii=False, separators=(",", ":")))
    return lines, sorted(set(n.replace("\\", "/") for n in names))


def _tally(reqs, resps, keep, payload_bytes):
    counts = {"requests": 0, "answers": 0, "prompt_tokens": 0, "cached_tokens": 0,
              "answer_tokens": 0, "payload_bytes": payload_bytes}
    for seq in keep:
        if reqs.get(seq):
            counts["requests"] += 1
        if resps.get(seq):
            counts["answers"] += 1
        prompt, cached, out = _usage((resps.get(seq) or {}).get("usage"))
        counts["prompt_tokens"] += prompt
        counts["cached_tokens"] += cached
        counts["answer_tokens"] += out
    return counts


def _write_zip(out_path, manifest, lines, names, lookup, missing=None):
    folder = os.path.dirname(os.path.abspath(out_path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        archive.writestr("events.jsonl", "\n".join(lines) + "\n")
        for name in names:
            text = lookup(name)
            if text is not None:
                archive.writestr(name, text)
            elif missing is not None:
                missing.append(name)


def save(source, out_path, chosen, window=None, window_src="", note="", with_payloads=True,
         selection=""):
    """Write the chosen rounds, and the payloads they name, into one capture file."""
    reqs, resps, _ = _events(source)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": note,
        "source": source.path,
        "took_dir": source.kind == "dir",
        "selection": selection,
        "rounds": len(chosen),
        "first_seq": chosen[0] if chosen else None,
        "last_seq": chosen[-1] if chosen else None,
        "first_ts": (reqs.get(chosen[0]) or {}).get("ts") if chosen else None,
        "last_ts": (reqs.get(chosen[-1]) or {}).get("ts") if chosen else None,
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
        "payloads": with_payloads,
        "with": {"requests": 0, "answers": 0, "prompt_tokens": 0, "cached_tokens": 0,
                 "answer_tokens": 0, "payload_bytes": 0},
    }
    if window:
        manifest["window"] = window
        manifest["window_src"] = window_src

    lines, names = _lines_and_names(reqs, resps, chosen)
    if not with_payloads:
        names = []
    payload_bytes = 0
    for name in names:
        text = source.payload(name)
        if text is not None:
            payload_bytes += len(text.encode("utf-8"))
    manifest["with"] = _tally(reqs, resps, chosen, payload_bytes)
    missing = []
    _write_zip(out_path, manifest, lines, names, source.payload, missing)
    if missing:
        manifest["missing"] = missing
    return manifest


def extract(cap, to_dir):
    """Unpack a capture into a plain log folder, the way the logger writes one,
    so anything that wants a folder (or the logger itself) can use it."""
    if cap.kind != "file":
        raise SystemExit("%s is already a folder" % cap.path)
    os.makedirs(os.path.join(to_dir, "payloads"), exist_ok=True)
    with open(os.path.join(to_dir, "requests.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(cap.events())
    written = 0
    for member in cap.zip.namelist():
        if member.startswith("payloads/"):
            with open(os.path.join(to_dir, *member.split("/")), "wb") as handle:
                handle.write(cap.zip.read(member))
            written += 1
    return written


def delete_rounds(cap, seqs):
    """Take rounds out of a saved capture, along with their answers and payload files.
    The file is rewritten, and the version before is kept beside it as
    NAME.bak-TIMESTAMP, so a wrong deletion can be put back."""
    if cap.kind != "file":
        raise ValueError("the live log cannot be edited - the logger is still writing it. "
                         "Save a capture first, then delete rounds from that file.")
    gone = set(int(s) for s in seqs)
    reqs, resps, order = _events(cap)
    keep = [s for s in order if s not in gone]
    removed = sorted(gone & set(order))
    if not removed:
        return {"removed": [], "rounds": len(keep), "backup": None, "path": cap.path}
    manifest = dict(cap.manifest or {})
    manifest["rounds"] = len(keep)
    manifest["first_seq"] = keep[0] if keep else None
    manifest["last_seq"] = keep[-1] if keep else None
    manifest["first_ts"] = (reqs.get(keep[0]) or {}).get("ts") if keep else None
    manifest["last_ts"] = (reqs.get(keep[-1]) or {}).get("ts") if keep else None
    manifest["edited"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest["removed"] = sorted(set(int(s) for s in (manifest.get("removed") or [])) | gone)
    manifest["selection"] = (manifest.get("selection") or "") + "  (edited: removed %s)" % _brief(removed)
    lines, names = _lines_and_names(reqs, resps, keep)
    texts = {}
    for name in names:
        text = cap.payload(name)
        if text is not None:
            texts[name] = text
    manifest["with"] = _tally(reqs, resps, keep,
                              sum(len(t.encode("utf-8")) for t in texts.values()))
    manifest.pop("missing", None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (cap.path, stamp)
    temp = "%s.writing-%s" % (cap.path, stamp)
    # read everything we need first, then put the file down: Windows will not move
    # a file that is still open, and anything else may be holding this one
    cap.close()
    _write_zip(temp, manifest, lines, names, texts.get)
    # Write the new version over the old one, instead of moving files around: another
    # program may be reading this file, and Windows will not rename it while it does.
    try:
        shutil.copy2(cap.path, backup)
        shutil.copyfile(temp, cap.path)
    except OSError as exc:
        if os.path.exists(backup):
            shutil.copyfile(backup, cap.path)      # put back what we had
        raise ValueError("the capture file could not be rewritten: %s" % exc)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass
    return {"removed": removed, "rounds": len(keep), "backup": backup, "path": cap.path}


def _brief(seqs):
    """1, 2, 3, 7 -> '1-3, 7' for a note someone will read later."""
    seqs = sorted(set(int(s) for s in seqs))
    parts, start, last = [], None, None
    for s in seqs:
        if start is None:
            start = last = s
        elif s == last + 1:
            last = s
        else:
            parts.append("%d-%d" % (start, last) if last > start else "%d" % start)
            start = last = s
    if start is not None:
        parts.append("%d-%d" % (start, last) if last > start else "%d" % start)
    return ", ".join(parts)


def list_captures(folder):
    """The capture files in a folder, newest name first, with what each one says."""
    out = []
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except OSError:
        return out
    for name in names:
        if not name.endswith(SUFFIX):
            continue
        path = os.path.abspath(os.path.join(folder, name))
        if not zipfile.is_zipfile(path):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception:
            manifest = {"format": "?", "note": "not a capture this tool can read"}
        out.append({"name": name, "path": path, "bytes": os.path.getsize(path),
                    "manifest": manifest})
    return out


def load_hidden(folder):
    """Which rounds each source is hiding from the view. View only: nothing is removed."""
    try:
        with open(os.path.join(folder, "hidden.json"), encoding="utf-8") as handle:
            table = json.load(handle)
        return {str(k): sorted(int(s) for s in v) for k, v in table.items()}
    except (OSError, ValueError, TypeError):
        return {}


def set_hidden(folder, source_path, seqs):
    table = load_hidden(folder)
    if seqs:
        table[str(source_path)] = sorted(set(int(s) for s in seqs))
    else:
        table.pop(str(source_path), None)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "hidden.json"), "w", encoding="utf-8") as handle:
            json.dump(table, handle, indent=1)
    except OSError:
        pass
    return table
