"""Command line entry point.

    marginal chrome                          launch the dedicated Chrome profile
    marginal read    <doc>                   dump the document text
    marginal list    <doc>                   list existing comment threads

  Leave comments. `mode` in the config says who writes them:
    marginal comment <doc> -n 5              mode = "api": run the loop here
                                                  mode = "agent": print the brief

  The same two modes, named explicitly. `comment` is these commands, chosen by
  config rather than by which one you remember to type:
    marginal review  <doc> -n 5              API mode; needs a model API key
    marginal context <doc>                   agent mode; guidance + document
    marginal submit-brief <doc>              agent mode; placing, for a subagent
    marginal post-batch <doc> --from -       agent mode; post its JSON array

  Either way:
    marginal respond <doc>                   answer replies to our comments
    marginal post    <doc> -q Q -b BODY      post one comment by hand
    marginal reply   <doc> -c ID -b BODY     reply in a thread (no browser)
    marginal unpost  <doc> -c ID [-c ID]     delete comments we created

Every command that takes part in writing a comment takes the same flags, because
each one is a setting both modes read from the same config. A flag that shaped an
API-mode run and had no equivalent on the agent-mode command was a difference
between the modes that nobody chose — see `_commenting` and `_placing` below.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

from . import auth, config as config_mod
from . import gdocs, ledger, reactions
from . import setup as setup_mod
from .cdp import launch_chrome, wait_for_port
from .docs_ui import open_doc
from .post import post_many, unpost
from .run import _pick_tab as pick_tab
from .run import context, post_batch, respond, review, submit_brief

# The settings a run is tuned with, and the settings that decide where a comment
# lands. Split in two only because a command that never opens the editor has no use
# for a selection strategy; within each group every command gets the identical set.
#
# This exists so the two modes cannot drift by accident. `review` used to own the
# critic flags and `post-batch` owned none, so an agent-mode run silently took the
# configured defaults while an API-mode run took whatever was typed — and the
# resulting comments differed in length for a reason that had nothing to do with
# which mode wrote them.


def _commenting(p, doc: bool = True) -> None:
    """What a comment is: which model, how hard it thinks, how short the critic cuts.

    `doc=False` is for `config`, which takes the same flags without a document so
    that "what would this run use" can be asked with the flags you meant to use.
    """
    if doc:
        p.add_argument("doc")
        p.add_argument("--tab")
    p.add_argument("-n", type=int, dest="comments", help="comment budget (default: config)")
    p.add_argument("--mode", choices=("api", "agent"), help="who writes the comments")
    p.add_argument("--model")
    p.add_argument("--focus", help="steer what the model looks for")
    p.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        help="reasoning effort for the commenter (default: the provider's)",
    )
    p.add_argument("--provider", choices=("auto", "anthropic", "openrouter"))
    _web_search_flags(p)
    p.add_argument(
        "--suggestions",
        dest="suggestions",
        action="store_true",
        default=None,
        help="let the commenter propose suggested edits (tracked changes) as well",
    )
    p.add_argument(
        "--no-suggestions",
        dest="suggestions",
        action="store_false",
        default=None,
        help="comments only, overriding a config that enables suggestions",
    )
    p.add_argument(
        "--no-critic",
        dest="critic",
        action="store_false",
        default=None,
        help="skip the editing pass; post the commenter's own wording",
    )
    p.add_argument("--critic-model", help="model for the editing pass (default: claude-opus-5)")
    p.add_argument("--critic-effort", choices=("low", "medium", "high", "xhigh", "max"))
    p.add_argument("--min-words", type=int, help="shortest a typical comment should be")
    p.add_argument("--max-words", type=int, help="longest a typical comment should be")
    p.add_argument("--word-ceiling", type=int, help="hard upper bound on comment length")
    p.add_argument(
        "--schedule",
        choices=("async", "sync"),
        help="async: overlap critique and posting with writing (default). "
        "sync: one comment start to finish, then the next",
    )


def _reacts(obj: dict) -> str:
    """The reactions on one comment or reply, as `  👍2 (Ada, Grace)`."""
    out = []
    for r in obj.get("reactions") or []:
        who = f" ({', '.join(r['voters'])})" if r["voters"] else ""
        out.append(f"{r['emoji']}{r['count']}{who}")
    return "  " + "  ".join(out) if out else ""


def _colour(code: str, text: str) -> str:
    """ANSI, and only to a terminal. A pipe or a file gets plain text."""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


# Settings grouped the way the example config groups them, so the printed view and
# the file being explained have the same shape.
_GROUPS = (
    ("what runs", ("mode", "model", "provider", "effort", "comments", "suggestions",
                   "source", "schedule")),
    ("the editing pass", ("critic", "critic_model", "critic_effort", "critic_workers",
                          "min_words", "max_words", "word_ceiling")),
    ("browser", ("port", "profile", "headless", "strategy")),
    ("identity", ("account", "credentials")),
    ("prompts", ("commenter_prompt", "critique_prompt", "respond_prompt")),
)


def _all_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`_GROUPS`, plus whatever it does not mention.

    The grouping is a presentation choice and has to be written by hand; *which
    settings exist* is not, and listing those by hand meant the command that answers
    "what would this run use" quietly did not mention eight of them — including
    `reconcile_anchors`, which decides whether the anchor ladder has a model rung, and
    `header`, which decides which comments the tool believes are its own. Setting one
    of those and running `config` to check produced no confirmation either way.
    """
    named = {name for _, names in _GROUPS for name in names}
    rest = tuple(f.name for f in fields(config_mod.Config) if f.name not in named)
    return _GROUPS + ((("everything else", rest),) if rest else ())


def _show_config(cfg, where: dict[str, str]) -> int:
    """Print the settings a run here would use, and where each one came from.

    A TOML file cannot colour itself, and the question this answers is not "what
    does the file say" — `cat` does that — but "which of my two files won".
    """
    default = config_mod.Config()
    for title, names in _all_groups():
        print(_colour("1", f"# {title}"))
        for name in names:
            value = getattr(cfg, name)
            shown = "unset" if value is None else str(value)
            source = where.get(name)
            # Padded before colouring: an escape sequence counts toward a format
            # width and would knock the column out by exactly its own length.
            field = f"{name:<20}"
            # Bright for anything you set, dim for a default nobody touched: the
            # short list of things this run does differently should be readable at a
            # glance rather than counted out of forty lines.
            if source:
                print(f"  {_colour('36', field)} {_colour('1;32', shown)}"
                      f"  {_colour('2', '← ' + source)}")
            elif value != getattr(default, name):
                print(f"  {_colour('36', field)} {shown}")
            else:
                print(_colour("2", f"  {field} {shown}"))
        print()
    # Not a setting, and printed anyway. A bundled OAuth client means a run reaches
    # the user's documents through somebody else's Google Cloud project, which is
    # the sort of thing that should be visible rather than deduced.
    print(_colour("1", "# oauth client"))
    found = auth.client_source()
    if found is None:
        print(_colour("2", f"  {'none':<20} run `marginal auth --client ...`"))
    else:
        path, kind = found
        field = f"{kind:<20}"
        print(f"  {_colour('36', field)} {path}" if kind == "your own"
              else _colour("2", f"  {field} {path}"))
    print()
    return 0


# Which mode each explicit command belongs to. `comment` is in neither: it is
# whichever one the config names.
_MODE_OF = {
    "review": "api",
    "context": "agent",
    "submit-brief": "agent",
    "post-batch": "agent",
}


def _placing(p) -> None:
    """How a comment reaches the document. Only for commands that open the editor."""
    p.add_argument("--port", type=int)
    p.add_argument("--strategy", choices=("paragraph", "chars"))
    p.add_argument("--dry-run", action="store_true", help="show proposals, post nothing")


def _web_search_flags(p) -> None:
    """The same flags on both modes, because both modes are told the same thing.

    Called from `_commenting`, so searching is a setting like every other one rather
    than a flag that exists on whichever command happened to grow it. The cap only
    binds on the direct Anthropic route — agent mode searches through its own
    harness, and OpenRouter's `web` plugin caps results rather than searches — but
    it is accepted everywhere and reported by `config`, which is how a setting that
    does not bind here stays visible instead of silently looking like it applied.
    """
    p.add_argument(
        "--web-search",
        dest="web_search",
        action="store_true",
        default=None,
        help="let the commenter look things up while it reads",
    )
    p.add_argument(
        "--no-web-search",
        dest="web_search",
        action="store_false",
        default=None,
        help="review from the document alone, overriding a config that enables search",
    )
    p.add_argument(
        "--web-search-max-uses",
        type=int,
        help="searches per turn (direct Anthropic route only; OpenRouter's web "
        "plugin has no per-request cap)",
    )


def _report(run, pairs, notes, ceiling: int = 80) -> int:
    # `notes` carries rejected proposals and per-comment edits alike, so it is
    # printed as-is rather than labelled "dropped".
    print(f"{len(pairs)} comment(s) ready")
    for why in notes:
        print(f"  {why}")
    if pairs:
        # Counted before the attribution header goes on, so this measures what the
        # model wrote. Length is the thing prompt changes move most, and it was
        # previously only observable by counting comments by hand.
        counts = [len(body.split()) for _, body in pairs]
        # Reported, never enforced: a long comment that survived the editing pass
        # is a regression to notice, not something to silently drop.
        over = [c for c in counts if c > ceiling]
        tail = f" — {len(over)} over the {ceiling}-word ceiling" if over else ""
        print(f"  words: {counts}, median {sorted(counts)[len(counts) // 2]}{tail}")
    if run is None:
        for q, b in pairs:
            print(f"\n  on {q!r}\n     {b}")
        return 0
    for note in getattr(run, "notes", []):
        print(f"  {note}")
    print(f"\nposted {len(run.posted)}/{max(len(pairs), len(run.results))}")
    for r in run.results:
        mark = "✓" if r.ok else "✗"
        if r.kind == "suggestion":
            print(f"  {mark} {r.post_seconds:5.2f}s suggested on {r.quote[:45]!r}")
            print(f"      → {r.replacement[:100] if r.ok else r.error}")
            continue
        print(f"  {mark} {r.post_seconds:5.2f}s on {r.quote[:55]!r}")
        print(f"      {r.body[:100] if r.ok else r.error}")
    # Against results as well as pairs: a comment that failed before it could be
    # posted has a Result and no pair, and comparing only against pairs made a run
    # that lost every comment in the editing stage look like a clean run of zero.
    return 0 if len(run.posted) == max(len(pairs), len(run.results)) else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point. `_main` does the work; this turns a bad tab into a message.

    A LookupError from `_pick_tab` is a user naming a tab that isn't there, or
    naming none on a document with several. Both want the tab listing printed, not
    a traceback with the listing buried in it.
    """
    try:
        return _main(argv)
    except LookupError as e:
        sys.exit(f"marginal: {e}")


def _parser() -> argparse.ArgumentParser:
    """The whole command surface. Its own function so a test can compare commands.

    Parity between the modes is a property of this parser — every command that takes
    part in writing a comment must accept the same settings — and a property nothing
    checks is one that lapses at the next command added.
    """
    ap = argparse.ArgumentParser(prog="marginal")
    ap.add_argument("--config", type=Path, help="TOML config (see marginal.example.toml)")
    ap.add_argument(
        "--credentials",
        type=Path,
        help="deprecated: explicit workspace-MCP-style Google credential JSON",
    )
    ap.add_argument("--account", help="named Marginal Google account")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="authenticate and select named Google accounts")
    p.add_argument("action", nargs="?", choices=("login", "list", "default"), default="login")
    p.add_argument("name", nargs="?", help="account name for login/default")
    p.add_argument("--account", dest="auth_account", help="account name for login")
    p.add_argument("--client", type=Path, help="downloaded Google Desktop OAuth client JSON")
    p.add_argument("--no-browser", action="store_true", help="print the URL and paste its redirect")

    p = sub.add_parser("setup", help="check this machine and write a config that fits it")
    p.add_argument(
        "doc",
        nargs="?",
        help="a document URL. With one, the Google session is proved by exporting it "
        "rather than inferred from a page load",
    )
    p.add_argument("--port", type=int)
    p.add_argument("--profile")
    p.add_argument(
        "--no-write",
        dest="write",
        action="store_false",
        help=f"print the {setup_mod.CONFIG_NAME} this would write, and write nothing",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=f"overwrite an existing {setup_mod.CONFIG_NAME}",
    )
    # Not `--config`, which names a file to *read* and refuses one that does not
    # exist — the exact opposite of what setup does with a path. Overloading it
    # meant `--config new.toml setup` failed at load, before setup could create the
    # file it was being asked to create.
    p.add_argument(
        "--out",
        type=Path,
        help=f"where to write the config (default: ./{setup_mod.CONFIG_NAME})",
    )

    p = sub.add_parser("config", help="print the settings a run here would use")
    _commenting(p, doc=False)
    p.add_argument("--port", type=int)
    p.add_argument("--strategy", choices=("paragraph", "chars"))

    p = sub.add_parser("chrome", help="launch Chrome with the dedicated profile")
    p.add_argument("--port", type=int)
    p.add_argument("--profile")
    p.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="no visible window; sign in headful first and reuse the profile",
    )

    for name in ("read", "list"):
        p = sub.add_parser(name)
        p.add_argument("doc")
        p.add_argument("--tab")
        if name == "list":
            p.add_argument(
                "--reactions",
                action="store_true",
                help="also read emoji reactions, which need the browser",
            )
            p.add_argument("--port", type=int)

    p = sub.add_parser("comment", help="leave comments; `mode` says who writes them")
    _commenting(p)
    _placing(p)

    p = sub.add_parser("review", help="API mode: this tool reads the doc and comments")
    _commenting(p)
    _placing(p)

    for name in ("context", "submit-brief"):
        p = sub.add_parser(name)
        _commenting(p)

    p = sub.add_parser("post-batch", help="agent mode: post {quote, comment} pairs")
    _commenting(p)
    _placing(p)
    p.add_argument("--from", dest="src", required=True, help="JSON file, or - for stdin")
    p.add_argument(
        "--as",
        dest="as_model",
        help="model name for the comment header, e.g. opus-5 (defaults to 'agent')",
    )

    p = sub.add_parser("respond", help="answer replies to our comments")
    p.add_argument("doc")
    p.add_argument("--model")
    p.add_argument("--provider", choices=("auto", "anthropic", "openrouter"))
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("post")
    p.add_argument("doc")
    p.add_argument("-q", "--quote", required=True)
    p.add_argument("-b", "--body", required=True)
    p.add_argument("--tab")
    p.add_argument("--port", type=int)
    p.add_argument("--strategy", choices=("paragraph", "chars"))

    p = sub.add_parser("reply")
    p.add_argument("doc")
    p.add_argument("-c", "--comment-id", required=True)
    p.add_argument("-b", "--body", required=True)

    p = sub.add_parser("unpost")
    p.add_argument("doc")
    p.add_argument("-c", "--comment-id", action="append", required=True)
    p.add_argument("--force", action="store_true", help="also delete ids absent from the ledger")

    return ap


def _main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    # Built once and used twice: `load` resolves the value, `provenance` records
    # which file or flag set it. Two call sites reading one dict, so the printed
    # explanation cannot describe a layering the run did not use.
    #
    # Derived from the dataclass rather than listed by hand. The rule is "a flag named
    # after a setting overrides that setting", which the field names already carry;
    # written out, it was an enumeration to remember, and forgetting it fails
    # silently — the flag parses and the run ignores it. Fields with no flag come
    # through as None, which both consumers discard, so this is the same effective
    # configuration as the list it replaces.
    overrides = {f.name: getattr(a, f.name, None) for f in fields(config_mod.Config)}
    cfg = config_mod.load(a.config, **overrides)

    if a.cmd == "config":
        return _show_config(cfg, config_mod.provenance(a.config, **overrides))

    if a.cmd == "auth":
        try:
            if a.action == "list":
                default = auth.default_account()
                for account in auth.list_accounts():
                    print(f"{'*' if account == default else ' '} {account}")
                return 0
            account = a.auth_account or a.name or cfg.account
            if not account:
                raise auth.AuthError("auth needs --account NAME")
            if a.action == "default":
                auth.set_default(account)
                print(f"default Google account: {account}")
                return 0
            auth.authorize(account, client_path=a.client, no_browser=a.no_browser)
            print(f"authenticated Google account: {account}")
            return 0
        except auth.AuthError as e:
            sys.exit(f"marginal: {e}")

    # Before the document is parsed: `setup` takes an optional URL, and is the one
    # command that has to work on a machine where nothing is configured yet.
    if a.cmd == "setup":
        return setup_mod.run(cfg, a.doc, write=a.write, force=a.force, config_path=a.out)

    if a.cmd == "chrome":
        launch_chrome(cfg.profile, cfg.port, headless=cfg.headless)
        wait_for_port(cfg.port)
        visibility = "headless" if cfg.headless else "visible"
        print(f"Chrome up ({visibility}) on debug port {cfg.port}, profile {cfg.profile}")
        if cfg.headless:
            print("If Google refuses sign-in, launch without --headless once, then reuse it.")
        else:
            print("Sign in to Google in this window once; the session persists.")
        return 0

    try:
        doc_id = gdocs.doc_id_from_url(a.doc)
    except ValueError as e:
        sys.exit(f"marginal: {e}")

    # A pasted URL names a tab; an explicit --tab beats it. Without this the tab in
    # the URL was dropped and the run fell through to whichever tab came first.
    # `hasattr` because the subcommands that need no tab do not define the flag.
    if hasattr(a, "tab") and a.tab is None:
        a.tab = gdocs.tab_from_url(a.doc)

    # `comment` is the mode-neutral name: the config decides which of the two
    # commands it stands for. Agent mode's commenter is the coding agent calling
    # this, so the tool's turn ends with the brief — there is nothing further it
    # could do without taking the writing away from the agent. Resolved here,
    # before anything branches on `a.cmd`.
    if a.cmd == "comment":
        a.cmd = "review" if cfg.mode == "api" else "context"
    elif a.cmd in _MODE_OF and _MODE_OF[a.cmd] != cfg.mode:
        # Not an error: comparing the two modes on one document means running both
        # against one config, and a tool that refused would have you edit the file
        # between the two halves of the comparison. Said out loud so that a run is
        # never quietly attributed to the mode the config names.
        print(
            f"note: mode = {cfg.mode!r}, running {_MODE_OF[a.cmd]} mode because "
            f"`{a.cmd}` was named explicitly",
            file=sys.stderr,
        )

    # Only mint a Google token for commands that actually reach the Drive API. The
    # browser source exists so a user can run with no Google credentials at all;
    # minting one up front here made that path fail before it started.
    #
    # Which commands those are is a question about the source, not about the mode.
    # This used to name `context` and `post-batch` — the two agent-mode commands —
    # so `review` demanded credentials under `source = "browser"` that agent mode
    # did not, even though it reads through the browser and verifies through
    # `browser_reader` exactly as agent mode does. The commands below reach Drive
    # whatever the source: they read the comment list, or write to it.
    reads_drive = a.cmd in ("read", "list", "reply", "unpost", "respond", "post")
    reads_drive = reads_drive or cfg.source != "browser"
    try:
        token = (
            gdocs.access_token(cfg.credentials, account=cfg.account) if reads_drive else None
        )
    except (FileNotFoundError, auth.AuthError) as e:
        # Configuration, not a bug. A traceback here buries the one line that says
        # what to set, which is the whole message a new user needs.
        sys.exit(f"marginal: {e}")

    if a.cmd == "read":
        doc = gdocs.read_doc(doc_id, token)
        tab = pick_tab(doc, a.tab)
        print(f"# {doc['title']} — tab {tab['id']} ({len(tab['text'])} chars)\n")
        print(tab["text"])
        return 0

    if a.cmd == "context":
        print(context(doc_id, token, cfg, a.tab, a.focus))
        return 0

    if a.cmd == "submit-brief":
        print(submit_brief(doc_id, token, cfg, a.tab))
        return 0

    if a.cmd == "list":
        comments = gdocs.list_comments(doc_id, token)
        unmatched: list[str] = []
        if a.reactions:
            # Opt-in because it costs a browser: reactions are the one thing on a
            # comment the Drive API cannot see, so reading them means the docx
            # export through the signed-in Chrome.
            page = open_doc(doc_id, a.tab, port=cfg.port)
            try:
                unmatched = reactions.attach(comments, reactions.from_export(page, doc_id))
            finally:
                page.close()
        for c in comments:
            mark = "✓" if c.get("resolved") else " "
            quoted = (c.get("quotedFileContent") or {}).get("value", "")
            print(f"[{mark}] {c['id']}  {c['author']['displayName']}{_reacts(c)}")
            print(f"     on: {quoted[:70]!r}")
            print(f"     {c['content'][:100]}")
            for r in c.get("replies", []):
                print(f"       ↳ {r['author']['displayName']}: {r['content'][:80]}{_reacts(r)}")
        for text in unmatched:
            # A reaction visible in the document and absent from this output is the
            # failure worth naming; silence would read as "there were none".
            print(f"\n! reaction on an unmatched card: {text!r}")
        return 0

    if a.cmd == "reply":
        r = gdocs.create_reply(doc_id, a.comment_id, a.body, token)
        print(f"replied: {r.get('id')}")
        return 0

    if a.cmd == "unpost":
        deleted = unpost(doc_id, a.comment_id, token, a.force)
        print(f"deleted {len(deleted)} comment(s)")
        return 0

    if a.cmd == "review":
        return _report(*review(doc_id, token, cfg, a.tab, cfg.comments, a.focus, a.dry_run),
                       ceiling=cfg.word_ceiling)

    if a.cmd == "post-batch":
        raw = sys.stdin.read() if a.src == "-" else Path(a.src).read_text()
        items = json.loads(raw)
        if not isinstance(items, list):
            sys.exit("expected a JSON array of {quote, comment} objects")
        return _report(
            *post_batch(doc_id, token, cfg, items, a.tab, a.dry_run, a.as_model),
            ceiling=cfg.word_ceiling,
        )

    if a.cmd == "respond":
        items, usage = respond(doc_id, token, cfg, a.dry_run)
        if not items:
            print("no threads awaiting a reply")
        for it in items:
            print(f"\non {it['on'][:60]!r}\n  {it['reply']}")
        if items:
            # Same line the review path prints, for the same reason: a dead cache
            # breakpoint raises no error and returns the same replies — it just
            # pays for the document once per thread. A zero cache read across
            # several answered threads is the only symptom it has.
            print(f"\ntokens: {usage.line()}")
        return 0

    if a.cmd == "post":
        doc = gdocs.read_doc(doc_id, token)
        tab = pick_tab(doc, a.tab)
        page = open_doc(doc_id, tab["id"], port=cfg.port)
        try:
            run = post_many(page, doc_id, tab, [(a.quote, a.body)], token, cfg.strategy)
        finally:
            page.close()
        for r in run.posted:
            ledger.record(doc_id, r.comment_id, r.quote, r.body, "manual")
        print(json.dumps(run.as_dict(), indent=2))
        return 0 if run.posted else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
