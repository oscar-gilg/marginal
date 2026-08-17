---
name: marginal:setup
description: >
  Get Marginal working on this machine: find or install the CLI, bring up the dedicated
  Chrome profile, confirm it is signed into Google, and write a config that matches what is
  actually available. Argument $ARGUMENTS — an optional Google Doc URL, which lets the check
  prove the browser session by exporting that document rather than inferring it from a page
  load. Run this once before `/marginal:comment`.
argument-hint: "[doc-url]"
user-invocable: true
---

You are setting up Marginal for the user. Almost all of it is automatable; one
step is not, and the whole job is to do the automatable parts without ceremony and
then be precise about the one that needs them.

## 1. Find the CLI

Try these in order and use the first that works. Do not install anything globally
without saying so.

```bash
marginal --help                                  # already on PATH
uvx marginal --help                              # published release
uvx --from git+https://github.com/oscar-gilg/marginal marginal --help
```

The rest of this skill writes `marginal ...`; if the bare command was not the
one that worked, prefix each with `uvx ` (or the full `uvx --from git+...` form).

If none works, check whether `uv` exists (`uv --version`); if it does not, tell the
user to install it from https://docs.astral.sh/uv/ and stop. Do not fall back to
`pip install --user` or to a system Python — a broken install is harder to explain
than a missing one.

## 2. Run the setup check

```bash
marginal setup $ARGUMENTS
```

Pass the document URL through if the user gave one. It makes the difference between
"a Google session exists" and "this browser can read the document you care about",
and the second is the one worth having.

The command prints a checklist and writes a `marginal.toml` describing what it
found. Read its output rather than assuming: it decides `source` and `mode` from
what is present, and those decisions are what the next skill depends on.

## 3. The step you cannot do

If the check reports the browser is signed out, it exits non-zero and a Chrome
window is open on the desktop. **Tell the user to sign in to Google in that window,
and wait for them to say they have.** Then run `marginal setup $ARGUMENTS` again.

Do not:

- retry in a loop hoping it resolves — it will not, a human has to type a password;
- report success on the strength of the window having opened;
- try to drive the sign-in form with browser automation. Google frequently refuses
  automated sign-in, and a half-completed attempt can lock the profile out.

This is a one-time step. The profile keeps its session afterwards.

## 4. Say what they got

Report, in a couple of lines:

- which invocation works, so the user can run it themselves;
- whether a model API key was found. Without one this runs in **agent mode** — you
  write the comments, Marginal places them — and the shortening pass is off,
  so comments post at whatever length they were written;
- whether Google OAuth was found. Without it the document is read through the
  browser session, and `list`, `reply`, `respond` and `unpost` are unavailable.
  Adding it is one command: `marginal auth --account you@example.com`;
- that `/marginal:comment <doc-url>` is the next thing to run.

Do not present the missing key or the missing OAuth client as problems to fix.
Both are supported paths, and the credential-free one is the reason this tool has
a browser source at all. Mention what each would buy, once, and leave it.

## 5. Ask once about the OAuth client

If they are going to use OAuth at all, ask — do not decide for them, and do not
skip it, because the answer determines whose Google Cloud project their documents
are reached through and that should be a choice rather than a default they never
saw.

Put it roughly like this, in your own words and no longer:

> marginal ships with an OAuth client, so `marginal auth` works straight away.
> That means you authenticate through the project that ships with the tool rather
> than one you own. **You don't need to change this** — it works, and the token is
> yours and stays on your machine.
>
> Setting up your own takes about ten minutes of clicking in the Google Cloud
> console, and buys three things: your API quota is not shared with every other
> user of the shipped client; you are not subject to its 100-user cap; and if
> Google ever requires that client to be verified, your setup does not stop
> working with everyone else's.
>
> Want to use the shipped one for now, or set up your own?

If they want their own, run `/marginal:oauth`. If they want the shipped one — or
have no opinion — say so plainly and move on; it is the sensible default and
switching later is one command, since an installed client takes precedence from
then on.

Ask this **once**. If they have already authenticated, or already have their own
client (`marginal config` says `your own` under `# oauth client`), there is nothing
to ask and raising it is noise.
