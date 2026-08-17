"""Owned Google OAuth, named accounts, and private token storage.

Marginal deliberately uses the OAuth endpoints directly.  The rest of the
Google integration is a small ``urllib`` client, so authentication should not pull
in the much larger Google API client stack merely to refresh one token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


CONFIG_DIR = Path.home() / ".config/marginal"
CLIENT_PATH = CONFIG_DIR / "oauth-client.json"
# The client shipped with the package, if this build has one. Inside the package
# directory, so ordinary package selection carries it into both the wheel and the
# sdist — no force-include, unlike the prompts, which live outside `src/`. Absent
# is a supported state: `_client` then says how to supply one.
BUNDLED_CLIENT = Path(__file__).resolve().parent / "oauth-client.json"
AUTH_PATH = CONFIG_DIR / "auth.json"
ACCOUNTS_DIR = CONFIG_DIR / "accounts"

SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
)
TIMEOUT = 60
# How long to wait for Google to redirect back. Much longer than an HTTP timeout,
# because what happens in between is a person reading screens: an account chooser,
# the "Google hasn't verified this app" interstitial with its Advanced → Go to
# (unsafe) bypass, and a consent page listing two scopes. Sixty seconds is not
# enough for a first-time consent, and running out looks identical to the flow
# failing — the message says the callback timed out, which reads as a broken tool
# rather than "you were still clicking".
CALLBACK_TIMEOUT = 300
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


class AuthError(RuntimeError):
    """Authentication is missing, corrupt, or no longer refreshable."""


def _validate_account(account: str) -> str:
    if account in (".", "..") or not account or not _ACCOUNT_RE.fullmatch(account):
        raise AuthError(
            f"invalid account name {account!r}; use only letters, numbers, '.', '_', "
            "'@', and '-'"
        )
    return account


def _token_path(account: str) -> Path:
    return ACCOUNTS_DIR / _validate_account(account) / "token.json"


def _private_dirs(path: Path) -> list[Path]:
    """Every directory that must be 0700 for `path` to be private.

    A token is only as private as the least private directory above it, so this
    walks from `CONFIG_DIR` down to the file's own parent. A path outside
    `CONFIG_DIR` gets only its own parent — this owns its own store and does not
    reach up and tighten permissions on directories it did not create.
    """
    try:
        relative = path.parent.relative_to(CONFIG_DIR)
    except ValueError:
        return [path.parent]
    out, current = [CONFIG_DIR], CONFIG_DIR
    for part in relative.parts:
        current = current / part
        out.append(current)
    return out


def _write_private(path: Path, content: str) -> None:
    """Atomically replace `path` with a file that is private from creation.

    Three properties, each of which has a way of being lost:

    * **Private from creation, not after it.** `mkstemp` opens at 0600 and the
      mode is set on the descriptor, so there is no window in which the token
      exists world-readable. Writing then chmod-ing leaves exactly that window.
    * **Atomic.** `os.replace` is atomic within a filesystem, so a reader sees the
      old file or the new one and never a half-written one. The temporary file is
      created in the destination's own directory to keep the rename within one
      filesystem.
    * **Safe against a second writer.** The temporary name is unique rather than a
      predictable `.tmp`, so two processes refreshing a token at once cannot write
      through each other.

    On any failure the temporary file is removed rather than left behind holding
    credentials under a name nothing will look at again.
    """
    for directory in _private_dirs(path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    # `fdopen` takes ownership of the descriptor, and the `with` closes it — so
    # after it is handed over, closing here would be a *double* close. That is
    # usually a caught EBADF, but between the two closes the number can be reused,
    # and then the second close shuts an unrelated file in another thread. Tracking
    # ownership is what makes the difference; the previous version claimed to and
    # did not.
    owned = True
    try:
        os.fchmod(fd, 0o600)
        fh = os.fdopen(fd, "w")
        owned = False
        with fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if owned:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        raise AuthError(f"{label} not found: {path}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"{label} is unreadable or corrupt: {path}: {e}") from e
    if not isinstance(value, dict):
        raise AuthError(f"{label} must contain a JSON object: {path}")
    return value


def _installed(raw: dict, where: str = "") -> dict:
    """The `installed` block of a Desktop OAuth client, or a refusal naming what is wrong.

    One validator for both gates. `install_client` checks a file the user has just
    downloaded and `_client` checks the copy in our store; a client accepted at
    install time and rejected at use time is the confusing half of that failure, and
    two copies of the field list is how that happens.
    """
    installed = raw.get("installed")
    required = ("client_id", "client_secret", "auth_uri", "token_uri")
    if not isinstance(installed, dict) or any(
        not isinstance(installed.get(k), str) or not installed[k] for k in required
    ):
        raise AuthError(
            f"OAuth client{where} must be a Google Desktop app JSON with an "
            f"'installed' object containing {', '.join(required)}"
        )
    # Where the endpoints point is not cosmetic: `auth_uri` is where the user is
    # sent to approve, and `token_uri` is where this process posts the client
    # secret, the authorization code and the PKCE verifier. A client file naming
    # somewhere else turns `--client` into "hand your Google session to whoever
    # wrote this JSON", and the file may have come from a gist or a chat message.
    for field in ("auth_uri", "token_uri"):
        parsed = urllib.parse.urlparse(installed[field])
        host = parsed.hostname or ""
        # Both domains, because Google uses both and a rule that allows only the
        # obvious one rejects Google's own client: `accounts.google.com` issues the
        # authorization, `oauth2.googleapis.com` mints the token.
        google = any(
            host == d or host.endswith("." + d) for d in ("google.com", "googleapis.com")
        )
        if parsed.scheme != "https" or not google:
            raise AuthError(
                f"OAuth client{where} has a {field} of {installed[field]!r}, which is "
                f"not an https Google endpoint. Refusing to use it: that is where your "
                f"authorization and this client's secret would be sent."
            )
    return installed


def client_source() -> tuple[Path, str] | None:
    """Which OAuth client a run would use, and what kind it is.

    Two places, most specific first:

    1. `~/.config/marginal/oauth-client.json` — installed by `auth --client`, and
       the one to use to run on your own Google Cloud project.
    2. the copy shipped inside the package — so that a new user runs one command
       instead of creating a project, enabling two APIs and configuring a consent
       screen before they can try anything.

    Returned rather than just opened, because "whose project am I on" is a question
    worth being able to answer directly. A shipped client means a user's documents
    are reached through *somebody else's* Cloud project, and that should never have
    to be inferred from behaviour.

    A desktop client's `client_secret` is not a secret — it ships in every copy, by
    construction, which is why the flow uses PKCE and why the redirect is confined
    to loopback. See RFC 8252.
    """
    if CLIENT_PATH.is_file():
        return CLIENT_PATH, "your own"
    if BUNDLED_CLIENT.is_file():
        return BUNDLED_CLIENT, "bundled"
    return None


def _client() -> dict:
    found = client_source()
    if found is None:
        raise AuthError(
            "no OAuth client available. This build ships without one, so point "
            "marginal at your own: create a Desktop OAuth client in a Google Cloud "
            "project with the Docs and Drive APIs enabled, then run "
            "`marginal auth --client /path/to/client_secret.json --account NAME`."
        )
    path, kind = found
    return _installed(_read_json(path, "OAuth client"), f" {path} ({kind})")


def install_client(path: Path) -> None:
    """Validate and copy a downloaded Desktop OAuth client into our store.

    Installing takes precedence over the bundled one from then on, permanently and
    without a flag to remember: the file's presence *is* the preference.
    """
    raw = _read_json(Path(path).expanduser(), "OAuth client")
    _installed(raw)
    _write_private(CLIENT_PATH, json.dumps(raw, indent=2) + "\n")


def list_accounts() -> list[str]:
    if not ACCOUNTS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in ACCOUNTS_DIR.iterdir() if p.is_dir() and (p / "token.json").is_file()
    )


def default_account() -> str | None:
    if not AUTH_PATH.exists():
        return None
    raw = _read_json(AUTH_PATH, "OAuth account selection")
    account = raw.get("default_account")
    if not isinstance(account, str):
        raise AuthError(f"OAuth account selection has no string default_account: {AUTH_PATH}")
    return _validate_account(account)


def select_account(account: str | None = None) -> str:
    """Choose an owned account without ever guessing among several."""
    if account is not None:
        selected = _validate_account(account)
        if not _token_path(selected).is_file():
            raise AuthError(
                f"no authentication for {selected!r}; run: "
                f"marginal auth --account {selected}"
            )
        return selected
    default = default_account()
    if default is not None:
        if not _token_path(default).is_file():
            raise AuthError(
                f"default account {default!r} has no token; run: "
                f"marginal auth --account {default}"
            )
        return default
    accounts = list_accounts()
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise AuthError(
            "not authenticated with Google; run: marginal auth --account NAME"
        )
    raise AuthError(
        "more than one Google account is authenticated; pass --account NAME or set one "
        f"with `marginal auth default NAME` (available: {', '.join(accounts)})"
    )


def set_default(account: str) -> None:
    account = _validate_account(account)
    if not _token_path(account).is_file():
        raise AuthError(f"no authentication for {account!r}")
    _write_private(AUTH_PATH, json.dumps({"default_account": account}, indent=2) + "\n")


def _post_form(url: str, fields: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise AuthError(f"Google OAuth returned HTTP {e.code}: {_why(e.read())}") from e
    except (OSError, urllib.error.URLError) as e:
        raise AuthError(f"could not reach Google OAuth: {e}") from e
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AuthError("Google OAuth returned invalid JSON") from e
    if not isinstance(value, dict) or value.get("error"):
        raise AuthError(f"Google OAuth failed: {_why(value)}")
    return value


def _why(payload) -> str:
    """The reason out of an OAuth error, without the rest of the response.

    This runs on the endpoint that mints tokens, and its output goes to a terminal
    and from there into bug reports and pasted logs. Echoing a whole response body
    means one malformed or unexpected reply puts a refresh token somewhere it will
    outlive the process. Only the two fields the spec defines for saying what went
    wrong come out; anything else is reported as its shape.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"<{len(payload)} bytes, not JSON>"
    if not isinstance(payload, dict):
        return f"<{type(payload).__name__}>"
    error = payload.get("error")
    if isinstance(error, dict):  # the Google-API shape rather than the OAuth one
        error = error.get("status") or error.get("message")
    described = payload.get("error_description")
    out = " — ".join(str(p) for p in (error, described) if p)
    return out or f"<no error field; keys: {sorted(payload)}>"


def access_token(account: str | None = None) -> str:
    """Return a valid token for an owned account, refreshing and saving if needed."""
    selected = select_account(account)
    path = _token_path(selected)
    token = _read_json(path, f"OAuth token for {selected}")
    cached = token.get("access_token")
    expires_at = token.get("expires_at")
    if isinstance(cached, str) and isinstance(expires_at, (int, float)):
        if expires_at > time.time() + 60:
            return cached
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthError(
            f"authentication for {selected} has no refresh token; run: "
            f"marginal auth --account {selected}"
        )
    client = _client()
    try:
        refreshed = _post_form(
            client["token_uri"],
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except AuthError as e:
        raise AuthError(
            f"authentication for {selected} expired or was revoked; run: "
            f"marginal auth --account {selected} ({e})"
        ) from e
    value = refreshed.get("access_token")
    if not isinstance(value, str) or not value:
        raise AuthError(f"Google OAuth refresh for {selected} returned no access_token")
    token.update(refreshed)
    # Google normally omits this on refresh, but OAuth permits rotation. Preserve
    # the old token only when no replacement was returned; overwriting a rotated
    # token with the old one makes the next command fail after this one succeeded.
    token["refresh_token"] = refreshed.get("refresh_token") or refresh_token
    token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600))
    _write_private(path, json.dumps(token, indent=2) + "\n")
    return value


def legacy_access_token(path: Path) -> str:
    """Refresh an explicitly named workspace-MCP-style credential JSON."""
    raw = _read_json(Path(path), "legacy Google credential")
    required = ("client_id", "client_secret", "refresh_token", "token_uri")
    if any(not raw.get(key) for key in required):
        raise AuthError(f"legacy Google credential is missing one of {required}: {path}")
    value = _post_form(
        raw["token_uri"],
        {
            "client_id": raw["client_id"],
            "client_secret": raw["client_secret"],
            "refresh_token": raw["refresh_token"],
            "grant_type": "refresh_token",
        },
    ).get("access_token")
    if not isinstance(value, str) or not value:
        raise AuthError("legacy Google credential refresh returned no access_token")
    return value


def _callback_values(url: str, state: str) -> str:
    values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if values.get("state", [None])[0] != state:
        raise AuthError("OAuth callback state did not match; authentication was refused")
    if values.get("error"):
        raise AuthError(f"Google authorization failed: {values['error'][0]}")
    code = values.get("code", [None])[0]
    if not code:
        raise AuthError("Google authorization callback contained no code")
    return code


def authenticated_identity(access_token: str) -> str | None:
    """Which Google account a token actually belongs to, or None if unanswerable.

    Asked of Drive, because the Drive scope is already granted and no extra one has
    to be requested to find out who is holding the token.
    """
    req = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
        headers={"authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            return json.loads(response.read()).get("user", {}).get("emailAddress")
    except Exception:
        # An identity we could not check is not an identity that failed the check.
        # Refusing here would turn a transient Drive error into a failed login.
        return None


def _refuse_a_mismatched_identity(account: str, access_token: str | None) -> None:
    """Stop a token being filed under an account it does not belong to.

    `login_hint` only *suggests* an account to Google. Someone signed into two
    accounts can pick the other one at the chooser, and the token would then be
    stored under the name that was asked for rather than the one that was granted.
    Nothing downstream would notice: comments would post and replies would come back
    as a different person, which on a shared document is the kind of mistake that
    cannot be taken back.
    """
    if not access_token:
        return
    actual = authenticated_identity(access_token)
    if actual and actual.lower() != account.lower():
        raise AuthError(
            f"asked to authenticate {account!r} but Google granted access as "
            f"{actual!r}. Nothing has been saved. Re-run with --account {actual}, or "
            f"sign out of the other account and try again — a token filed under the "
            f"wrong name posts comments as the wrong person."
        )


def authorize(account: str, client_path: Path | None = None, no_browser: bool = False) -> None:
    """Run Google's installed-app loopback flow and store a named refresh token."""
    account = _validate_account(account)
    if client_path is not None:
        install_client(client_path)
    client = _client()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    callback: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        """Accept Google's redirect, and only Google's redirect.

        Anything on this port that is not the callback is ignored and the server
        keeps waiting. Taking the first request that arrived meant a favicon fetch,
        a probe, or a stray tab could end the wait — and then authentication failed
        with a message about a missing code rather than about what actually
        happened. A page that knows the port could end it deliberately.

        The success page is written *after* the callback parses, so "Marginal is
        authenticated" is never shown for a request that authenticated nothing.
        """

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", ""):
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if not ({"code", "error"} & set(query)):
                self.send_error(400, "not an OAuth callback")
                return
            try:
                callback["code"] = _callback_values(f"http://127.0.0.1{self.path}", state)
            except AuthError as e:
                callback["error"] = str(e)
                body = b"Authentication failed. Return to the terminal for the reason."
            else:
                body = b"Marginal is authenticated. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    # Headless/manual mode must also work in sandboxes and remote shells that
    # cannot bind a listening socket. The redirect will show a connection error,
    # but its complete URL still contains the code the user pastes back here.
    server = None if no_browser else HTTPServer(("127.0.0.1", 0), Handler)
    if server is not None:
        server.timeout = CALLBACK_TIMEOUT
    port = 53682 if server is None else server.server_port
    redirect_uri = f"http://127.0.0.1:{port}/"
    query = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": account,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    url = f"{client['auth_uri']}?{query}"
    try:
        if no_browser:
            print(f"Open this URL in a browser:\n\n{url}\n", file=sys.stderr)
            pasted = input("Paste the complete redirected URL: ").strip()
            code = _callback_values(pasted, state)
        else:
            if not webbrowser.open(url):
                print(f"Open this URL in a browser:\n\n{url}\n", file=sys.stderr)
            # Keep serving until the callback arrives or the clock runs out, rather
            # than until *a* request arrives. `handle_request` honours the timeout
            # per call, so this loop is bounded by the same deadline it always was.
            deadline = time.time() + CALLBACK_TIMEOUT
            while not callback and time.time() < deadline:
                server.handle_request()
            if "error" in callback:
                raise AuthError(callback["error"])
            if "code" not in callback:
                raise AuthError(
                    f"no OAuth callback within {CALLBACK_TIMEOUT}s. If the browser showed "
                    '"Access blocked: ... has not completed the Google verification '
                    'process", the consent screen is neither published nor lists this '
                    "account as a test user — an external app must be one or the other. "
                    "Publish it, or add this account under Test users."
                )
            code = callback["code"]
    finally:
        if server is not None:
            server.server_close()

    token = _post_form(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthError(
            "Google returned no refresh token; revoke the app grant and authenticate again"
        )
    _refuse_a_mismatched_identity(account, token.get("access_token"))
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    _write_private(_token_path(account), json.dumps(token, indent=2) + "\n")
    if default_account() is None:
        set_default(account)
