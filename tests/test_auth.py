"""Owned OAuth regressions.

The account-selection and private-write cases cover behaviour whose importance
LucaDeLeo/gdoc pointed out; the implementations under test and the tests themselves
are our own, and assert against this package's public behaviour.
"""

import json
import os
from pathlib import Path

import pytest

from marginal import auth, cli, config as config_mod, gdocs


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "CLIENT_PATH", tmp_path / "oauth-client.json")
    monkeypatch.setattr(auth, "AUTH_PATH", tmp_path / "auth.json")
    monkeypatch.setattr(auth, "ACCOUNTS_DIR", tmp_path / "accounts")
    return tmp_path


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _client(store):
    _save(
        store / "oauth-client.json",
        {
            "installed": {
                "client_id": "client",
                "client_secret": "secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
    )


def _token(store, account, **values):
    body = {"access_token": "cached", "expires_at": auth.time.time() + 3600}
    body.update(values)
    path = store / "accounts" / account / "token.json"
    _save(path, body)
    return path


def test_private_write_is_0600_and_fixes_a_loose_existing_file(tmp_path):
    # The permission regression LucaDeLeo/gdoc pointed out is worth pinning: a
    # token written over a file that was already world-readable must not stay so.
    target = tmp_path / "token.json"
    target.write_text("old")
    os.chmod(target, 0o644)

    auth._write_private(target, "new")

    assert target.read_text() == "new"
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_account_names_cannot_escape_the_store(store):
    for name in ("../../outside", ".", ".."):
        with pytest.raises(auth.AuthError, match="invalid account"):
            auth.select_account(name)


def test_explicit_account_beats_default(store):
    _token(store, "default@example.com")
    _token(store, "work@example.com")
    auth.set_default("default@example.com")

    assert auth.select_account("work@example.com") == "work@example.com"
    assert auth.select_account() == "default@example.com"


def test_one_account_is_unambiguous_but_several_are_not(store):
    _token(store, "only@example.com")
    assert auth.select_account() == "only@example.com"
    _token(store, "second@example.com")
    with pytest.raises(auth.AuthError, match="more than one"):
        auth.select_account()


def test_missing_account_points_to_owned_auth_not_workspace_mcp(store):
    with pytest.raises(auth.AuthError, match="marginal auth") as error:
        auth.select_account()
    assert "workspace_mcp" not in str(error.value)


def test_cached_token_is_used_without_a_refresh(store, monkeypatch):
    _token(store, "bot@example.com")
    monkeypatch.setattr(auth, "_post_form", lambda *a, **k: pytest.fail("refreshed"))
    assert auth.access_token("bot@example.com") == "cached"


def test_refresh_is_persisted_without_losing_the_refresh_token(store, monkeypatch):
    _client(store)
    path = _token(
        store,
        "bot@example.com",
        access_token="old",
        expires_at=0,
        refresh_token="keep-me",
    )
    seen = {}

    def refresh(url, fields):
        seen.update(url=url, fields=fields)
        return {"access_token": "new", "expires_in": 1200}

    monkeypatch.setattr(auth, "_post_form", refresh)
    assert auth.access_token("bot@example.com") == "new"
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "keep-me"
    assert saved["access_token"] == "new"
    assert seen["fields"]["grant_type"] == "refresh_token"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_refresh_token_rotation_is_not_overwritten(store, monkeypatch):
    _client(store)
    path = _token(
        store,
        "bot@example.com",
        access_token="old",
        expires_at=0,
        refresh_token="old-refresh",
    )
    monkeypatch.setattr(
        auth,
        "_post_form",
        lambda *_: {
            "access_token": "new",
            "refresh_token": "rotated-refresh",
            "expires_in": 1200,
        },
    )
    auth.access_token("bot@example.com")
    assert json.loads(path.read_text())["refresh_token"] == "rotated-refresh"


def test_corrupt_or_revoked_token_is_not_deleted(store, monkeypatch):
    corrupt = store / "accounts" / "bad@example.com" / "token.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("not json")
    with pytest.raises(auth.AuthError, match="corrupt"):
        auth.access_token("bad@example.com")
    assert corrupt.exists()

    _client(store)
    revoked = _token(
        store,
        "revoked@example.com",
        access_token="old",
        expires_at=0,
        refresh_token="evidence",
    )
    monkeypatch.setattr(
        auth, "_post_form", lambda *a, **k: (_ for _ in ()).throw(auth.AuthError("revoked"))
    )
    with pytest.raises(auth.AuthError, match="expired or was revoked"):
        auth.access_token("revoked@example.com")
    assert json.loads(revoked.read_text())["refresh_token"] == "evidence"


def test_explicit_legacy_credential_warns(store, monkeypatch, tmp_path):
    path = tmp_path / "legacy.json"
    _save(
        path,
        {
            "client_id": "c",
            "client_secret": "s",
            "refresh_token": "r",
            "token_uri": "https://oauth2.googleapis.com/token",
        },
    )
    monkeypatch.setattr(auth, "_post_form", lambda *a, **k: {"access_token": "legacy"})
    with pytest.warns(FutureWarning, match="legacy"):
        assert gdocs.access_token(path, account="ignored") == "legacy"


def test_no_browser_authorization_stores_token_and_first_default(store, monkeypatch):
    _client(store)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda *_: "state-1")
    monkeypatch.setattr(
        "builtins.input",
        lambda *_: "http://127.0.0.1/callback?state=state-1&code=code-1",
    )
    seen = {}

    def exchange(url, fields):
        seen.update(url=url, fields=fields)
        return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    monkeypatch.setattr(auth, "_post_form", exchange)
    auth.authorize("bot@example.com", no_browser=True)

    assert auth.default_account() == "bot@example.com"
    saved = json.loads(_token_path(store, "bot@example.com").read_text())
    assert saved["refresh_token"] == "refresh"
    assert seen["fields"]["grant_type"] == "authorization_code"
    assert seen["fields"]["code_verifier"]


def _token_path(store, account):
    return store / "accounts" / account / "token.json"


def test_auth_cli_dispatches_before_document_parsing(store, monkeypatch, capsys):
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    seen = {}
    monkeypatch.setattr(auth, "authorize", lambda account, **kw: seen.update(account=account, **kw))

    assert cli.main(["auth", "--account", "bot@example.com", "--no-browser"]) == 0
    assert seen["account"] == "bot@example.com" and seen["no_browser"] is True
    assert "authenticated" in capsys.readouterr().out


def test_config_accepts_named_account(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('account = "bot@example.com"\n')
    assert config_mod.load(path).account == "bot@example.com"


def test_a_failed_write_neither_leaks_a_token_nor_double_closes(tmp_path, monkeypatch):
    # Two things, and the second is the one that needs watching. A leftover file is
    # visible; a double close is not — it raises EBADF, which the old code caught,
    # so every observable effect looked correct. The damage only appears when the
    # descriptor number has been reused in between, and then it shuts an unrelated
    # file in another thread. So this asserts the call is not made at all.
    target = tmp_path / "token.json"

    class Boom(Exception):
        pass

    closed: list[int] = []
    handed_over: list[int] = []
    real_fdopen, real_close = os.fdopen, os.close

    def spy_close(fd, *a, **k):
        closed.append(fd)
        return real_close(fd, *a, **k)

    def explode(fd, *a, **k):
        handed_over.append(fd)
        fh = real_fdopen(fd, *a, **k)
        fh.write = lambda _: (_ for _ in ()).throw(Boom("disk full"))
        return fh

    monkeypatch.setattr(auth.os, "close", spy_close)
    monkeypatch.setattr(auth.os, "fdopen", explode)
    with pytest.raises(Boom):
        auth._write_private(target, '{"refresh_token": "secret"}')

    assert handed_over, "fdopen never ran; the test is not exercising the handover"
    # The file object owns the descriptor once fdopen returns, and its context
    # manager closes it. Any close of that same fd from our error path is the
    # double close. Reverting the ownership flag makes this fail.
    assert not [fd for fd in closed if fd in handed_over], closed
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "a temporary file was left holding a token"


def test_a_client_you_installed_beats_the_bundled_one(tmp_path, monkeypatch):
    # The whole point of `--client` is running on your own Cloud project. A bundled
    # client that quietly won would send a user's documents through somebody else's
    # project while they believed otherwise.
    mine, bundled = tmp_path / "mine.json", tmp_path / "bundled.json"
    mine.write_text("{}")
    bundled.write_text("{}")
    monkeypatch.setattr(auth, "CLIENT_PATH", mine)
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", bundled)
    assert auth.client_source() == (mine, "your own")


def test_the_bundled_client_is_used_when_there_is_no_other(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled.json"
    bundled.write_text("{}")
    monkeypatch.setattr(auth, "CLIENT_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", bundled)
    assert auth.client_source() == (bundled, "bundled")


def test_no_client_at_all_says_how_to_supply_one(tmp_path, monkeypatch):
    # A build shipping without a client is supported. Failing with "not found" and a
    # path would send the reader looking for a file that was never meant to exist.
    monkeypatch.setattr(auth, "CLIENT_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", tmp_path / "also-absent.json")
    assert auth.client_source() is None
    with pytest.raises(auth.AuthError) as e:
        auth._client()
    assert "--client" in str(e.value)


def test_installing_a_client_takes_over_from_the_bundled_one(tmp_path, monkeypatch):
    # No flag to remember and no state to get out of step: the file's presence is
    # the preference.
    downloaded = tmp_path / "downloaded.json"
    downloaded.write_text(json.dumps({"installed": {
        "client_id": "id", "client_secret": "s",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}))
    installed, bundled = tmp_path / "store.json", tmp_path / "bundled.json"
    bundled.write_text("{}")
    monkeypatch.setattr(auth, "CLIENT_PATH", installed)
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", bundled)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    assert auth.client_source() == (bundled, "bundled")
    auth.install_client(downloaded)
    assert auth.client_source() == (installed, "your own")
    assert auth._client()["client_id"] == "id"


def test_a_malformed_client_names_which_one_it_was(tmp_path, monkeypatch):
    # "your own" and "bundled" fail for different reasons and have different fixes.
    bad = tmp_path / "bundled.json"
    bad.write_text(json.dumps({"web": {"client_id": "x"}}))
    monkeypatch.setattr(auth, "CLIENT_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", bad)
    with pytest.raises(auth.AuthError, match="bundled"):
        auth._client()


# --- the bundled client, as actually shipped ---------------------------------


def test_the_bundled_client_in_this_repo_is_usable():
    # The suite would otherwise pass with the client missing, malformed, or
    # rejected by our own validation — and a shipped client that fails validation
    # is the one failure nobody would think to check for.
    import marginal

    shipped = Path(marginal.__file__).parent / "oauth-client.json"
    if not shipped.is_file():
        pytest.skip("this build ships no OAuth client")
    installed = auth._installed(json.loads(shipped.read_text()), " (bundled)")
    assert installed["client_id"].endswith(".apps.googleusercontent.com")
    assert installed["auth_uri"].startswith("https://accounts.google.com/")
    assert installed["token_uri"].startswith("https://oauth2.googleapis.com/")


# --- client endpoints ---------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://accounts.evil.example/auth",
        "http://accounts.google.com/o/oauth2/auth",       # not https
        "https://accounts.google.com.evil.example/auth",  # suffix, not the domain
        "https://google.com.evil.example/auth",
    ],
)
def test_a_client_pointing_somewhere_other_than_google_is_refused(uri):
    # `token_uri` is where this process posts the client secret, the code and the
    # PKCE verifier. A file naming somewhere else turns `--client` into "send my
    # authorization to whoever wrote this JSON", and such a file may arrive by
    # chat message.
    raw = {"installed": {
        "client_id": "id", "client_secret": "s",
        "auth_uri": uri, "token_uri": "https://oauth2.googleapis.com/token",
    }}
    with pytest.raises(auth.AuthError, match="not an https Google endpoint"):
        auth._installed(raw)


def test_googles_own_two_domains_are_both_accepted():
    # A rule allowing only `.google.com` rejects Google: the token endpoint is on
    # `googleapis.com`. Caught by making the test fixtures realistic.
    raw = {"installed": {
        "client_id": "id", "client_secret": "s",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}
    assert auth._installed(raw)["client_id"] == "id"


def test_a_non_string_field_is_refused_rather_than_used():
    raw = {"installed": {
        "client_id": {"nested": "object"}, "client_secret": "s",
        "auth_uri": "https://accounts.google.com/a", "token_uri": "https://oauth2.googleapis.com/t",
    }}
    with pytest.raises(auth.AuthError, match="Desktop app JSON"):
        auth._installed(raw)


# --- what an OAuth error is allowed to say ------------------------------------


def test_an_error_never_carries_the_rest_of_the_response():
    # This runs on the endpoint that mints tokens, and its output lands in
    # terminals, bug reports and pasted logs. One malformed reply should not put a
    # refresh token somewhere it outlives the process.
    body = json.dumps({
        "error": "invalid_grant",
        "error_description": "Bad Request",
        "refresh_token": "1//super-secret-should-never-appear",
        "access_token": "ya29.also-secret",
    }).encode()
    said = auth._why(body)
    assert "invalid_grant" in said and "Bad Request" in said
    assert "secret" not in said, said


def test_an_unparseable_error_is_described_not_echoed():
    assert auth._why(b"\x00\x01not json") == "<10 bytes, not JSON>"


def test_a_google_api_shaped_error_still_yields_a_reason():
    said = auth._why({"error": {"status": "PERMISSION_DENIED", "message": "nope"}})
    assert "PERMISSION_DENIED" in said


# --- the account a token actually belongs to ----------------------------------


def test_authenticating_as_the_wrong_account_is_refused_and_saves_nothing(monkeypatch):
    # `login_hint` only suggests. Someone signed into two accounts can pick the
    # other at the chooser, and the token would be filed under the name that was
    # asked for — after which comments post as the wrong person on a document
    # other people can see.
    monkeypatch.setattr(auth, "authenticated_identity", lambda _t: "other@example.com")
    with pytest.raises(auth.AuthError) as e:
        auth._refuse_a_mismatched_identity("wanted@example.com", "tok")
    assert "other@example.com" in str(e.value) and "--account" in str(e.value)


def test_a_matching_identity_passes_and_case_does_not_matter(monkeypatch):
    monkeypatch.setattr(auth, "authenticated_identity", lambda _t: "Me@Example.com")
    auth._refuse_a_mismatched_identity("me@example.com", "tok")  # must not raise


def test_an_unanswerable_identity_check_does_not_fail_the_login(monkeypatch):
    # A Drive blip must not turn into a failed authentication: an identity we could
    # not check is not an identity that failed the check.
    monkeypatch.setattr(auth, "authenticated_identity", lambda _t: None)
    auth._refuse_a_mismatched_identity("me@example.com", "tok")  # must not raise
