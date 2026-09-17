"""Handle one Codex hook event.

Wiring (see README.md):

- ``SessionStart`` (``startup|clear|compact``) and ``SubagentStart`` inject the
  thread's session start rules. ``compact`` first forgets what the thread
  received, because compaction removed it from the conversation.
- ``PreToolUse`` (``Bash|apply_patch|view_image``) injects the rules that the
  files a tool call touches load, and session start rules the thread has not
  received yet.
- ``PostCompact`` forgets what the thread received and prints nothing.

Every other event, and ``SessionStart`` with source ``resume``, does nothing:
a resumed thread still holds what it received. With
``C2C_RULESYNC_EPHEMERAL_RULES=0``, no event does anything in a thread whose
payload has a null ``transcript_path``, such as a side conversation: it gets no
rules and leaves no state.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Mapping

from c2c_rulesync.payload import Payload, parse_payload
from c2c_rulesync.state import LockTimeout, State, ThreadState, state_root, sweep
from c2c_rulesync.touched import touched_paths

__all__ = ["run_hook"]

_START_SOURCES = ("startup", "clear", "compact")
# Rules for touched files are looked up until this many seconds after the hook
# started; the rest are left for a later tool call, within the CLI's deadline.
TRIGGER_BUDGET_SECONDS = 3.0


def run_hook(
    data: bytes,
    emit: Callable[[bytes], None],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Handle the hook payload ``data``, passing any output to ``emit``.

    ``emit`` is called at most once, with one JSON object. Errors propagate;
    the caller turns them into a silent exit.
    """
    started = time.monotonic()
    environ = os.environ if environ is None else environ
    payload = parse_payload(data)
    if payload is None:
        return
    event = payload.event
    injects = event in ("SessionStart", "SubagentStart", "PreToolUse")
    if not injects and event != "PostCompact":
        return
    if event == "SessionStart" and payload.source not in _START_SOURCES:
        return
    if payload.transcript_null and environ.get("C2C_RULESYNC_EPHEMERAL_RULES") == "0":
        # Codex keeps no transcript for an ephemeral thread, such as a side
        # conversation, and nothing else in the payload tells it apart.
        return

    root = state_root(environ)
    thread = (
        ThreadState(root, payload.session_id, payload.thread())
        if root is not None and payload.session_id is not None
        else None
    )
    if root is not None:
        # The sweep runs before any output, so that nothing slow follows the
        # record of delivered rules; this session is marked as in use first.
        with contextlib.suppress(OSError):
            if thread is not None:
                thread.touch()
            sweep(root)
    if thread is not None and (event == "PostCompact" or payload.source == "compact"):
        try:
            thread.new_epoch()
        except OSError:
            # Forgetting the record is as good as a new epoch; if that fails
            # too, this start injects without a record.
            try:
                thread.forget()
            except OSError:
                thread = None
    if injects:
        _inject(payload, thread, environ, emit, started)


def _inject(
    payload: Payload,
    thread: ThreadState | None,
    environ: Mapping[str, str],
    emit: Callable[[bytes], None],
    started: float,
) -> None:
    event = payload.event
    cwd = payload.cwd or os.getcwd()
    home = environ.get("HOME") if os.path.isabs(environ.get("HOME", "")) else None
    start_known_done = False
    touched: list[str] = []
    if event == "PreToolUse":
        # Without state every tool call would inject the same rules again.
        if thread is None:
            return
        touched = touched_paths(payload.tool_name or "", payload.tool_input, cwd, home)
        start_known_done = thread.peek().start_done
        if not touched and start_known_done:
            return

    from c2c_rulesync.render import display_path, hook_output, render_rules
    from c2c_rulesync.rules import RuleFinder, SessionRules

    # Discovery runs before taking the lock, which parallel tool calls share.
    finder = RuleFinder(cwd, _user_rules_dir(environ, cwd, home))
    start_rules = None if start_known_done else finder.session_start_rules()
    triggered = []
    for path in touched:
        if time.monotonic() - started > TRIGGER_BUDGET_SECONDS:
            break
        triggered.append((path, finder.trigger_rules(path)))

    def respond(state: State, notes: tuple[str, ...] = ()) -> tuple[bytes | None, State]:
        nonlocal start_rules
        session = SessionRules(set(state.loaded))
        rules = []
        start_done = state.start_done
        if not start_done:
            # A compaction since the unlocked read makes start rules due again.
            if start_rules is None:
                start_rules = finder.session_start_rules()
            rules += session.take(start_rules)
            start_done = True
        for path, found in triggered:
            # Claude Code records a read before loading the rules it triggers.
            # Only a Markdown file can be a rule file, so other reads are not
            # recorded and the record stays small.
            if path.endswith(".md"):
                session.mark_read(path)
            rules += session.take(found)
        warnings = [w for w in dict.fromkeys(finder.warnings) if w not in state.warned]
        messages = [f"c2c-rulesync: {note}" for note in (*notes, *warnings)]
        if rules:
            loaded = ", ".join(display_path(rule.path, cwd, home) for rule in rules)
            messages.insert(0, f"c2c-rulesync loaded {loaded}")
        output = hook_output(event, render_rules(rules, cwd, home), messages)
        return output, State(start_done, session.loaded, state.warned + warnings)

    if thread is None:
        output, _ = respond(State(False, set(), []))
        if output is not None:
            emit(output)
        return
    emitted = False
    try:
        with thread.locked() as transaction:
            output, new_state = respond(transaction.state)
            if output is None and _unchanged(transaction.state, new_state):
                return
            transaction.stage(new_state)
            if output is not None:
                emitted = True
                emit(output)
            transaction.commit()
    except (LockTimeout, OSError) as error:
        # Without usable state only the start of a thread still injects; a
        # tool call would repeat its rules on every call.
        if emitted or event == "PreToolUse":
            raise
        note = (
            f"cannot record delivered rules in {thread.directory} ({_describe(error)}); "
            "rules with paths are not delivered in this thread"
        )
        output, _ = respond(State(False, set(), []), (note,))
        if output is not None:
            emit(output)


def _describe(error: BaseException) -> str:
    if isinstance(error, LockTimeout):
        return "another hook process holds its lock"
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return type(error).__name__


def _unchanged(old: State, new: State) -> bool:
    return (old.start_done, old.loaded, old.warned) == (new.start_done, new.loaded, new.warned)


def _user_rules_dir(environ: Mapping[str, str], cwd: str, home: str | None) -> str | None:
    """Claude Code's user rules directory, or ``None`` when user rules are turned off."""
    if environ.get("C2C_RULESYNC_USER_RULES") == "0":
        return None
    config = environ.get("CLAUDE_CONFIG_DIR", "")
    if config:
        if config == "~" or config.startswith("~/"):
            if home is None:
                return None
            config = home + config[1:]
        return os.path.join(cwd, config, "rules")
    return None if home is None else os.path.join(home, ".claude", "rules")
