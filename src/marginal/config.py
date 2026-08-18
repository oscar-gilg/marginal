"""User configuration.

Prompts, model, provider and budget are settings rather than code, because the
prompt is the part people will actually want to change — and because the good
prompts already exist elsewhere (see `prompts/README.md`) rather than being
something this tool should invent.

Resolution order, first match wins per field:

    1. an explicit argument (CLI flag)
    2. --config <path>, if given
    3. ./marginal.toml in the working directory
    4. ~/.config/marginal/config.toml
    5. the defaults below
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

def source_checkout() -> Path | None:
    """The repo root when running from a source checkout, otherwise None.

    `parents[2]` is only the repo when this file sits in `src/marginal/`. Once
    installed it lands in `site-packages/marginal/`, where the same expression
    points at `lib/pythonX.Y/` — so prompts, the ledger and `.env` all resolved to
    directories that do not exist and the package could not run at all. Checking for
    the pyproject makes the difference explicit rather than assumed.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _builtin_prompts() -> Path:
    """Where the bundled prompts live: inside the package once installed.

    The wheel force-includes `prompts/` into the package directory (see
    pyproject.toml), so an installed copy finds them next to this file. A source
    checkout has no such copy and falls back to the repo's own `prompts/`, which is
    also the directory a contributor edits.
    """
    packaged = Path(__file__).resolve().parent / "prompts"
    if packaged.is_dir():
        return packaged
    root = source_checkout()
    return root / "prompts" if root else packaged


REPO = source_checkout()
BUILTIN_PROMPTS = _builtin_prompts()

USER_CONFIG = Path.home() / ".config/marginal/config.toml"
LOCAL_CONFIG = Path("marginal.toml")


@dataclass
class Config:
    # Who writes the comments. "api" has this tool call a model itself over the
    # Messages API; "agent" hands the document to a coding agent already running and
    # takes its batch back. It used to be implied by which subcommand you typed,
    # which meant the two modes could not share a settings file: every knob a run was
    # tuned with had to be retyped as a flag on the other mode's command, and a
    # difference in comment length between modes could as easily be a difference in
    # configuration. `comment` dispatches on this; the mode-specific commands still
    # run either way.
    #
    # This puts "api" on two axes that vary independently: `mode = "api"` is who
    # writes the comments, `source = "api"` is where the document text is read from.
    # An agent-mode run can read through the API, and an api-mode run can read
    # through the browser.
    #
    # "agent" is the default because it is the mode that needs nothing: most
    # installs arrive through a coding agent that is already reading the document,
    # and agent mode runs on that agent's subscription rather than a key. API mode
    # is the opt-in for running this tool standalone.
    mode: str = "agent"  # api | agent
    model: str = "claude-fable-5"
    # How hard the commenter thinks before each comment. None leaves the provider
    # default alone, which is what every run before this setting existed used;
    # `critic_effort` is the same knob for the editing pass and stays separate,
    # because the two jobs are not equally hard.
    effort: str | None = None  # low | medium | high | xhigh | max
    provider: str = "auto"  # auto | anthropic | openrouter
    comments: int = 5  # default comment budget
    strategy: str = "paragraph"  # paragraph | chars
    port: int = 9222
    headless: bool = False
    profile: str = ".chrome-profile"
    # How the document text is read. "api" needs Google OAuth; "browser" reads the
    # document through the session the signed-in Chrome already has, so a user
    # configures no credentials at all. See `source` notes in the README.
    source: str = "api"  # api | browser
    # Prefixed to every comment and reply. "" disables, which also disables
    # header-based ownership — see attribution.py.
    header: str = "**{model}**"
    # Whose account the comments are posted and read as. A separate account for the
    # model is what lets `author.me` distinguish its comments from the human's,
    # instead of guessing from the ledger — see identity notes in the README.
    # Unset uses Marginal's configured default or the sole owned account.
    account: str | None = None
    # Deprecated migration path. This must be named explicitly: Marginal no
    # longer discovers or depends on workspace-MCP's private token store.
    credentials: Path | None = None
    # The editing pass. A second model tightens each comment while the first is
    # still writing the next one, so its latency is hidden rather than paid.
    critic: bool = True
    # Show the commenter the document's figures. Off means it is told a figure is
    # there and that it cannot see it, which is the honest fallback but a weak one:
    # a figure usually carries the result the surrounding prose is claiming.
    figures: bool = True
    max_figures: int = 8
    max_figure_bytes: int = 4_000_000
    # How long a comment should be. Substituted into the critique prompt's
    # {min_words} / {max_words} / {ceiling} placeholders, so the band is a setting
    # rather than something to edit a prompt file for. A prompt with no
    # placeholders is used verbatim, which keeps a vendored one working.
    min_words: int = 20
    max_words: int = 60
    word_ceiling: int = 80
    # The commenter's reply budget. Generous because the reply carries the model's
    # reasoning as well as the tool call that submits the comment: at the 4096
    # default a run on a long document spent the whole budget thinking, returned a
    # truncated reply with no tool call, and looked exactly like a commenter with
    # nothing to say. Truncation is now reported rather than inferred, but the
    # cheaper fix is to not run out.
    max_tokens: int = 16384
    critic_model: str = "claude-opus-5"
    # Placing a quotation that no string rule could place. Retrieval rather than
    # judgement — the answer is a span that is already in the document — so a
    # cheaper model at low effort is the right trade, and it is reached rarely.
    fallback_model: str = "claude-sonnet-5"
    fallback_effort: str = "low"
    # Ask a model where an unplaceable quote belongs. Off by default: agent mode is
    # the path that needs it and is also the path with no API key, so there the work
    # is done by the submitting subagent instead. Turn it on if you have a key and
    # would rather spend a call than a round trip.
    reconcile_anchors: bool = False
    # Let the commenter look things up while it reads. Off by default: most review
    # comments are about the document's own reasoning, and a commenter that can
    # search will sometimes spend a comment on what the literature says instead.
    #
    # Both modes are told the same thing about when searching is worth it; only the
    # mechanism differs, as with the submission handoff. In API mode the commenter
    # gets Anthropic's server-side search tool; in agent mode the coding agent uses
    # whatever search its own harness provides. Without this setting the two modes
    # diverged silently — an agent could search because nothing stopped it, and the
    # api-mode commenter could not because it was given exactly one tool.
    web_search: bool = False
    # Searches per turn, on the direct Anthropic route. OpenRouter's `web` plugin
    # has no per-request equivalent — it caps results per search — so this does not
    # bind there, and a run says which it got rather than leaving the cap to look
    # like it applied.
    web_search_max_uses: int = 5
    critic_effort: str = "medium"  # low | medium | high | xhigh | max
    # Concurrent edits in flight. The generator is serial, so this only needs to be
    # large enough that a slow critique never becomes the thing the poster waits on.
    critic_workers: int = 4
    # How the stages are scheduled. Comments are written one at a time either way —
    # that is what lets the commenter decline to repeat itself and stop early — and
    # the two differ only in whether critique and posting overlap the writing.
    #
    #   async  each comment is critiqued and posted while the next is being written
    #   sync   one comment start to finish, then the next; no threads
    #
    # Named `schedule` rather than `mode`: `source` and the model/agent split were
    # both being called "mode" too, and three unrelated axes sharing one word made
    # every sentence about them ambiguous.
    schedule: str = "async"  # async | sync
    # Named for the job, not for the `review` subcommand that happens to load it:
    # this is the prompt that decides what to say, and the critique prompt below is
    # the one that decides how few words to say it in. Both run under `review`.
    commenter_prompt: Path = field(
        default_factory=lambda: BUILTIN_PROMPTS / "commenter-v2.md"
    )
    respond_prompt: Path = field(default_factory=lambda: BUILTIN_PROMPTS / "respond-v1.md")
    critique_prompt: Path = field(default_factory=lambda: BUILTIN_PROMPTS / "critique-v1.md")

    def commenter_system(self) -> str:
        return _read_prompt(self.commenter_prompt)

    def respond_system(self) -> str:
        return _read_prompt(self.respond_prompt)

    def critique_system(self) -> str:
        """The critique prompt with the configured length band substituted in.

        Plain `.replace` rather than `str.format`, because a prompt that shows the
        model a JSON example carries literal braces and `.format` would raise on
        them — a prompt file should not have to be escaped to be usable.
        """
        text = _read_prompt(self.critique_prompt)
        _check_band(self.min_words, self.max_words, self.word_ceiling)
        for key, value in (
            ("min_words", self.min_words),
            ("max_words", self.max_words),
            ("ceiling", self.word_ceiling),
        ):
            text = text.replace("{" + key + "}", str(value))
        return text


def _check_band(min_words: int, max_words: int, ceiling: int) -> None:
    """The length band must be orderable, or the critique prompt asks for nonsense.

    Checked in two places on purpose, through one function. `_validate` catches it at
    load, which is early enough to matter — `critic.tighten` catches everything, keeps
    the unedited draft, and a nonsensical band therefore disabled the editing pass
    without saying so. `critique_system` catches a `Config` built directly, which
    skips `load` entirely.
    """
    if not (min_words <= max_words <= ceiling):
        raise ValueError(
            f"length band must be min <= max <= ceiling, got "
            f"{min_words} / {max_words} / {ceiling}"
        )


def _read_prompt(path: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        # Relative paths resolve against the working directory first, then the
        # repo, so a config can point at either a user's own prompt or a bundled one.
        for base in (Path.cwd(), REPO, BUILTIN_PROMPTS.parent):
            if base is not None and (base / p).exists():
                p = base / p
                break
    if not p.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    return p.read_text().strip()


_PATH_FIELDS = {"credentials"}
_PROMPT_KEYS = (
    ("commenter", "commenter_prompt"),
    ("respond", "respond_prompt"),
    ("critique", "critique_prompt"),
)


def _load_file(path: Path) -> dict:
    data = tomllib.loads(path.read_text())
    flat = {
        k: (Path(v).expanduser() if k in _PATH_FIELDS else v)
        for k, v in data.items()
        if not isinstance(v, dict)
    }
    for key, dest in _PROMPT_KEYS:
        if key in data.get("prompts", {}):
            flat[dest] = Path(data["prompts"][key]).expanduser()
    # Fail with the offending key rather than a dataclasses TypeError, and expand `~`
    # so a config written the obvious way works.
    unknown = sorted(set(flat) - {f.name for f in fields(Config)})
    if unknown:
        raise ValueError(f"{path}: unknown setting(s) {unknown}")
    return flat


# Settings whose value must be one of a fixed set. Checked at load rather than at
# use: a typo in `schedule` used to select async silently, and one in `source` chose
# the credentialed path — both of which look like the tool ignoring the config.
_CHOICES = {
    "mode": ("api", "agent"),
    "provider": ("auto", "anthropic", "openrouter"),
    "source": ("api", "browser"),
    "schedule": ("async", "sync"),
    "strategy": ("paragraph", "chars"),
    "critic_effort": ("low", "medium", "high", "xhigh", "max"),
}


# Values that used to be spelled differently, and what to say about each. Renaming
# a value silently accepted is how a config keeps running the wrong mode; renaming
# one silently rejected sends the reader to the source. Both are avoided by naming
# the replacement in the error.
_RENAMED = {("mode", "model"): 'mode = "api"'}


def _validate(cfg: Config) -> Config:
    for name, allowed in _CHOICES.items():
        value = getattr(cfg, name)
        if value not in allowed:
            hint = _RENAMED.get((name, value))
            if hint:
                raise ValueError(
                    f"{name} = {value!r} was renamed; write {hint} instead. "
                    f'"api" means this tool calls a model itself, and is separate '
                    f'from source = "api", which is where the text is read from.'
                )
            raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
    # Same set as `critic_effort`, plus None for "leave the provider default alone".
    if cfg.effort is not None and cfg.effort not in _CHOICES["critic_effort"]:
        raise ValueError(
            f"effort must be one of {_CHOICES['critic_effort']} or unset, got {cfg.effort!r}"
        )
    for name in (
        "comments",
        "critic_workers",
        "min_words",
        "max_words",
        "word_ceiling",
        "web_search_max_uses",
    ):
        value = getattr(cfg, name)
        # `isinstance(True, int)` is True and TOML floats pass `< 1`, so both would
        # reach a ThreadPoolExecutor or a prompt substitution before failing.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a whole number of at least 1, got {value!r}")
    _check_band(cfg.min_words, cfg.max_words, cfg.word_ceiling)
    return cfg


def provenance(explicit: Path | None = None, **overrides) -> dict[str, str]:
    """Which file, or flag, last set each setting. Same layering as `load`.

    A config that is not doing what you expect is nearly always two files
    disagreeing, or a flag you forgot was in the shell history. Knowing the value is
    half the answer; knowing where it came from is the other half.
    """
    out: dict[str, str] = {}
    for path in (USER_CONFIG, LOCAL_CONFIG, explicit):
        if path and Path(path).exists():
            for key in _load_file(Path(path)):
                out[key] = str(path)
    for key, value in overrides.items():
        if value is not None:
            out[key] = "flag"
    return out


def load(explicit: Path | None = None, **overrides) -> Config:
    """Build a Config from files plus non-None keyword overrides."""
    if explicit is not None and not Path(explicit).exists():
        # A named config that isn't there is a mistake, not a reason to fall back:
        # the run would otherwise post under the default account, model and budget
        # while the user believed it was using theirs.
        raise FileNotFoundError(f"config file not found: {explicit}")
    cfg = Config()
    for path in (USER_CONFIG, LOCAL_CONFIG, explicit):
        if path and Path(path).exists():
            cfg = replace(cfg, **_load_file(Path(path)))
    real = {k: v for k, v in overrides.items() if v is not None}
    return _validate(replace(cfg, **real) if real else cfg)
