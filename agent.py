#!/usr/bin/env python3
"""
agent.py (v3) -- manual "coding agent" driver for M365 Copilot relay.

You are the loop. Copilot is the brain. This script is the hands.

v3: fence-free protocol. M365 Copilot's message copy button returns
RENDERED text -- code-fence lines (three backticks) do NOT survive the
round trip. So the protocol keys on sentinel LINES, which do survive.
Sentinels are only recognized at column 0 (no indentation), so indented
examples inside documents never collide with them.

Payload format Copilot must emit (taught via BOOTSTRAP.md / Agent Builder):

  -----OPS-----
  read: src/app.py
  read: src/app.py:40-80
  ls: src
  grep: TODO src
  run: pytest -q
  write: src/new.py
  apply:
  -----FILE src/new.py-----
  ...full contents (one FILE section per write: target)...
  -----DIFF-----
  --- a/src/app.py
  +++ b/src/app.py
  @@ -10,3 +10,4 @@
   existing line
  +new line
  -----END-----

Copilot may wrap the whole payload in ONE code fence for display; the
parser ignores fence lines entirely, so it works whether they survive
the copy or not. -----END----- is MANDATORY: a payload without it is
treated as truncated and nothing is executed (tell Copilot "CONTINUE").
A file whose body contains column-0 sentinel-like lines uses a heredoc
terminator:  -----FILE docs/x.md UNTIL:EOF9271-----  ...body...  EOF9271

Cycle
-----
  1. Copy Copilot's reply (the message copy button is fine) into in.md
     (or clipboard, with --clip).
  2. Run:   python agent.py
  3. It runs the OPS section against your real repo.
  4. It writes  out.txt : STATE.md + git status + recent commits + results.
     Every out.txt is self-sufficient: pasted into a NEW chat it re-grounds
     the task, so clearing the chat at any point is safe.
     - Small  -> paste it into Copilot.
     - Large  -> ATTACH out.txt as a file (Copilot accepts TXT uploads;
                 paste limits vary by tenant, attachment is the safe path).
  5. Repeat.

Extras
------
  python agent.py resume      -> writes resume.txt: a fresh-chat packet
                                 (mini-protocol + STATE.md + git status + log).
  auto_commit: true (config)  -> after any write/apply batch, checkpoint with
                                 git add -A / git commit (git = your undo log).
  python agent.py qa deck.pptx -> render slides to JPGs + print a QA prompt
                                 for a fresh-eyes visual check in a new chat.

Dependencies: stdlib only. Clipboard optional (pip install pyperclip).
"""
from __future__ import annotations
import json, re, subprocess, sys
from pathlib import Path

try:  # never crash on console encoding (e.g. Japanese question piped on cp932)
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

CONFIG = {
    "repo": ".",
    "verify": "",            # e.g. "pytest -q" -> auto-run after each batch
    "timeout": 180,
    "max_chars": 6000,       # cap per-action output
    "paste_limit": 7000,     # above this, advise attaching out.txt instead
    "state_max_lines": 60,   # STATE.md is re-pasted every turn; nag above this
    "auto_commit": False,    # git checkpoint after each mutating batch
    "infile": "in.md",
    "outfile": "out.txt",
    "resumefile": "resume.txt",
}

DANGER = [
    r"rm\s+-rf\s+/", r"rm\s+-rf\s+\*", r"git\s+push\s+.*--force",
    r"git\s+reset\s+--hard", r"mkfs", r"dd\s+if=", r">\s*/dev/sd",
    r":\(\)\s*\{", r"chmod\s+-R\s+777\s+/",
]

# Written as "`"*3 so this file itself contains no literal triple backticks
# and travels through markdown/chat channels without nested-fence tricks.
FENCE = "`" * 3

# Column-0 only: indented sentinel-looking lines (e.g. examples in docs)
# are treated as ordinary body content. Trailing dashes are optional --
# models routinely emit "-----FILE x.py" and the relay must tolerate it.
SENTINEL = re.compile(
    r"^-{3,}\s*(?:(OPS|DIFF|ASK|END)|FILE\s+(\S+?)(?:\s+UNTIL:(\S+?))?)\s*-*\s*$",
    re.IGNORECASE,
)

MINI_PROTOCOL = """\
You are a coding agent operating through a human relay (no direct shell).
Reply format, every turn:
1) <scratch> English planning/self-questioning. Predict expected output. </scratch>
2) Exactly ONE machine payload, structured by column-0 sentinel lines
   (you may wrap it in one code fence for display; fences carry no meaning):
     -----OPS-----            read:/ls:/grep:/run:/write:/apply:, small batches
     -----FILE path-----      full file body (one section per write: target)
     -----DIFF-----           unified diff for apply:
     -----ASK-----            a question to the principal, in Japanese, INSTEAD
                              of ops -- only for decisions the human must make
                              (ambiguous requirements, design forks, destructive
                              changes). Investigate with read:/grep: first.
     -----END-----            MANDATORY last line of the payload
   write: PATH runs only if the SAME reply has a matching -----FILE PATH-----
   section: command in OPS, body in FILE -- never inline a body in OPS.
   Per edit send ONE of apply:+DIFF or write:+FILE, not both. DIFF hunks
   need REAL @@ -N,M +N,M @@ line numbers; when unsure prefer write:+FILE.
Rules: READ before you WRITE/APPLY. Never invent file contents. Trust only
the pasted results (## results / git status) as ground truth. Keep replies
compact. A payload without the END sentinel is treated as truncated and
nothing is executed -- when told CONTINUE, resend the cut section from its
own sentinel line onward, smaller if needed.
Keep STATE.md current with write: STATE.md whenever a decision or task
status changes -- new chats resume from STATE.md plus git facts only.
STATE.md stays SMALL with fixed sections 目的/決定事項/完了/次の一手/残タスク;
「次の一手」 always holds the immediate next step, concrete enough for a
brand-new chat, because this chat may be cleared at any moment.
Japanese summary ONLY after verify (tests) passes for a unit of work.
Below is the current ground truth. Continue the task from here.
"""


def load_config() -> dict:
    cfg = dict(CONFIG)
    p = Path(__file__).with_name("agent.config.json")
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8-sig")))
    return cfg


def sh(cmd: str, repo: str, timeout: int) -> str:
    try:
        r = subprocess.run(cmd, shell=True, cwd=repo, timeout=timeout,
                           capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s]"
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return f"[exit {r.returncode}]\n{out}".rstrip()


def clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return f"{s[:limit*3//4]}\n... [truncated {len(s)-limit} chars] ...\n{s[-limit//4:]}"


def read_input(cfg) -> str:
    if "--clip" in sys.argv:
        try:
            import pyperclip
            t = pyperclip.paste()
            if t.strip():
                return t
        except Exception:
            print("(pyperclip unavailable; falling back to file)", file=sys.stderr)
    p = Path(cfg["infile"])
    if not p.exists():
        sys.exit(f"{cfg['infile']} not found. Paste Copilot's reply there first.")
    # utf-8-sig: Windows editors/PowerShell often add a BOM; locale default is cp932
    return p.read_text(encoding="utf-8-sig")


def strip_stray_fence(body: list) -> list:
    """Drop one cosmetic code-fence pair wrapping a section body, if present.
    Copilot habitually fences code; rendered copies usually lose the fence
    lines, but raw pastes keep them -- either way must parse the same."""
    b = list(body)
    while b and not b[0].strip():
        b.pop(0)
    while b and not b[-1].strip():
        b.pop()
    if b and b[0].lstrip().startswith(FENCE):
        rest = b[1:]
        if rest and rest[-1].strip().startswith(FENCE):
            rest = rest[:-1]
        return rest
    return b


def parse_payload(text: str):
    """Sentinel-based parser. Returns (ops_lines, files, diff, truncated).
    Fence lines are cosmetic; only column-0 sentinel lines carry meaning.
    truncated=True means no END sentinel was seen: the open (cut) section
    is dropped and the caller must not execute anything."""
    ops_lines, files, diffs, asks = [], {}, [], []
    current = None
    saw_end = False

    def close(sec):
        body = sec["body"] if sec["until"] else strip_stray_fence(sec["body"])
        if sec["kind"] == "OPS":
            ops_lines.extend(body)
        elif sec["kind"] == "FILE":
            files[sec["arg"]] = "\n".join(body) + "\n"
        elif sec["kind"] == "DIFF":
            diffs.append("\n".join(body))
        elif sec["kind"] == "ASK":
            asks.append("\n".join(body).strip())

    for raw in text.splitlines():
        if current and current["until"]:
            if raw.strip() == current["until"]:
                close(current)
                current = None
            else:
                current["body"].append(raw)
            continue
        m = SENTINEL.match(raw)
        if not m:
            if current:
                current["body"].append(raw)
            continue
        if current:
            close(current)
            current = None
        kw = (m.group(1) or "FILE").upper()
        if kw == "END":
            saw_end = True
            break
        current = {"kind": kw, "arg": m.group(2), "until": m.group(3), "body": []}

    truncated = not saw_end  # an open `current` here is the cut section: dropped
    ops = [s for s in (ln.strip() for ln in ops_lines)
           if s and not s.startswith("#") and not s.startswith(FENCE)
           and not re.fullmatch(r"-+", s)]  # bare --- lines are rendering noise
    diff = ("\n".join(diffs) + "\n") if diffs else None
    ask = "\n\n".join(a for a in asks if a) or None
    return ops, files, diff, ask, truncated


def confirm(cmd: str) -> bool:
    if "--yes" in sys.argv:
        return True
    if any(re.search(p, cmd) for p in DANGER):
        return input(f"\n!! looks destructive: {cmd!r}\n   run it? [y/N] ").strip().lower() == "y"
    return True


def do_ls(arg: str, repo: str) -> str:
    """Portable ls (Windows cmd has no ls; the relay must run anywhere)."""
    base = Path(repo) / (arg or ".")
    if not base.exists():
        return f"[no such path: {arg or '.'}]"
    if base.is_file():
        return f"f {base.stat().st_size:>9} {base.name}"
    rows = []
    for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        size = f"{p.stat().st_size}" if p.is_file() else "-"
        rows.append(f"{'f' if p.is_file() else 'd'} {size:>9} {p.name}")
    return f"[{arg or '.'}: {len(rows)} entries]\n" + "\n".join(rows)


def do_grep(arg: str, repo: str, max_hits: int = 300) -> str:
    """Portable recursive grep (regex), skipping .git and __pycache__."""
    pat, _, path = arg.partition(" ")
    try:
        rx = re.compile(pat)
    except re.error as e:
        return f"[bad pattern {pat!r}: {e}]"
    base = Path(repo) / (path.strip() or ".")
    if not base.exists():
        return f"[no such path: {path.strip() or '.'}]"
    hits, root = [], Path(repo)
    for fp in ([base] if base.is_file() else sorted(base.rglob("*"))):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel.as_posix()}:{i}:{line.strip()}")
        if len(hits) >= max_hits:
            hits = hits[:max_hits] + [f"... [capped at {max_hits} hits]"]
            break
    return "\n".join(hits) if hits else "[no matches]"


def do_read(arg: str, repo: str) -> str:
    m = re.match(r"(.+?):(\d+)-(\d+)$", arg)
    path, rng = (m.group(1), (int(m.group(2)), int(m.group(3)))) if m else (arg, None)
    fp = Path(repo) / path
    if not fp.exists():
        return f"[no such file: {path}]"
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    if rng:
        a, b = rng
        return f"[{path} lines {a}-{b} of {len(lines)}]\n" + "\n".join(lines[a-1:b])
    return f"[{path}, {len(lines)} lines]\n" + "\n".join(lines)


def state_notes(cfg) -> list:
    """Driver-side STATE.md health checks. The file is pasted back to the
    model every turn (per-turn tax -> size cap), and 「次の一手」 is what
    lets a cleared chat resume mid-unit -- so its absence is a defect the
    driver nags about, not a style choice left to the model."""
    p = Path(cfg["repo"]) / "STATE.md"
    if not p.exists():
        return ["[STATE.md does not exist -- create it via write: STATE.md "
                "(sections: 目的/決定事項/完了/次の一手/残タスク). Chats may be "
                "cleared at any moment and resume from STATE.md + git facts alone]"]
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    notes = []
    if "次の一手" not in text:
        notes.append("[STATE.md has no 「次の一手」 section -- add one via "
                     "write: STATE.md: the immediate next step, concrete enough "
                     "that a brand-new chat could execute it directly]")
    n = len(text.splitlines())
    limit = cfg.get("state_max_lines", 60)
    if n > limit:
        notes.append(f"[STATE.md is {n} lines (soft limit {limit}) -- it is "
                     "re-pasted every turn; trim to current facts, one line per "
                     "decision (history already lives in git log)]")
    return notes


def run_batch(lines, files, diff, cfg):
    repo, out, mutated = cfg["repo"], [], False
    written, saw_apply, stray, applied = set(), False, [], []
    for cmd in lines:
        verb, _, arg = cmd.partition(":")
        verb, arg = verb.strip().lower(), arg.strip()
        if verb == "read":
            res = do_read(arg, repo)
        elif verb == "ls":
            res = do_ls(arg, repo)
        elif verb == "grep":
            res = do_grep(arg, repo)
        elif verb == "run":
            res = sh(arg, repo, cfg["timeout"]) if confirm(arg) else "[skipped by human]"
        elif verb == "write":
            if arg not in files:
                res = f"[no FILE section for {arg} in your reply]"
            else:
                fp = Path(repo) / arg
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(files[arg], encoding="utf-8", newline="\n")
                res, mutated = f"[wrote {arg}, {len(files[arg].splitlines())} lines]", True
                written.add(arg)
        elif verb == "apply":
            saw_apply = True
            if not diff:
                res = "[no DIFF section in your reply]"
            else:
                # bytes on stdin: text mode would rewrite \n as \r\n on Windows
                # and the CRLF patch then fails against LF files
                blob = diff.encode("utf-8")
                r = subprocess.run(["git", "apply", "--whitespace=nowarn"],
                                   cwd=repo, input=blob, capture_output=True)
                if r.returncode != 0:
                    r2 = subprocess.run(["git", "apply", "--3way", "--whitespace=nowarn"],
                                        cwd=repo, input=blob, capture_output=True)
                    ok = r2.returncode == 0
                    err = (r.stderr + r2.stderr).decode("utf-8", "replace")
                    res = ("[patch applied via --3way]" if ok else
                           f"[PATCH FAILED]\n{err}\n"
                           "[hint] git apply needs a strict unified diff: hunk "
                           "headers with REAL line numbers (@@ -N,M +N,M @@) and "
                           "exact context lines copied from the file. If you "
                           "cannot compute them, use write: with a FILE section "
                           "(full file) instead of a diff.")
                    mutated = mutated or ok
                else:
                    res, mutated, ok = "[patch applied cleanly]", True, True
                if ok:
                    applied.extend(re.findall(r"^\+\+\+ (?:b/)?(\S+)", diff, re.M)
                                   or ["diff"])
        else:
            stray.append(cmd)
            continue
        out.append(f"### {cmd}\n{clip(res, cfg['max_chars'])}")
    if stray:
        shown = "\n".join(stray[:5]) + ("\n..." if len(stray) > 5 else "")
        out.append(f"### ignored: {len(stray)} non-command line(s) in the OPS section\n"
                   "[not one of read:/ls:/grep:/run:/write:/apply:. If this was a file "
                   "body, it belongs in a -----FILE path----- section, not inline in OPS]\n"
                   + shown)
    notes = [f"[FILE section {p} was NOT written: no matching 'write: {p}' op]"
             for p in files if p not in written]
    if diff and not saw_apply:
        notes.append("[DIFF section present but no 'apply:' op -- the diff was NOT applied]")
    if mutated and "STATE.md" not in written:
        notes.append("[reminder] code changed but STATE.md was not updated -- if a "
                     "decision or task status changed, write: STATE.md in a coming batch")
    notes.extend(state_notes(cfg))  # checks post-batch content: writes ran above
    if notes:
        out.append("### relay notes\n" + "\n".join(notes))
    if cfg["verify"]:
        vres = sh(cfg["verify"], repo, cfg["timeout"])
        # human-facing progress signal; the full output still goes to out.txt
        print(f"verify {'GREEN' if vres.startswith('[exit 0]') else 'RED'}: {cfg['verify']}")
        out.append(f"### verify: {cfg['verify']}\n" + clip(vres, cfg["max_chars"]))
    if mutated and cfg.get("auto_commit"):
        summary = ", ".join([f"write {p}" for p in sorted(written)]
                            + [f"apply {p}" for p in applied])
        out.append("### checkpoint\n" + checkpoint(repo, summary))
    return "\n\n".join(out)


def checkpoint(repo: str, summary: str = "") -> str:
    """Git checkpoint after a mutating batch. Driver files stay unstaged.
    The ops summary goes into the commit message so `git log --oneline`
    doubles as an activity log (and feeds the resume packet).
    Plain subprocess calls, no shell string -- works on Windows cmd too."""
    def g(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, errors="replace", timeout=60)
    g("add", "-A", ".")
    g("reset", "-q", "--", "agent.py", "agent.config.json",
      "in.md", "out.txt", "resume.txt")
    if g("diff", "--cached", "--quiet").returncode == 0:
        return "[nothing to commit]"
    msg = ("agent: " + summary)[:72] if summary else "agent batch checkpoint"
    r = g("commit", "-m", msg)
    return (r.stdout + r.stderr).strip() or f"[commit exit {r.returncode}]"


def ground(cfg) -> str:
    repo, parts = cfg["repo"], []
    state = Path(repo) / "STATE.md"
    if state.exists():
        parts.append("## STATE.md\n" + state.read_text(encoding="utf-8-sig", errors="replace"))
    parts.append("## git status\n" + sh("git status --short --branch", repo, 30))
    parts.append("## git diff --stat\n" + sh("git diff --stat", repo, 30))
    # trajectory, not just state: with auto_commit the subjects are ops
    # summaries, so any single out.txt can seed a brand-new chat
    parts.append("## recent commits\n" + sh("git log --oneline -5", repo, 30))
    return "\n\n".join(parts)


def write_out(path: str, text: str, paste_limit: int):
    Path(path).write_text(text, encoding="utf-8")
    try:
        import pyperclip
        pyperclip.copy(text)
        note = ", copied to clipboard"
    except Exception:
        note = ""
    how = ("paste it into Copilot" if len(text) <= paste_limit
           else f"LARGE ({len(text)} chars): ATTACH {path} as a file instead of pasting")
    print(f"Wrote {path}{note}. -> {how}")


def cmd_resume(cfg):
    """Fresh-chat handoff packet: model-maintained intent (STATE.md) plus
    driver-collected facts (files, activity log, verify status), with a
    staleness warning when STATE.md lags behind the code."""
    repo = cfg["repo"]
    parts = [MINI_PROTOCOL, ground(cfg)]  # ground already carries recent commits
    parts.append("## files (git ls-files)\n" + sh("git ls-files", repo, 30))
    if cfg["verify"]:
        vres = sh(cfg["verify"], repo, cfg["timeout"])
        status = "GREEN" if vres.startswith("[exit 0]") else "RED"
        parts.append(f"## verify at handoff: {status}\n" + clip(vres, 1500))
    parts.extend(state_notes(cfg))
    touched = sh("git log -5 --format= --name-only", repo, 30)
    if "STATE.md" not in touched:
        parts.append("[warning] STATE.md was NOT updated in the last 5 commits and is "
                     "probably STALE. FIRST batch: read the key files, reconcile "
                     "STATE.md (次の一手/残タスク) with reality via write: STATE.md, "
                     "then continue the task.")
    packet = ("\n\n".join(parts)
              + "\n\nStart with a <scratch> plan and one small OPS batch.")
    write_out(cfg["resumefile"], packet, cfg["paste_limit"])


QA_PROMPT = """\
You are a fresh reviewer with no memory of how these slides were built.
Inspect the attached slide images ONLY for user-visible defects:
- text overflowing or cut off at a box/edge (most common — check every text box)
- overlapping elements (text over shapes, lines through words)
- footers/citations colliding with content above
- gaps too tight (<0.3") or wildly uneven; margins <0.5" from slide edge
- columns/cards not aligned; low-contrast text or icons
- leftover placeholder text (xxxx, lorem, TODO, [insert ...])
For each slide, list only defects a viewer would notice. Skip cosmetic nitpicks.
Return a per-slide list I can paste back to the build chat."""


def cmd_qa(cfg, pptx: str):
    """Render a .pptx to per-slide JPGs locally, then print paths + a QA prompt.
    You attach the JPGs to a NEW Copilot chat with the printed prompt =
    the skill's 'subagent with fresh eyes' visual QA, done via the relay."""
    repo = cfg["repo"]
    if not (Path(repo) / pptx).exists():
        print(f"[warn] {pptx} not found under {repo}; will still print the QA prompt.")
    conv = None
    for soffice in ("soffice", "libreoffice"):
        r = subprocess.run(f"command -v {soffice}", shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            conv = soffice
            break
    if conv:
        print(sh(f'{conv} --headless --convert-to pdf "{pptx}" && '
                 f'rm -f slide-*.jpg && pdftoppm -jpeg -r 150 "{Path(pptx).stem}.pdf" slide && '
                 f'ls -1 "$PWD"/slide-*.jpg', repo, cfg["timeout"]))
    else:
        print("[warn] LibreOffice (soffice/libreoffice) not found. Install it + poppler "
              "(pdftoppm), or convert to images another way, then attach the JPGs.")
    print("\n--- paste this into a NEW Copilot chat and attach the slide JPGs above ---\n")
    print(QA_PROMPT)


def main():
    cfg = load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "resume":
        return cmd_resume(cfg)
    if len(sys.argv) > 2 and sys.argv[1] == "qa":
        return cmd_qa(cfg, sys.argv[2])
    text = read_input(cfg)
    lines, files, diff, ask, truncated = parse_payload(text)
    if truncated:
        sys.exit("Payload has no -----END----- sentinel (reply was likely cut off). "
                 "Nothing was executed. Send CONTINUE to Copilot, then remove the cut "
                 "section from the end of in.md, append the resent section, and re-run.")
    if ask:
        # the principal's turn: surface the question, execute nothing
        print("\n########  ASK -- the agent needs YOUR decision  ########\n")
        print(ask)
        print("\n#########################################################")
        print("Answer directly in the chat. No out.txt this turn.")
        if lines or files or diff:
            print("[note] the reply also had ops/file/diff sections; they were "
                  "NOT executed. The agent should resend them after your answer.")
        return
    if not lines and not files and not diff:
        sys.exit("No OPS / FILE / DIFF / ASK sentinel sections found in the input. "
                 "(If the reply was plain prose addressed to you, just answer it "
                 "in the chat.)")
    results = (run_batch(lines, files, diff, cfg) if lines
               else "(no ops; payload sections only -- add an OPS section with write:/apply:)")
    report = (f"{ground(cfg)}\n\n---\n## results\n{results}\n\n"
              "(End of results. Continue in English <scratch>, keep the next ops batch small, "
              "and give the Japanese summary only when this unit of work passes verify.)")
    write_out(cfg["outfile"], report, cfg["paste_limit"])


if __name__ == "__main__":
    main()
