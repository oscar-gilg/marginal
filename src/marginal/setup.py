"""The first command a new user runs, and the one that decides what they need.

Most of what a full Marginal install can do is optional, and the README used
to open with the part that is not required at all: a Google Cloud project, a
consent screen, a Desktop OAuth client. That is the friction wall. Only two things
are actually necessary — a Chrome, and a Google session inside it — and everything
else is an upgrade that buys something nameable.

So this command checks what is present rather than demanding what is missing, and
writes the configuration those findings imply:

    Google OAuth?     no -> source  = "browser"   read and verify through Chrome
    a model API key?  no -> critic  = false       nothing here calls a model

Agent mode — a coding agent writes the comments and this tool places them — is the
default whether or not a key is present, so `mode` is no longer setup's decision;
a key buys the shortening pass and the *option* of `mode = "api"`.

That critic line is the one place a missing key costs something real, and it is
said out loud rather than discovered later. `critic` is the shortening pass, and it runs
inside `post-batch` — on this tool's side, not the agent's — precisely so that an
agent taking a shortcut cannot skip it. With no key there is nothing to run it
with, so comments post at whatever length the agent wrote them.

**Signing in cannot be automated and is not pretended otherwise.** Google refuses
sign-in often enough in automation-launched browsers that a setup command claiming
success on a launched-but-signed-out profile would be the worst kind of wrong. The
check is therefore an operation the tool actually needs: with a document URL, a
real export through the browser's own session, which is exactly what `source =
"browser"` does on every run. Without one, the weaker check is landing on
docs.google.com rather than a sign-in page, and it says which one it made.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import auth, browser_source, gdocs, model
from .cdp import Page, launch_chrome, wait_for_port
from .config import Config

# `cdp.launch_chrome` hardcodes this, and the selection path sends Cmd-modified
# keys, so the tool is macOS-only in practice. Named here as well so the failure is
# "no Chrome at this path" during setup rather than a Popen error mid-run.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CONFIG_NAME = "marginal.toml"


@dataclass
class Check:
    """One thing that is either true of this machine or not.

    `fix` is what to do about it, and is printed only when `ok` is false — a
    checklist that explains how to fix things that are already working is one
    nobody reads to the bottom of.
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""


def chrome_installed() -> Check:
    found = Path(CHROME).exists()
    return Check(
        "Chrome",
        found,
        CHROME if found else "not found",
        fix="Install Google Chrome. Marginal drives the real Docs editor, which "
        "is the only route that anchors a comment without the Workspace Developer "
        "Preview.",
    )


def chrome_running(port: int) -> bool:
    """Whether a debuggable Chrome is already answering on the port."""
    try:
        wait_for_port(port, timeout=1.0)
        return True
    except TimeoutError:
        return False


def start_chrome(cfg: Config, timeout: float = 30.0) -> Check:
    """Bring up the dedicated profile, or report the one already there.

    Headful unconditionally, whatever the config says. This command exists to get a
    profile signed in, and headless sign-in is the specific thing that fails — a
    `headless = true` config would make setup launch the one browser the user cannot
    complete the step in.
    """
    if chrome_running(cfg.port):
        return Check("Chrome profile", True, f"already running on port {cfg.port}")
    launch_chrome(cfg.profile, port=cfg.port, url="https://docs.google.com/", headless=False)
    try:
        wait_for_port(cfg.port, timeout=timeout)
    except TimeoutError:
        return Check(
            "Chrome profile",
            False,
            f"launched, but nothing answered on port {cfg.port} within {timeout:.0f}s",
            fix=f"Another process may hold port {cfg.port}. Set a different `port` in "
            f"{CONFIG_NAME}, or quit whatever is using it.",
        )
    return Check("Chrome profile", True, f"launched with profile {cfg.profile!r}")


def _signed_in_shallow(port: int) -> Check:
    """Did docs.google.com load, or did it bounce to a sign-in page?

    The weak check, used when no document was named. It proves a session exists; it
    does not prove that session can read anything. Said in the detail, because a
    tick with no caveat is how someone concludes they are set up and then watches
    the first real run fail.
    """
    page = None
    try:
        page = Page.open("https://docs.google.com/document/u/0/", port=port)
        # `wait_until` returns False on timeout rather than raising. Ignoring it
        # meant a page that never finished loading was read for its URL anyway —
        # and mid-navigation that URL is still the one we asked for, so a stalled
        # load produced a tick. A timeout means the answer is unknown, and unknown
        # is reported as a failure here rather than resolved in our own favour.
        loaded = page.wait_until("document.readyState === 'complete'", timeout=20)
        here = page.eval("location.href") or ""
    except Exception as e:
        return Check(
            "Google session",
            False,
            f"could not reach docs.google.com: {type(e).__name__}: {e}",
            fix="Check the browser window that just opened.",
        )
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    if not loaded:
        return Check(
            "Google session",
            False,
            "docs.google.com did not finish loading within 20s, so nothing is proved",
            fix="Retry. If it keeps stalling, open the Chrome window and see what it "
            "is showing — a consent screen or an account chooser will sit there "
            "indefinitely.",
        )
    if "accounts.google.com" in here:
        return Check(
            "Google session",
            False,
            "docs.google.com redirected to a sign-in page",
            fix=f"Sign in to Google in the Chrome window that just opened, then run "
            f"`{invocation()} setup` again. This profile keeps its session, so "
            f"this is a one-time step.",
        )
    return Check(
        "Google session",
        True,
        "signed in (not verified against a document — pass a doc URL to prove it)",
    )


def _signed_in_proven(port: int, doc_url: str) -> Check:
    """Export the named document through the browser's own session.

    The real check: this is the operation `source = "browser"` performs on every
    run, so passing it means the credential-free path works rather than merely
    looking like it should.
    """
    try:
        doc_id = gdocs.doc_id_from_url(doc_url)
    except Exception as e:
        return Check("Google session", False, f"not a Google Docs URL: {e}",
                     fix="Pass the URL from the browser's address bar.")
    page = None
    try:
        page = Page.open("about:blank", port=port)
        blob = browser_source.export(page, doc_id, "txt", tab_id=gdocs.tab_from_url(doc_url))
    except Exception as e:
        return Check(
            "Google session",
            False,
            f"could not export the document: {type(e).__name__}: {e}",
            fix="Sign in to Google in the Chrome window that just opened, with an "
            "account that can open this document, then run `marginal setup` "
            "again.",
        )
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        # The export cache is keyed per run and this document will be read again in
        # a moment by a real command; leaving setup's copy in it would serve bytes
        # fetched before the user did anything else.
        browser_source.invalidate(doc_id)
    return Check(
        "Google session",
        True,
        f"exported {len(blob)} characters from the document you named",
    )


def google_session(port: int, doc_url: str | None) -> Check:
    return _signed_in_proven(port, doc_url) if doc_url else _signed_in_shallow(port)


def model_keys() -> tuple[Check, list[str]]:
    """Which model routes are reachable. Both are optional; neither is required."""
    model.load_env()
    found = [n for n in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY") if os.environ.get(n)]
    if found:
        return Check("Model API key", True, ", ".join(found)), found
    return (
        Check(
            "Model API key",
            False,
            "none set — optional",
            fix="Without one, a coding agent writes the comments and nothing here "
            "calls a model. Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY in a `.env` "
            "to let this tool write them itself, and to run the shortening pass.",
        ),
        [],
    )


def google_accounts(account: str | None = None) -> tuple[Check, list[str]]:
    """Google OAuth accounts this tool owns. Optional: the browser reads without them.

    The question is not "are there token files" but "will the next command pick an
    account", and those differ: two accounts with no default is a directory full of
    tokens and an `AuthError` on every run. So the check is `select_account`, the
    same function the real path calls, rather than a directory listing. Presence
    without selectability used to pass here and write `source = "api"`, which is the
    Ready-then-fail shape this command exists to prevent.

    `list_accounts` returns an empty list on a machine that has never authenticated,
    so nothing there is caught — a failure listing them is a real problem and must
    not read as "no accounts", which would silently write `source = "browser"` over
    a working credentialed setup.
    """
    accounts = auth.list_accounts()
    if accounts:
        try:
            selected = auth.select_account(account)
        except auth.AuthError as e:
            # Not "no accounts": these exist and one of them is probably wanted, so
            # the fix is to name one rather than to authenticate again. Treated as
            # unusable for the config, because it is.
            return (
                Check(
                    "Google OAuth",
                    False,
                    f"{len(accounts)} account(s), none selectable: {e}",
                    fix="Set one with `marginal auth default NAME`, or put "
                    "`account = \"NAME\"` in the config. Until then the document is "
                    "read through the browser session instead.",
                ),
                [],
            )
        shown = ", ".join(f"{a}{' (selected)' if a == selected else ''}" for a in accounts)
        return Check("Google OAuth", True, shown), accounts
    return (
        Check(
            "Google OAuth",
            False,
            "no accounts — optional",
            fix="Without it, the document is read and verified through the browser "
            "session instead, and `list`, `reply`, `respond` and `unpost` are "
            "unavailable. `" + invocation() + " auth --client CLIENT.json --account you" +
            "@example.com` adds it in one "
            "step — this build ships an OAuth client, so there is no Google Cloud "
            "project to create.",
        ),
        [],
    )


def preset(keys: list[str], accounts: list[str]) -> dict[str, object]:
    """The settings these findings imply, and only the ones that differ from default.

    A config file that restates the defaults is one nobody can read for what is
    unusual about this machine.
    """
    settings: dict[str, object] = {}
    if not accounts:
        settings["source"] = "browser"
    if not keys:
        # `mode = "agent"` is already the default, so only the critic needs saying:
        # it runs inside `post-batch` and would otherwise fail on every comment at
        # the point it tries to tighten one.
        settings["critic"] = False
    return settings


# Why each non-default setting is there, written into the file it is written to. A
# generated config whose lines have no reason attached is one that gets copied
# forward long after the reason expired.
_WHY = {
    "source": "no Google OAuth here, so the document is read through the browser's "
    "own session. `marginal auth ...` and this can go back to \"api\".",
    "critic": "the shortening pass calls a model, and there is no key to call one "
    "with. Comments post at the length they were written.",
}


def render(settings: dict[str, object]) -> str:
    """The config file, as text."""
    lines = [
        "# Written by `marginal setup`. Every setting here differs from the",
        "# default; see marginal.example.toml for the full list and what each",
        "# one means. `marginal config` prints what a run here would use.",
    ]
    if not settings:
        lines += [
            "",
            "# Nothing to override: this machine has both a model API key and a",
            "# Google account, so the defaults are already the fast path.",
        ]
        return "\n".join(lines) + "\n"
    for name, value in settings.items():
        rendered = str(value).lower() if isinstance(value, bool) else f'"{value}"'
        # Wrapped, because these reasons are the point of the file and an unwrapped
        # one runs off the side of the editor it is read in.
        lines += [""] + [f"# {ln.strip()}" for ln in _wrap(_WHY[name], 0, width=74)]
        lines.append(f"{name} = {rendered}")
    return "\n".join(lines) + "\n"


def write_config(path: Path, settings: dict[str, object], force: bool) -> Check:
    """Write the config, refusing to overwrite one that already exists.

    A setup command that clobbers a hand-tuned config is worse than one that does
    nothing, so an existing file is reported with what would have been written and
    left alone. `--force` is the way to say it anyway.
    """
    if path.exists() and not force:
        return Check(
            f"{path}",
            True,
            "already exists, left alone",
            fix="",
        )
    path.write_text(render(settings))
    return Check(f"{path}", True, "written")


def run(
    cfg: Config,
    doc_url: str | None = None,
    write: bool = True,
    force: bool = False,
    config_path: Path | None = None,
) -> int:
    """Check everything, report it, write the config those findings imply.

    Returns non-zero only when something *required* is missing — Chrome, or a
    signed-in session. A missing key and a missing OAuth client are choices, and
    exiting non-zero on a choice makes this command useless in a script that is
    deliberately taking the credential-free path.
    """
    checks = [chrome_installed()]
    if not checks[0].ok:
        _report(checks)
        return 1

    checks.append(start_chrome(cfg))
    if not checks[-1].ok:
        _report(checks)
        return 1

    checks.append(google_session(cfg.port, doc_url))
    signed_in = checks[-1].ok

    key_check, keys = model_keys()
    account_check, accounts = google_accounts(cfg.account)
    checks += [key_check, account_check]

    _report(checks)

    settings = preset(keys, accounts)
    path = config_path or Path(CONFIG_NAME)
    print()
    if write:
        result = write_config(path, settings, force)
        print(f"  {result.name}: {result.detail}")
        if path.exists() and not force and result.detail.startswith("already"):
            print("\n  what would have been written:\n")
            for line in render(settings).splitlines():
                print(f"    {line}")
    else:
        print("  config not written (--no-write). What it would say:\n")
        for line in render(settings).splitlines():
            print(f"    {line}")

    print()
    if not signed_in:
        print(f"Sign in to Google in the Chrome window, then run `{invocation()} setup` again.")
        return 1
    print(_next_step(doc_url, keys, accounts))
    return 0


# The commands that mint a Google token whatever `source` says. `reply`, `unpost`
# and `respond` write to the comment list, which has no browser route at all.
# Listed here so the one thing a credential-free install cannot do is said at setup
# time, rather than found later when a reply fails.
NEEDS_OAUTH = ("read", "list", "post", "reply", "unpost", "respond")


def _next_step(doc_url: str | None, keys: list[str], accounts: list[str]) -> str:
    doc = doc_url or "<doc-url>"
    first = (
        f"Ready, in agent mode. Ask your coding agent to run:\n"
        f"  {invocation()} comment {doc} -n 3      # prints the brief and the document\n"
        f"and to post what it writes back with `{invocation()} post-batch`."
    )
    if keys:
        first += (
            f"\nOr have this tool call a model itself:  {invocation()} review {doc} -n 3"
        )
    if accounts:
        # Already authenticated, so the long explanation is behind them. One line,
        # because "you have a token" is not the same as "you knowingly chose whose
        # project it is on" — someone who authenticated before this shipped never
        # saw the question at all.
        # The one thing worth saying to someone whose OAuth already works, because
        # it is invisible until a thread has two names in it: a comment is posted
        # through the browser and a reply through the API, so the two identities are
        # the Chrome profile's and the token's. Different accounts means threads
        # that open as one person and answer as another, on a document other people
        # are reading.
        identity = "\n\n" + "\n".join(
            _wrap(
                f"Comments post as whichever account this Chrome profile is signed "
                f"into; replies post as {accounts[0]}. Sign the profile into the "
                f"same account, or threads will open as one person and answer as "
                f"another.",
                2,
            )
        )
        if _bundled_client_in_use():
            return (
                f"{first}\n\nAuthenticated through the OAuth client shipped with "
                f"marginal. `marginal config` shows it; `marginal auth --client` "
                f"switches to your own.{identity}"
            )
        return first + identity
    lines = [
        first,
        "",
        f"Without Google OAuth, these still need a token and will exit asking for "
        f"one: {', '.join(NEEDS_OAUTH)}. Commenting works; answering the replies "
        f"does not.",
        "",
    ]
    # Said here rather than left to the plugin, because most of the value of
    # knowing it goes to whoever is *not* being walked through this by an agent.
    # Which project a user's documents are reached through is a choice, and a
    # default nobody was shown is not one.
    if not auth.client_source():
        candidates = find_client_files()
        if candidates:
            # The file is usually already on disk — just handed over, or downloaded
            # from the console minutes ago. Naming it turns "supply a client" into
            # a line to copy.
            lines += [
                f"  {invocation()} auth --client {candidates[0]} --account you@example.com",
                "",
            ]
            if len(candidates) > 1:
                lines += _wrap(
                    f"({len(candidates)} client files found; the newest is used above. "
                    f"The project number is in the filename — check it is the one you "
                    f"meant, because an old download authenticates against a project "
                    f"you have forgotten and fails without mentioning it.)",
                    2,
                ) + [""]
        else:
            lines += [
                f"  {invocation()} auth --client /path/to/client_secret.json --account you@example.com",
                "",
            ]
        lines += _wrap(
            "A Google OAuth client identifies the application to Google, and none "
            "ships with marginal. Three ways to have one: you were sent a "
            "client_secret JSON along with this tool, in which case that is the "
            "file; you ask whoever gave you marginal to send theirs, which costs "
            "them nothing and is the usual answer; or /marginal:oauth walks through "
            "making your own, which takes about ten minutes and is yours to keep.",
            2,
        )
    elif _bundled_client_in_use():
        lines += _wrap(
            "That uses the OAuth client shipped with marginal, so there is no "
            "Google Cloud project to create — you authenticate through the "
            "project that ships with the tool rather than one you own. That is "
            "fine, and the token stays on this machine. Setting up your own takes "
            "about ten minutes in the Google Cloud console and means your API "
            "quota is not shared, the 100-user cap on the shipped client does not "
            "apply, and a verification demand against it cannot stop your setup "
            "working. See `marginal auth --client`, or the /marginal:oauth skill.",
            2,
        )
    return "\n".join(lines)


def invocation() -> str:
    """How to spell this program in a command a user can paste.

    `uvx marginal` installs nothing, which is the point of recommending it — and
    means `marginal` is then not on PATH. Printing bare `marginal auth ...` to
    somebody who got here that way hands them a command that fails with
    "command not found", which reads as a broken tool rather than a missing install.
    """
    return "marginal" if shutil.which("marginal") else "uvx marginal"


def _bundled_client_in_use() -> bool:
    """Whether a run right now would authenticate through the shipped client."""
    found = auth.client_source()
    return found is not None and found[1] == "bundled"


# Where a client file plausibly is, in the order it is worth looking. `private/` is
# the convention for keeping one beside a checkout; `~/Downloads` is where a browser
# puts it and where it stays.
CLIENT_HUNTING_GROUND = ("private", ".", "~/Downloads")


def find_client_files() -> list[Path]:
    """Client JSONs lying around, most recently written first.

    Setup knows a client is needed and the user has usually just been handed one, so
    telling them to "supply a client" while the file sits in Downloads is a step
    they have to do and this command could have done. Named rather than installed:
    picking one for somebody, out of several, is how the wrong project gets used.
    """
    found: list[Path] = []
    for where in CLIENT_HUNTING_GROUND:
        directory = Path(where).expanduser()
        if not directory.is_dir():
            continue
        for pattern in ("client_secret*.json", "oauth-client.json"):
            found += [p for p in directory.glob(pattern) if p.is_file()]
    # Deduplicated by resolved path, because `.` and an absolute path can be the
    # same file, and offering it twice looks like there is a choice to make.
    seen, unique = set(), []
    for p in sorted(found, key=lambda p: -p.stat().st_mtime):
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _report(checks: list[Check]) -> None:
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {'✓' if c.ok else '✗'} {c.name:<{width}}  {c.detail}")
        if not c.ok and c.fix:
            for line in _wrap(c.fix, width + 6):
                print(line)


def _wrap(text: str, indent: int, width: int = 78) -> list[str]:
    pad = " " * indent
    out, line = [], pad
    for word in text.split():
        if len(line) + len(word) + 1 > width and line != pad:
            out.append(line)
            line = pad
        line += ("" if line == pad else " ") + word
    if line != pad:
        out.append(line)
    return out
