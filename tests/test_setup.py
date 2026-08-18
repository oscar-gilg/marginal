"""What `marginal setup` decides, and what it refuses to claim.

The command's whole job is to be trusted about a machine it just inspected, so the
tests here are about its two ways of being wrong: reporting a working install when
the browser is signed out, and writing a config that does not match what it found.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginal import setup
from marginal.config import Config


def test_no_credentials_at_all_still_produces_a_working_config():
    # The zero-friction path, and the reason this command exists: nothing
    # configured must still yield settings that run.
    settings = setup.preset(keys=[], accounts=[])
    assert settings == {"source": "browser", "critic": False}


def test_a_key_and_an_account_override_nothing():
    # Every default is already the fast path, so the file should say so rather than
    # restate six settings that were never in question.
    assert setup.preset(keys=["ANTHROPIC_API_KEY"], accounts=["me@example.com"]) == {}


def test_a_key_without_google_only_changes_where_the_text_is_read():
    assert setup.preset(keys=["OPENROUTER_API_KEY"], accounts=[]) == {"source": "browser"}


def test_google_without_a_key_only_turns_the_critic_off():
    # Agent mode is the default now, so a missing key changes nothing about who
    # writes — but the shortening pass runs inside `post-batch`, so it would
    # otherwise fail on every comment at the point it tries to tighten one.
    assert setup.preset(keys=[], accounts=["me@example.com"]) == {"critic": False}


@pytest.mark.parametrize(
    "keys,accounts",
    [([], []), (["K"], []), ([], ["a@x.com"]), (["K"], ["a@x.com"])],
)
def test_every_reachable_config_parses_as_the_settings_it_claims(tmp_path, keys, accounts):
    # Rendering is string building, so it can produce something that reads correctly
    # and loads as something else. All four reachable presets, not just the one that
    # happened to be checked — a quoting bug in a branch nobody parametrised is
    # exactly the shape this misses otherwise.
    import tomllib

    settings = setup.preset(keys=keys, accounts=accounts)
    path = tmp_path / "marginal.toml"
    setup.write_config(path, settings, force=False)
    assert tomllib.loads(path.read_text()) == settings


def test_the_written_config_is_accepted_by_the_config_loader(tmp_path, monkeypatch):
    # A generated file that the tool itself rejects is the worst possible output of
    # a setup command, and `_validate` rejects values as well as names.
    from marginal import config as config_mod

    path = tmp_path / "marginal.toml"
    setup.write_config(path, setup.preset(keys=[], accounts=[]), force=False)
    monkeypatch.chdir(tmp_path)
    cfg = config_mod.load()
    assert (cfg.source, cfg.mode, cfg.critic) == ("browser", "agent", False)


def test_an_existing_config_is_never_overwritten_without_force(tmp_path):
    path = tmp_path / "marginal.toml"
    path.write_text("# hand tuned\nheadless = true\n")
    check = setup.write_config(path, {"source": "browser"}, force=False)
    assert check.ok and "already exists" in check.detail
    assert "hand tuned" in path.read_text()

    setup.write_config(path, {"source": "browser"}, force=True)
    assert "hand tuned" not in path.read_text()


def test_the_shallow_session_check_says_it_is_shallow(monkeypatch):
    # A tick with no caveat is how someone concludes they are set up and then
    # watches the first real run fail. Without a document there is nothing to prove
    # the session can read, and the detail has to say so.
    class FakePage:
        def wait_until(self, *a, **k):
            return True

        def eval(self, expr):
            return "https://docs.google.com/document/u/0/"

        def close(self):
            pass

    monkeypatch.setattr(setup.Page, "open", classmethod(lambda cls, *a, **k: FakePage()))
    check = setup.google_session(9222, None)
    assert check.ok and "not verified" in check.detail


def test_a_sign_in_redirect_is_a_failure_with_the_fix_on_it(monkeypatch):
    class FakePage:
        def wait_until(self, *a, **k):
            return True

        def eval(self, expr):
            return "https://accounts.google.com/ServiceLogin?continue=..."

        def close(self):
            pass

    monkeypatch.setattr(setup.Page, "open", classmethod(lambda cls, *a, **k: FakePage()))
    check = setup.google_session(9222, None)
    assert not check.ok
    assert "Sign in" in check.fix


def test_a_failed_export_is_reported_as_a_failure(monkeypatch):
    # The export is the operation `source = "browser"` performs on every run, so a
    # failure here is the credential-free path not working — never a pass.
    class FakePage:
        def close(self):
            pass

    monkeypatch.setattr(setup.Page, "open", classmethod(lambda cls, *a, **k: FakePage()))
    monkeypatch.setattr(
        setup.browser_source,
        "export",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("no download")),
    )
    check = setup.google_session(9222, "https://docs.google.com/document/d/" + "d" * 25 + "/edit")
    assert not check.ok and "could not export" in check.detail


def test_a_successful_export_proves_the_session(monkeypatch):
    class FakePage:
        def close(self):
            pass

    monkeypatch.setattr(setup.Page, "open", classmethod(lambda cls, *a, **k: FakePage()))
    monkeypatch.setattr(setup.browser_source, "export", lambda *a, **k: b"x" * 40)
    monkeypatch.setattr(setup.browser_source, "invalidate", lambda *a, **k: None)
    check = setup.google_session(9222, "https://docs.google.com/document/d/" + "d" * 25 + "/edit")
    assert check.ok and "exported 40" in check.detail


def test_setup_exits_non_zero_when_the_browser_is_signed_out(monkeypatch, tmp_path, capsys):
    # Exit codes are what a coding agent driving this reads. Signed out is the one
    # state where the next command cannot work, so it must not exit 0.
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", True, "ok"))
    monkeypatch.setattr(
        setup, "start_chrome", lambda cfg, **k: setup.Check("Chrome profile", True, "up")
    )
    monkeypatch.setattr(
        setup,
        "google_session",
        lambda port, doc: setup.Check("Google session", False, "redirected", fix="Sign in"),
    )
    monkeypatch.setattr(setup, "model_keys", lambda: (setup.Check("k", True, "set"), ["K"]))
    monkeypatch.setattr(setup, "google_accounts", lambda a=None: (setup.Check("g", True, "a"), ["a"]))
    code = setup.run(Config(), None, write=True, config_path=tmp_path / "c.toml")
    assert code == 1
    assert "Sign in to Google" in capsys.readouterr().out


def test_setup_exits_zero_with_no_credentials_because_that_is_a_choice(
    monkeypatch, tmp_path, capsys
):
    # A missing key is the supported path, not a failure. Exiting non-zero on it
    # would break the very scripts that deliberately take it.
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", True, "ok"))
    monkeypatch.setattr(
        setup, "start_chrome", lambda cfg, **k: setup.Check("Chrome profile", True, "up")
    )
    monkeypatch.setattr(
        setup, "google_session", lambda port, doc: setup.Check("Google session", True, "signed in")
    )
    monkeypatch.setattr(setup, "model_keys", lambda: (setup.Check("k", False, "none"), []))
    monkeypatch.setattr(setup, "google_accounts", lambda a=None: (setup.Check("g", False, "none"), []))
    path = tmp_path / "marginal.toml"
    assert setup.run(Config(), None, write=True, config_path=path) == 0
    out = capsys.readouterr().out
    assert "agent mode" in out
    assert 'source = "browser"' in path.read_text()


def test_setup_stops_at_a_missing_chrome_rather_than_launching_one(monkeypatch, tmp_path):
    # Everything after this needs a browser, so continuing would produce a cascade
    # of failures whose first cause is the only one worth printing.
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", False, "gone"))
    monkeypatch.setattr(
        setup,
        "start_chrome",
        lambda *a, **k: pytest.fail("launched Chrome after reporting it is not installed"),
    )
    assert setup.run(Config(), None, write=True, config_path=tmp_path / "c.toml") == 1


def test_setup_never_writes_a_config_when_asked_not_to(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", True, "ok"))
    monkeypatch.setattr(
        setup, "start_chrome", lambda cfg, **k: setup.Check("Chrome profile", True, "up")
    )
    monkeypatch.setattr(
        setup, "google_session", lambda port, doc: setup.Check("Google session", True, "in")
    )
    monkeypatch.setattr(setup, "model_keys", lambda: (setup.Check("k", False, "none"), []))
    monkeypatch.setattr(setup, "google_accounts", lambda a=None: (setup.Check("g", False, "none"), []))
    path = tmp_path / "marginal.toml"
    setup.run(Config(), None, write=False, config_path=path)
    assert not path.exists()
    assert 'source = "browser"' in capsys.readouterr().out


def test_setup_launches_headful_even_when_the_config_says_headless(monkeypatch):
    # The command exists to get a profile signed in, and headless sign-in is the
    # part Google refuses. A `headless = true` config must not make setup open the
    # one browser the step cannot be completed in.
    seen = {}
    monkeypatch.setattr(setup, "chrome_running", lambda port: False)
    monkeypatch.setattr(
        setup,
        "launch_chrome",
        lambda profile, port, url=None, headless=False: seen.update(headless=headless),
    )
    monkeypatch.setattr(setup, "wait_for_port", lambda port, timeout=0: None)
    setup.start_chrome(Config(headless=True))
    assert seen == {"headless": False}


def test_a_config_that_needs_nothing_says_so_rather_than_being_empty(tmp_path):
    # An empty file is indistinguishable from a command that failed silently.
    path = tmp_path / "marginal.toml"
    setup.write_config(path, {}, force=False)
    text = path.read_text()
    assert "already the fast path" in text
    assert not [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def test_every_written_setting_carries_its_reason():
    # A generated config whose lines have no reason attached gets copied forward
    # long after the reason expired.
    rendered = setup.render(setup.preset(keys=[], accounts=[]))
    for name in ("source", "critic"):
        assert name in setup._WHY
    assert rendered.count("#") >= 3 + 2  # header lines plus one reason per setting


def test_the_config_path_the_command_writes_is_the_one_it_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", True, "ok"))
    monkeypatch.setattr(
        setup, "start_chrome", lambda cfg, **k: setup.Check("Chrome profile", True, "up")
    )
    monkeypatch.setattr(
        setup, "google_session", lambda port, doc: setup.Check("Google session", True, "in")
    )
    monkeypatch.setattr(setup, "model_keys", lambda: (setup.Check("k", True, "set"), ["K"]))
    monkeypatch.setattr(setup, "google_accounts", lambda a=None: (setup.Check("g", True, "a"), ["a"]))
    path = tmp_path / "elsewhere.toml"
    setup.run(Config(), None, write=True, config_path=path)
    assert str(path) in capsys.readouterr().out
    assert path.exists()


def test_a_page_that_never_finishes_loading_is_not_a_pass(monkeypatch):
    # `wait_until` returns False on timeout rather than raising, and mid-navigation
    # `location.href` is still the URL we asked for — so a stalled load looked
    # exactly like a signed-in one. Unknown must not resolve in our own favour.
    class StalledPage:
        def wait_until(self, *a, **k):
            return False

        def eval(self, expr):
            return "https://docs.google.com/document/u/0/"

        def close(self):
            pass

    monkeypatch.setattr(setup.Page, "open", classmethod(lambda cls, *a, **k: StalledPage()))
    check = setup.google_session(9222, None)
    assert not check.ok and "did not finish loading" in check.detail


def test_accounts_that_exist_but_cannot_be_selected_are_not_a_working_setup(monkeypatch):
    # Two tokens and no default is a directory full of credentials and an AuthError
    # on every run. Counting files passed it and wrote `source = "api"`, which is
    # the Ready-then-fail shape this command exists to prevent.
    monkeypatch.setattr(setup.auth, "list_accounts", lambda: ["a@x.com", "b@x.com"])
    monkeypatch.setattr(
        setup.auth,
        "select_account",
        lambda account=None: (_ for _ in ()).throw(setup.auth.AuthError("more than one")),
    )
    check, accounts = setup.google_accounts()
    assert not check.ok
    assert accounts == []
    # And the config that follows must route around them rather than trusting them.
    assert setup.preset(keys=["K"], accounts=accounts)["source"] == "browser"


def test_a_selectable_account_is_a_working_setup(monkeypatch):
    monkeypatch.setattr(setup.auth, "list_accounts", lambda: ["a@x.com"])
    monkeypatch.setattr(setup.auth, "select_account", lambda account=None: "a@x.com")
    check, accounts = setup.google_accounts()
    assert check.ok and accounts == ["a@x.com"]
    assert setup.preset(keys=["K"], accounts=accounts) == {}


def test_a_failure_listing_accounts_is_loud_rather_than_read_as_none(monkeypatch):
    # Silently treating this as "no accounts" would write `source = "browser"` over
    # a working credentialed setup.
    monkeypatch.setattr(
        setup.auth,
        "list_accounts",
        lambda: (_ for _ in ()).throw(OSError("permission denied")),
    )
    with pytest.raises(OSError):
        setup.google_accounts()


def test_the_configured_account_is_the_one_checked(monkeypatch):
    # `select_account(None)` and `select_account("b@x.com")` disagree on a machine
    # with two accounts, and the config names which one a run will use.
    asked = []
    monkeypatch.setattr(setup.auth, "list_accounts", lambda: ["a@x.com", "b@x.com"])
    monkeypatch.setattr(
        setup.auth, "select_account", lambda account=None: asked.append(account) or "b@x.com"
    )
    setup.google_accounts("b@x.com")
    assert asked == ["b@x.com"]


def test_no_oauth_says_which_commands_will_not_work(capsys):
    # The concrete cost of skipping OAuth, said at the moment it is being skipped
    # rather than discovered when a reply fails.
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "respond" in out and "reply" in out
    assert setup._next_step(None, keys=["K"], accounts=["a@x.com"]).count("respond") == 0


def test_setup_writes_to_out_not_to_the_config_it_reads(monkeypatch, tmp_path):
    # `--config` names a file to read and refuses one that does not exist, which is
    # the opposite of what setup does with a path. Overloading it meant
    # `--config new.toml setup` failed at load, before setup could create the file
    # it was being asked to create.
    from marginal import cli, gdocs

    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: pytest.fail("minted a token"))
    monkeypatch.setattr(setup, "chrome_installed", lambda: setup.Check("Chrome", True, "ok"))
    monkeypatch.setattr(
        setup, "start_chrome", lambda cfg, **k: setup.Check("Chrome profile", True, "up")
    )
    monkeypatch.setattr(
        setup, "google_session", lambda port, doc: setup.Check("Google session", True, "in")
    )
    monkeypatch.setattr(setup, "model_keys", lambda: (setup.Check("k", False, "none"), []))
    monkeypatch.setattr(setup, "google_accounts", lambda a=None: (setup.Check("g", False, "n"), []))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "written-here.toml"
    assert cli._main(["setup", "--out", str(out)]) == 0
    assert out.exists()
    assert not (tmp_path / "marginal.toml").exists()


def test_setup_is_reachable_from_the_cli_without_a_document(monkeypatch, tmp_path):
    # `setup` has to run on a machine where nothing is configured, which means it
    # must return before the document is parsed and before a Google token is minted.
    from marginal import cli, gdocs

    monkeypatch.setattr(
        gdocs, "access_token", lambda *a, **k: pytest.fail("minted a token during setup")
    )
    called = {}
    monkeypatch.setattr(
        setup, "run", lambda cfg, doc, **k: called.update(doc=doc, kw=k) or 0
    )
    monkeypatch.chdir(tmp_path)
    assert cli._main(["setup"]) == 0
    assert called["doc"] is None


def test_a_bundled_client_is_disclosed_at_setup_time(monkeypatch):
    # Which Google Cloud project a user's documents are reached through is a choice.
    # A default nobody was shown is not one, and most users never see the plugin
    # skill that would otherwise be the only place this is said.
    monkeypatch.setattr(setup.auth, "client_source", lambda: (Path("/pkg/oauth.json"), "bundled"))
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "shipped with marginal" in out
    assert "your own" in out
    assert "100-user cap" in out


def test_a_client_of_your_own_prompts_nothing(monkeypatch):
    # Nothing to disclose and nothing to offer: raising it would be noise.
    monkeypatch.setattr(
        setup.auth, "client_source", lambda: (Path("~/.config/marginal/x.json"), "your own")
    )
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "shipped with marginal" not in out


def test_an_authenticated_user_gets_one_line_not_the_explanation(monkeypatch):
    # Having a token is not the same as having chosen whose project it is on:
    # anyone who authenticated before the client shipped never saw the question.
    # So it is still named — once, briefly — rather than repeated in full, which is
    # the kind of advice that trains people to skim.
    monkeypatch.setattr(setup.auth, "client_source", lambda: (Path("/pkg/oauth.json"), "bundled"))
    out = setup._next_step(None, keys=["K"], accounts=["me@example.com"])
    assert "shipped with marginal" in out
    assert "100-user cap" not in out, "the full explanation is for people still deciding"
    # The agent-mode next-step block is four lines on its own; the cap is about the
    # OAuth explanation staying brief, not about that block.
    assert len(out.splitlines()) < 11


def test_an_authenticated_user_on_their_own_client_is_told_nothing(monkeypatch):
    monkeypatch.setattr(
        setup.auth, "client_source", lambda: (Path("~/.config/marginal/x.json"), "your own")
    )
    out = setup._next_step(None, keys=["K"], accounts=["me@example.com"])
    assert "shipped with marginal" not in out


def test_no_client_at_all_names_all_three_ways_to_get_one(monkeypatch):
    # With nothing shipped, "run marginal auth" is not actionable on its own. All
    # three routes matter, and asking is the one people overlook — it is the only
    # one that involves waiting on someone else rather than doing something.
    monkeypatch.setattr(setup.auth, "client_source", lambda: None)
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "--client" in out
    assert "were sent" in out, "the file may already be in hand"
    assert "ask whoever" in out, "asking is the cheapest route and the least obvious"
    assert "/marginal:oauth" in out, "and making your own must stay available"


def test_a_client_lying_around_is_named_in_the_command(monkeypatch, tmp_path):
    # The file is usually already on disk — just handed over, or downloaded from the
    # console minutes ago. "Supply a client" is a step this command can do most of.
    (tmp_path / "client_secret_123-abc.apps.googleusercontent.com.json").write_text("{}")
    monkeypatch.setattr(setup.auth, "client_source", lambda: None)
    monkeypatch.setattr(setup, "CLIENT_HUNTING_GROUND", (str(tmp_path),))
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "client_secret_123-abc" in out
    assert "/path/to/client_secret.json" not in out, "named a placeholder over a real file"


def test_several_clients_are_not_silently_chosen_between(monkeypatch, tmp_path):
    # Picking one out of several for somebody is how an old download authenticates
    # against a forgotten project and fails without mentioning it.
    import os, time
    for i, name in enumerate(("client_secret_111-old.json", "client_secret_999-new.json")):
        p = tmp_path / name
        p.write_text("{}")
        os.utime(p, (time.time() + i, time.time() + i))
    monkeypatch.setattr(setup.auth, "client_source", lambda: None)
    monkeypatch.setattr(setup, "CLIENT_HUNTING_GROUND", (str(tmp_path),))
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "client_secret_999-new" in out, "should offer the newest"
    assert "2 client files found" in out
    assert "project number" in out


def test_the_two_sided_identity_is_stated_once_oauth_works(monkeypatch):
    # Invisible until a thread has two names in it: comments go through the browser
    # and replies through the API, so they carry different accounts unless the same
    # one is on both sides.
    monkeypatch.setattr(setup.auth, "client_source", lambda: (Path("/x.json"), "your own"))
    out = setup._next_step(None, keys=["K"], accounts=["bot@example.com"])
    assert "bot@example.com" in out
    assert "Chrome profile is signed into" in out


def test_nothing_about_identity_before_oauth_exists(monkeypatch):
    # With no token there is only one identity, and the warning would be noise.
    monkeypatch.setattr(setup.auth, "client_source", lambda: None)
    monkeypatch.setattr(setup, "CLIENT_HUNTING_GROUND", ())
    out = setup._next_step(None, keys=["K"], accounts=[])
    assert "answer as another" not in out


def test_printed_commands_are_runnable_as_printed(monkeypatch):
    # `uvx marginal` installs nothing — that is why it is recommended — so `marginal`
    # is then not on PATH. Printing bare `marginal auth ...` to someone who got here
    # that way hands them a command that fails with "command not found", which reads
    # as a broken tool rather than a missing install.
    monkeypatch.setattr(setup.shutil, "which", lambda _n: None)
    monkeypatch.setattr(setup.auth, "client_source", lambda: None)
    monkeypatch.setattr(setup, "CLIENT_HUNTING_GROUND", ())
    out = setup._next_step(None, keys=["K"], accounts=[])
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("marginal "):
            raise AssertionError(f"not runnable without an install: {stripped!r}")
    assert "uvx marginal" in out

    monkeypatch.setattr(setup.shutil, "which", lambda _n: "/usr/local/bin/marginal")
    on_path = setup._next_step(None, keys=["K"], accounts=[])
    assert "uvx marginal" not in on_path, "should not prefix uvx when it is installed"
