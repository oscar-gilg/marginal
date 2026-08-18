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
- whether a model API key was found. **Agent mode** — you write the comments,
  Marginal places them — is the default either way; a key buys the shortening
  pass and the option of API mode (`marginal review`), and without one the
  shortening pass is off, so comments post at whatever length they were written;
- whether Google OAuth was found. Without it the document is read through the
  browser session, and `list`, `reply`, `respond` and `unpost` are unavailable.
  Adding it is one command: `marginal auth --account you@example.com`;
- that `/marginal:comment <doc-url>` is the next thing to run.

Do not present the missing key or the missing OAuth client as problems to fix.
Both are supported paths, and the credential-free one is the reason this tool has
a browser source at all. Mention what each would buy, once, and leave it.

## 5. OAuth, if they need it

**Check whether they need it at all first.** Commenting on a document works without
any Google credentials. OAuth buys four commands — `list`, `reply`, `respond`,
`unpost` — which means answering replies and removing comments. Someone who only
wants to leave comments should not be walked through this.

`marginal config` says under `# oauth client` whether one is already installed. If
it says **your own**, skip this section entirely.

Otherwise `marginal setup` has already printed the exact command, including the
path to a client file if it found one lying around. Use what it printed rather than
composing your own — it searched `private/`, the working directory and
`~/Downloads`, and it names the newest, which is usually the one just handed over.

Three ways they get one, and it is worth asking which applies:

> A Google OAuth client is a file that identifies this application to Google, and
> marginal does not ship one. If whoever sent you marginal also sent a
> `client_secret_….json`, that is it. If they did not, ask them — it costs them
> nothing to send and saves you the setup. Failing that, making your own takes
> about ten minutes in the Google Cloud console and it is yours: your own quota,
> your own project.

- **They have a file** → the command setup printed. They run it themselves: it
  opens a browser and takes longer than an automated shell allows. Quote the path;
  never glob it, since `client_secret_*.json` expands to every client they have
  ever downloaded and the extra one becomes a positional argument.
- **They could ask for one** → say so explicitly. Someone who was handed this tool
  by a colleague will usually get a client from the same person faster than they
  will create a Google Cloud project, and it is the option people do not think of
  because it involves asking rather than doing.
- **They want their own** → `/marginal:oauth`.
- **None of the above, for now** → say so and move on. Commenting is the main
  thing and it needs none of this.

Warn them once about the consent screen, so it does not look like a failure:
**"Google hasn't verified this app"** is expected for a client nobody has submitted
for review. Advanced → Go to marginal (unsafe) → grant. It will also describe the
access as *see, edit, create, and delete all of your Google Drive files*, which is
accurate — Drive's comments API cannot reach a document the app did not create, so
the narrower scope does not work.

## 6. The two identities, if OAuth is now working

Say this once, because it is invisible until a thread has two names in it:

- a **comment** is posted through the browser, so it carries whichever Google
  account the Chrome profile is signed into;
- a **reply** goes through the API, so it carries the account that was
  authenticated.

If those differ, threads open as one person and answer as another, on a document
other people are reading. Tell them to sign the Chrome profile into the same
account they authenticated, and — if it is a separate bot account — to share the
document with it as at least Commenter.
