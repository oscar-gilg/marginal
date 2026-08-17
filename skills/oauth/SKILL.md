---
name: marginal:oauth
description: >
  Create a Google OAuth client of your own, so marginal can reach the Docs and Drive APIs.
  Argument $ARGUMENTS — optionally the Google account to authenticate, e.g. you@example.com.
  Use when someone needs OAuth and has no client file: marginal ships none, and OAuth is what
  `list`, `reply`, `respond` and `unpost` need. Not needed to comment on a document, and not
  needed at all if somebody sent them a client_secret JSON — that file is quicker than this.
argument-hint: "[account@example.com]"
user-invocable: true
---

You are creating a Google OAuth client the user owns, so marginal can reach the
Docs and Drive APIs on their behalf.

**First, check they need this at all.** Two questions, in order:

1. **Do they need OAuth?** Commenting on a document needs none. OAuth buys `list`,
   `reply`, `respond` and `unpost` — answering replies and removing comments. If
   they only want to leave comments, say so and stop.
2. **Could somebody send them a client instead?** marginal ships none, so a client
   is either made or received. Whoever gave them marginal can send theirs in a
   message, and `marginal auth --client that-file.json --account NAME` is the whole
   setup. That is faster than everything below, and people overlook it because it
   means asking rather than doing.

Their own client is still worth it if they expect heavy use — the quota belongs to
whoever owns the project — or if they would rather not authenticate through
somebody else's.

## 1. The parts you can do for them

If `gcloud` is installed and authenticated (`gcloud auth list`), do this yourself —
substitute their own project id, which must be globally unique, so expect the
obvious names to be taken:

```bash
gcloud projects create <project-id> --name="marginal"
gcloud services enable docs.googleapis.com drive.googleapis.com --project=<project-id>
```

If `gcloud` is missing, they do the same in the console: create a project, then
enable **Google Docs API** and **Google Drive API**. Do not install `gcloud` just
for this; it is two clicks either way.

If they are using a **separate Google account** for the bot — which is worth
suggesting, since the account's address is shown to everyone who consents — they
must `gcloud auth login <that-account>` first, and it needs a browser. That part is
theirs.

## 2. The parts only they can do

Client creation is console-only; Google exposes no API for it. Give them these
links with the project id filled in, and say what to enter. **Start at Overview** —
it runs the initial configuration, and the other tabs are incomplete until it has.

1. **Overview** — `https://console.cloud.google.com/auth/overview?project=<id>`
   Complete the getting-started flow. App name, and their own address as user
   support contact and developer contact.
2. **Audience** — `https://console.cloud.google.com/auth/audience?project=<id>`
   **External**, then either **Publish app**, or leave it in Testing and add
   themselves under **Test users**. It must be one or the other — an app that is
   neither is refused with "has not completed the Google verification process",
   which reads like a verification problem and is really an access-list one.
   Publishing avoids the seven-day refresh-token expiry that Testing imposes for
   these scopes; Testing keeps the app off anyone else's account. Say both and let
   them choose.
3. **Data Access can be left alone.** marginal requests its scopes at runtime, so
   nothing needs registering there for this to work. Note that leaving them
   unregistered does not exempt an app from Google's verification requirements —
   it just is not a step they need for setup.
4. **Clients** — `https://console.cloud.google.com/auth/clients?project=<id>`
   **Create client → Application type: Desktop app** → Create → **Download JSON**.

The type matters. marginal reads the `installed` block of the JSON and redirects
to an ephemeral loopback port, which is what a Desktop client is for. A Web
application client has a different shape and must pre-register every redirect URI,
so it will not work here.

## 3. Authenticate

They run this themselves — it opens a browser for consent, and it needs longer than
an automated shell usually allows:

```bash
marginal auth --client "/full/path/to/client_secret_<projectnumber>-....json" --account NAME
```

Two things that go wrong here, both worth pre-empting:

- **Quote the path and do not use a glob.** `~/Downloads/client_secret_*.json`
  expands to every client they have ever downloaded, and the second one lands on a
  positional argument with a confusing error.
- **Check the project number in the filename** matches the project just created.
  Downloads accumulate, and authenticating against last year's project fails in a
  way that does not mention the project at all.

They will see "Google hasn't verified this app" — that is expected for a client
nobody has submitted for review. **Advanced → Go to <name> (unsafe)** → grant.

## 4. Confirm it took

```bash
marginal config
```

Under `# oauth client` it must say **your own** and the path under
`~/.config/marginal/`. If it says **none**, the install did not happen and `auth`
did not really succeed, whatever it printed.

Then report which project and which account. If they published the consent screen
rather than leaving it in Testing, say that the address they entered is shown to
anyone who authorizes the app — which is a reason to use an account made for the
purpose rather than a personal one.
