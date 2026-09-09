---
sidebar_position: 11
title: "ACP Host Integration"
description: "Use Hermes Agent inside ACP-compatible editors and collaboration platforms"
---

# ACP Host Integration

Hermes Agent can run as an ACP server, letting ACP-compatible hosts talk to
Hermes over stdio. Editors can render:

- chat messages
- tool activity
- file diffs
- terminal commands
- approval prompts
- streamed thinking / response chunks

Other hosts can use the same protocol to route collaboration events into
Hermes. ACP is a good fit when you want Hermes to keep its existing identity,
provider setup, memory, skills, and tools while another application owns the
conversation transport.

## What Hermes exposes in ACP mode

Hermes runs with a curated `hermes-acp` toolset designed for editor workflows. It includes:

- file tools: `read_file`, `write_file`, `patch`, `search_files`
- terminal tools: `terminal`, `process`
- web/browser tools
- memory, todo, session search
- skills
- execute_code and delegate_task
- vision
- `text_to_speech`, when a TTS provider is configured (see [Voice notes](#voice-notes))

It intentionally excludes things that do not fit typical editor UX, such as messaging delivery and cronjob management.

To run ACP sessions with a different toolset, set `acp.toolsets` in `config.yaml`
(the default is `["hermes-acp"]`; MCP server toolsets are added on top):

```yaml
acp:
  toolsets: ["hermes-acp"]
```

`text_to_speech` does not come from `hermes-acp` itself: ACP sessions add the
one-tool `tts` toolset on top, so any ACP host can answer a voice note with one
once a TTS provider is configured. The tool's own requirements check drops it
when no provider is set up, so a session without a voice setup sees the tool list
`hermes-acp` has always had. To keep it off a host that does have a TTS provider:

```yaml
acp:
  tts: false
```

## Voice notes

Hosts that carry attachments (Buzz Desktop via `buzz-acp`) can hand a voice note
to `session/prompt` as a `resource_link` to an audio file, an embedded `resource`
blob, or an `audio` content block. Hermes treats such a prompt as a voice turn,
the same way the gateway does on Telegram or Discord:

- **Speech-to-text first.** Each audio attachment (`audio/*` MIME, or an audio
  extension such as `.ogg`, `.opus`, `.mp3`, `.m4a`, `.wav`, `.webm`, `.flac`,
  `.aac` whose magic bytes agree) is transcribed with the configured `stt`
  provider before the model sees the prompt, with the local fallback the gateway
  uses. The transcript is prepended to any typed text as a quoted line. If STT is
  disabled, fails, or returns nothing, the model instead sees a short note naming
  the attached file. Audio is never inlined into the prompt as text.
- **Embedded clips land in the audio cache.** A voice note sent as bytes rather
  than a file path is written to the profile audio cache
  (`$HERMES_HOME/cache/audio`), owner-readable only, the same place the gateway
  puts inbound Telegram and Discord clips. It is removed at the end of the turn,
  or kept for the agent to reach when transcription failed and the note names it,
  in which case the cache sweep collects it: the ACP server prunes that cache on
  the gateway's policy (files older than 24 hours) each time a session starts, so
  kept clips do not accumulate on a machine that never runs the gateway. A docker
  or ssh terminal backend sees the path mapped into the container.
- **Voice-first reply.** When the turn carried audio, `voice.auto_tts` is on, and
  the `text_to_speech` tool is available (a TTS provider such as `tts.provider: xai`
  is configured), Hermes adds a per-turn instruction asking the model to call
  `text_to_speech` with a spoken version of its answer, then give the text answer
  and end it with the tool's `MEDIA:<absolute path>` line. The audio file is written
  under `<session cwd>/voice/` so the host can publish it alongside the text. That
  directory holds untracked mp3s inside your project, so add `voice/` to
  `.gitignore` (or point `acp.voice_dir` somewhere outside the repo).
- **Paths with spaces.** Hosts read a `MEDIA:` line up to the first space, so a
  reply file whose path contains whitespace could never be published. When the
  session cwd has a space in it (`~/Documents/My Project`), Hermes logs a warning
  and writes the reply audio to the profile audio cache instead.
- A voice note sent while the session is still working on the previous turn is
  transcribed straight away and queued; the queued turn still answers voice-first.
- Text-only prompts never trigger any of this, so editor hosts that never send
  audio see no change.

Related config keys (all optional):

```yaml
voice:
  auto_tts: true          # the gateway's key (default false); also gates ACP voice-first replies
acp:
  auto_tts: true          # override voice.auto_tts for ACP hosts only
  voice_dir: voice        # where text_to_speech writes, relative to the session cwd
  tts: false              # drop text_to_speech from ACP sessions entirely
```

## Installation

Install Hermes normally, then add the ACP extra from the install checkout:

```bash
cd ~/.hermes/hermes-agent && uv pip install -e '.[acp]'
```

This installs the `agent-client-protocol` dependency and enables:

- `hermes acp`
- `hermes-acp`
- `python -m acp_adapter`

## Launching the ACP server

Any of the following starts Hermes in ACP mode:

```bash
hermes acp
```

```bash
hermes-acp
```

```bash
python -m acp_adapter
```

Hermes logs to stderr so stdout remains reserved for ACP JSON-RPC traffic.

For non-interactive checks:

```bash
hermes acp --version
hermes acp --check
```

### Browser tools (optional)

Browser tools (`browser_navigate`, `browser_click`, etc.) depend on the
`agent-browser` npm package and Chromium, which aren't part of the Python
wheel. Install them with:

```bash
hermes acp --setup-browser           # interactive (prompts before ~400 MB download)
hermes acp --setup-browser --yes     # accept the download non-interactively
```

This is the standalone command. The terminal-auth flow (`hermes acp --setup`) also offers the browser bootstrap as a follow-up question after model selection, so most users never need to run `--setup-browser` directly.

What it does:

- Installs Node.js 26 into `~/.hermes/node/` if missing
- `npm install -g agent-browser @askjo/camofox-browser` into that prefix (no sudo needed — `npm`'s `--prefix` points at the user-writable Hermes-managed Node)
- Installs Playwright Chromium, or uses a detected system Chrome/Chromium when available

The bootstrap is idempotent — re-running it is fast and skips work that's already done.

## Host setup

### Buzz channels (relay bridge)

[Buzz](https://github.com/block/buzz) is a Nostr-based collaboration platform
for people and agents. Its `buzz-acp` harness connects Buzz channels to any ACP
agent over stdio:

```text
Buzz relay <-- WebSocket --> buzz-acp <-- ACP over stdio --> Hermes Agent
```

This is a transport integration, not a second Hermes installation. The
subprocess launched by `buzz-acp` uses the same Hermes configuration,
credentials, memory, skills, and state as `hermes` on that host.

(This is distinct from [Buzz Desktop's managed runtime](#buzz-desktop), which
spawns Hermes locally as a preset harness. The relay bridge is for joining Buzz
*channels* as an agent identity, typically on a server.)

Prerequisites:

- Complete the ACP installation and `hermes acp --check` above.
- Build `buzz-acp` and the `buzz` CLI from the
  [Buzz repository](https://github.com/block/buzz)
  (`cargo build --release -p buzz-acp`).
- Mint a dedicated Nostr keypair for Hermes (`buzz-admin generate-key`) and
  register it as a relay member (`buzz-admin add-member`). Every agent needs
  its own identity — do not reuse a human keypair.
- Add that identity to the intended Buzz channels.

Start a bridge with:

```bash
export BUZZ_RELAY_URL="wss://community.example.com"
export BUZZ_PRIVATE_KEY="..."
export BUZZ_API_TOKEN="..."
export BUZZ_ACP_AGENT_COMMAND="hermes"
export BUZZ_ACP_AGENT_ARGS="acp"

buzz-acp
```

`BUZZ_API_TOKEN` is needed only when the relay enforces token authentication.
Do not commit or paste the private key or API token.

For a persistent server deployment, run `buzz-acp` under a service manager as
the same operating-system user that owns the intended Hermes home. Setup,
key generation, channel discovery, and per-agent options are documented in the
[buzz-acp README](https://github.com/block/buzz/tree/main/crates/buzz-acp).

The bridge discovers every Buzz channel where the Hermes identity is a member
and automatically subscribes when it is added to another channel. Buzz channel
membership therefore remains the access boundary; Hermes does not need a
separate channel list in its own configuration.

To expose Hermes ACP activity in the owner's Buzz Desktop, add:

```bash
export BUZZ_ACP_RELAY_OBSERVER="true"
```

This publishes encrypted kind `24200` observer frames addressed to the agent's
owner (Buzz's NIP-AO). Desktop renders the live lifecycle, tool, response, and
usage stream in the agent's **Activity log**. The relay treats these frames as
ephemeral, so Desktop must be online before the turn starts; its local observer
archive is the durable owner-side history.

Headless bridges answer ACP permission requests themselves because no editor
is present to show approval dialogs — see
[Keep Buzz agents owner-only](#keep-buzz-agents-owner-only). Treat the bridge
as privileged automation: use a dedicated operating-system account, restrict
which Buzz users can prompt the agent (`buzz-acp` supports an owner-only
respond gate via `BUZZ_ACP_AGENT_OWNER`), and grant membership only in channels
where Hermes is expected to work.

### VS Code

Install the [ACP Client](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client) extension.

To connect:

1. Open the ACP Client panel from the Activity Bar.
2. Select **Hermes Agent** from the built-in agent list.
3. Connect and start chatting.

If you want to define Hermes manually, add it through VS Code settings under `acp.agents`:

```json
{
  "acp.agents": {
    "Hermes Agent": {
      "command": "hermes",
      "args": ["acp"]
    }
  }
}
```

### Zed

Configure Hermes as a custom agent server in Zed settings:

1. Open the Agent Panel.
2. Add a custom agent server with the following configuration:

```json
{
  "agent_servers": {
    "hermes-agent": {
      "type": "custom",
      "command": "hermes",
      "args": ["acp"]
    }
  }
}
```

3. Start a new Hermes external-agent thread.

Prerequisites:

- Configure Hermes provider credentials first with `hermes model`, or set them in `~/.hermes/.env` / `~/.hermes/config.yaml`.

### JetBrains

Use an ACP-compatible plugin and point it at `hermes acp` or `hermes-acp`.

### Buzz Desktop

[Buzz](https://github.com/block/buzz) ships Hermes Agent as a preset runtime.
With Hermes installed the normal way, Buzz discovers it automatically —
open **Settings → Runtimes** and Hermes appears under your runtimes.

If discovery fails (older installs), make sure the ACP launcher resolves on a
login-shell PATH:

```bash
command -v hermes-acp || command -v hermes
```

Recent installs write both `hermes` and `hermes-acp` launchers into
`~/.local/bin`; running `hermes update` adds the `hermes-acp` launcher to
older installs. As a manual fallback, configure Buzz's agent command as
`hermes` with args `["acp"]`.

#### Model picker

Buzz Desktop (v0.5.1+) renders Hermes' full model menu in the agent's runtime
settings. The list comes from Hermes itself over ACP: it shows every model
from providers you have authenticated in Hermes (the same inventory behind
`hermes model` and the `/model` command), so a model missing from the menu
means its provider has no credentials configured on the Hermes side.

Entry IDs take the form `provider:model` (e.g. `openrouter:z-ai/glm-5.1`), or
`custom:<name>:<model>` for custom OpenAI-compatible endpoints defined in
`config.yaml`. Picking a model applies to that agent's session; it does not
change your Hermes-wide default — use `hermes model` for that.

#### Keep Buzz agents owner-only

Buzz creates every agent with **Who can talk to this agent** set to `Owner only`.
Leave it there when the runtime is Hermes.

Two behaviors combine on this path. The `hermes-acp` toolset includes `terminal`
and `execute_code`, and Buzz's ACP bridge answers Hermes' permission requests
itself with `allow_once` rather than surfacing them. A Hermes agent in Buzz
therefore runs shell commands on the host without prompting. I asked one to run
`rm -rf` against a scratch directory and it deleted it, no prompt anywhere.

Selecting `Anyone` hands that same shell access to every author who can reach
the channel. Buzz does not warn when you pick it.

Neither of the obvious mitigations works today:

- `approvals.mode: manual` does make Hermes raise the permission request, but
  Buzz auto-approves it and the command still runs.
- `platform_toolsets.acp` does not narrow the ACP toolset, so it cannot be used
  to drop `terminal`.

`!shutdown` from the owner stops the agent in any mode, and Buzz ignores that
command from everyone else.

## Configuration and credentials

ACP mode uses the same Hermes configuration as the CLI:

- `~/.hermes/.env`
- `~/.hermes/config.yaml`
- `~/.hermes/skills/`
- `~/.hermes/state.db`

Provider resolution uses Hermes' normal runtime resolver, so ACP inherits the currently configured provider and credentials. Hermes also advertises a terminal auth method (`--setup`) for first-run ACP clients; this opens Hermes' interactive model/provider setup.

### Precedence under an ACP host

`~/.hermes/.env` normally overrides variables inherited from the launching
shell, so a stale export cannot beat the file `hermes setup` wrote. Two
exceptions apply while Hermes runs as an ACP engine (`hermes acp`,
`hermes-acp`), because there the host process, not the shell, owns the agent's
identity:

| Variable | Who wins under an ACP host | Notes |
|----------|---------------------------|-------|
| `HERMES_HOME` | The value the process already resolved | Protected from `.env` only. `--profile`, and the sticky profile set by `hermes profile use`, still route the process first; the value they land on is the one `.env` cannot change. |
| `BUZZ_*` | The host, **when `BUZZ_MANAGED_AGENT` is set** | Buzz Desktop's `buzz-acp` harness sets that marker and injects the managed identity. The host wins on every `BUZZ_*` name it passes; only the identity group is dropped from the profile when the host claims it. |

Everything else, `OPENAI_API_KEY` and the other provider credentials included,
keeps the normal rule: `.env` wins.

#### The rule

The Buzz **identity** is **one credential, not three**. `BUZZ_AUTH_TAG` is an
owner attestation bound to the key in `BUZZ_PRIVATE_KEY`, and `BUZZ_RELAY_URL` is
carried in the same signed auth event. An agent that signs with one identity and
presents another fails relay verification, and a relay that accepts it is worse.
So there is one rule, and everything below is that rule applied at a different
seam:

> Once an ACP host supplies a **signing** member of the Buzz identity group
> (`BUZZ_PRIVATE_KEY` or `BUZZ_AUTH_TAG`, non-blank), the host owns the whole
> group. Every member the host did not supply resolves to **nothing**, by every
> route. No second principal may complete the identity.

"Supplied" means present **and non-blank** everywhere the rule is asked: `""`,
`"   "` and a trailing newline are what a harness that reads a value out of a
file exports when the file is missing, and counting one as supplied would pin a
value that cannot sign while claiming the group with it.

The group is `BUZZ_PRIVATE_KEY`, `BUZZ_AUTH_TAG`, `BUZZ_RELAY_URL` and
`BUZZ_CREDENTIALS_FILE` (a credentials record is itself a key and an
attestation). Members the host did not supply are dropped rather than merged.

"By every route" includes the profile's `config.yaml`, which no environment
rule can see. `relay_url` there is refused under a claim for the same reason
`credentials_file` is: the relay is signed into the same kind-22242 event as
the key and the attestation, so a profile that could still supply it would
choose which relay the managed identity authenticates to, and on a
Buzz-managed fleet the agent often owns that file.

The rule is applied **atomically**. The restore is a sequence of `os.environ`
writes and non-loading readers take no lock, so its intermediate states are as
observable as its result: it deletes the profile's members before it re-asserts
the host's, and the managed overlay removes the members it does not define
before it reads the file rather than after. Taken the other way round, a reader
landing mid-restore saw exactly the pairing the rule refuses, and in an ACP
process that is not hypothetical: `load_hermes_dotenv` runs on background
threads (MCP discovery, session server registration) while the main thread
resolves the identity for a send.

#### Every route into the identity, and what the rule does to it

A private key and an attestation can reach the running agent independently by
these routes. Rows 14 to 17 are the ones the earlier thirteen-row table missed:
the plugin's configuration seam, the ordering inside the restore itself, and two
that are stated rather than closed.

| # | Route | Reaches the identity via | Under a claiming host |
|---|-------|--------------------------|-----------------------|
| 1 | Host-injected process environment | `os.environ` at spawn | Owns the group. Re-asserted after every profile source. |
| 2 | Profile `<home>/.env` | `load_dotenv(override=True)` | Dropped. Appeared during the load, absent from the host snapshot. |
| 3 | Project `./.env` | `load_dotenv(override=not loaded)` | Dropped, same rule. |
| 4 | `<home>/.op.env` | direct `os.environ` write | Dropped, same rule. |
| 5 | External secret managers (Bitwarden, 1Password, `override_existing: true`) | `registry.apply_all` | Dropped. The restore runs after the vault round trip. |
| 6 | `BUZZ_CREDENTIALS_FILE` in the environment | `_configured_credentials_file` | Dropped. It is a member of the group. |
| 7 | `credentials_file` in the profile's `config.yaml` | `_resolve_credentials_data` | Refused. The record supplies **neither** half of a claimed identity. |
| 8 | `~/.config/buzz/*credentials*.json` autodiscovery | `_credentials_candidates` | Refused, same seam as 7. |
| 9 | The plugin's unscoped fallback read of `<home>/.env` off disk | `build_profile_secret_scope` | Refused. The env rule cannot see a file read, so the rule is asked at the read. |
| 10 | Cached copies of 9 (`_UNSCOPED_PROFILE_SECRETS`, `_SECRET_SOURCE_VALUES_BY_HOME`) | memoised mappings | Refused. The rule is evaluated at read time, ahead of the cache. |
| 11 | Machine-wide overlay `/etc/hermes/.env` | `_apply_managed_env`, after the last restore | Outranks the host, **as a group**: an overlay defining a signing member owns all four names and the ones it did not define are removed. |
| 12 | Plugin, MCP and terminal children | `os.environ.copy()`, `_sanitize_subprocess_env` | Inherit the corrected environment. |
| 13 | A terminal child running `buzz` **itself** | outside Hermes entirely | **Not closed, and not closable here.** The child reads `~/.config/buzz/*.json` with its own code. While the host supplies a key that key is in the child's environment and wins; a tag-only host leaves the child free to pair the record's key with the inherited tag. Scrubbing the child's environment would not close it either, since an agent holding a terminal can re-export any value it can read. The fix belongs in the `buzz` CLI's own credential resolution. |
| 14 | `relay_url` in the profile's `config.yaml`, and the YAML-to-env bridge that writes it back into `os.environ` after every restore | `_configured_relay`, `_apply_yaml_config` | Refused. The relay is a group member, signed into the same auth event. A configured relay that the claim suppresses is logged once, so the agent does not simply stop connecting in silence. |
| 15 | The restore itself, mid-flight | `_restore_acp_host_env`, `_apply_managed_env` | Closed by ordering: members are dropped before the snapshot is re-asserted, and the overlay settles the group before its file is read, so no intermediate state pairs two principals. |
| 16 | The profile secret scope under multiplexing | `_get_scoped_secret`, scoped rung | Not reachable today (a Buzz-managed ACP host is single-profile) and fails closed in both directions if it ever is. Stated rather than closed: a host tag-only claim beside a scope holding only a key resolves to the scoped key and no tag, and an empty scope to neither. |
| 17 | `hermes_cli/config.py::reload_env()` | unguarded `os.environ[key] = value` over the whole profile `.env` | Not reachable from ACP: its callers are the REPL's reload command and the `reload.env` RPC, while `acp_adapter/session.py` goes straight to `run_agent`. The function carries a comment naming the predicate to ask before that changes. |

Seams 1 to 6 are the environment restore in `hermes_cli/env_loader.py`; 7 and 8
are pair resolution in the Buzz plugin (the key and the attestation are resolved
together, from one supplier, never each with its own fallback chain, and the
pair is the only thing a consumer can obtain); 9 and 10 are the same rule asked
at the plugin's read seam; 11 is the managed overlay; 14 is the plugin's
configuration seam, where `config.yaml` reaches the identity without passing
through the environment at all; 15 is the ordering inside 1 to 6 and 11.

A host claim that leaves nothing able to sign is always logged, once per
process and by names only, whether the claimant is the host (`BUZZ_AUTH_TAG`
with no `BUZZ_PRIVATE_KEY`) or the overlay (a claim that strips the key the
host did supply). Failing closed is correct; failing closed silently leaves the
operator with Buzz's generic "must be configured" error and nothing pointing at
the cause.

The overlay is the only route that may outrank the host, and only wholesale.
`/etc/hermes/.env` is root-owned admin lockdown and keeps its documented
top-of-stack precedence, but the admin is a different principal from both the
host and the profile owner, so an overlay carrying only `BUZZ_AUTH_TAG` no
longer leaves the host's key signing the admin's attestation. An overlay that
defines no signing member has claimed nothing and keeps its ordinary per-name
precedence, so `BUZZ_RELAY_URL` alone still points a host-keyed agent at the org
relay.

Outside a host claim none of this applies: there is one principal, and the
profile owner may mix their own sources freely. An ambient `BUZZ_AUTH_TAG`
alongside a `buzz login` record is the documented NIP-OA membership flow and
still works.

The rest of the `BUZZ_*` namespace is **plugin configuration, not identity**, and
is left alone. `BUZZ_CHANNELS`, `BUZZ_HOME_CHANNEL`, `BUZZ_ALLOWED_USERS`,
`BUZZ_CLI_PATH` and `BUZZ_POLL_INTERVAL` are written into the profile's `.env` by
`hermes setup`, and a managed agent that lost them would sign correctly and still
be unable to send or watch anything. The host still *wins* on any of them it
passes; it just does not delete the ones it did not.

`BUZZ_RELAY_URL` is a member of the identity group but does not by itself claim
it: it is a non-secret endpoint, so a host passing only a relay URL leaves the
profile's own key and tag in place rather than deleting them. Under someone
else's claim it is a full member, dropped from the environment and refused from
`config.yaml` alike, because it is signed into the same auth event as the key.
A managed host that claims the identity must therefore pass the relay too; if
it does not, and the profile configured one, the plugin logs why it stopped
using it.

`BUZZ_AUTH_TAG` is not symmetric with that. It **does** claim the group, so a
host that supplies an attestation and no `BUZZ_PRIVATE_KEY` drops the profile's
key and supplies no replacement. That is a real fail-closed and not a warning
only: the plugin will not sign with a key from the profile's credentials record
either, so Buzz sends fail with the "no private key" error rather than going out
under the profile owner's key with the host's attestation. Hermes logs a warning
naming that case at load time. Pass the signing key that owns the attestation,
unset `BUZZ_AUTH_TAG` on the host, or set `HERMES_ACP_HOST_ENV=0` to hand
precedence back to the profile.

Without `BUZZ_MANAGED_AGENT` the host is a plain editor (Zed, VS Code) and
nothing changes: the shell-export flow documented above for `buzz-acp` keeps
working, and `.env` keeps its usual precedence over it. A harness that injects
`BUZZ_PRIVATE_KEY` and forgets the marker therefore gets the pre-fix behaviour,
safely (the profile then supplies both halves, so nothing is mismatched) but
otherwise silently, so Hermes logs a debug line naming exactly that at startup.

Set `HERMES_ACP_HOST_ENV=0` to turn the whole exception off and put every
variable back on the normal `.env`-wins rule. It is read from the **environment
of the spawned Hermes process**, so set it on the host's spawn (the managed-agent
definition, or the shell that launches the editor), not as a `.env` or
`config.yaml` key: the exception is decided before the profile `.env` is read.

## Host integration

These variables are set by an **ACP host process** (an editor or another agent
harness) on the Hermes subprocess it spawns. They are not user configuration —
do not set them by hand in `.env` or `config.yaml`. The one an operator may set
is `HERMES_ACP_HOST_ENV`, and it too goes on the spawn.

| Variable | Value | Effect |
|----------|-------|--------|
| `HERMES_ACP_SKIP_CONFIGURED_MCP` | `1` | Skip starting the **globally configured** MCP servers from `config.yaml` before the ACP JSON-RPC loop begins. |
| `BUZZ_MANAGED_AGENT` | any non-empty value | Set by Buzz Desktop's `buzz-acp` harness to the app instance id; only its truthiness is read. Marks the agent's identity as host-managed: the injected `BUZZ_*` credentials then win over the profile `.env`, as one group. See [Precedence under an ACP host](#precedence-under-an-acp-host). |
| `HERMES_ACP_HOST_ENV` | `0` | Operator opt-out: turns that precedence exception off, so `.env` wins for every variable as it does outside ACP mode. The one variable in this table an operator is meant to set, and still on the spawn rather than in `.env`. |

Hermes normally starts every MCP server configured in `config.yaml` before it
enters the ACP JSON-RPC loop. A host that owns MCP itself — passing the
session's servers explicitly through `session/new` — does not need that global
startup, and an unrelated slow or interactive MCP server would otherwise delay
`initialize`. Setting the marker to exactly `1` lets such a host skip it.

Only the global `config.yaml` discovery is skipped. **MCP servers supplied by
the ACP session through `session/new` are still registered**, so a host loses
no capability it asked for. Any other value (unset, empty, `0`, `false`) keeps
the default behavior, so an unrelated truthy-looking string cannot silently
disable MCP.

## Session behavior

ACP sessions are tracked by the ACP adapter's in-memory session manager while the server is running.

Each session stores:

- session ID
- working directory
- selected model
- current conversation history
- cancel event

Conversations are persisted to Hermes' session database and can be listed, loaded,
resumed, or forked after the ACP server restarts. Opening a new session without a
prompt keeps it in memory only: model-discovery probes do not create empty history
rows. A nonempty fork is persisted immediately, and existing session metadata can
still be updated even when its current history is empty.

Existing empty rows from older versions are not automatically deleted. An open ACP
row does not prove its client has disconnected. After closing the relevant editor
sessions, inspect unwanted rows with `hermes sessions show <id>` and remove only
confirmed unwanted sessions with `hermes sessions delete <id>`.

## Working directory behavior

ACP sessions bind the editor's cwd to the Hermes task ID so file and terminal tools run relative to the editor workspace, not the server process cwd.

## Approvals

Dangerous terminal commands can be routed back to the editor as approval prompts. ACP approval options are simpler than the CLI flow:

- allow once
- allow always
- deny

Whether you actually see a prompt is up to the host. A host is free to answer the
request programmatically instead of showing it to you, in which case these
options exist on the wire but never reach a human. Buzz Desktop does this, so
treat that path as unattended execution regardless of your `approvals` setting.

On timeout or error, the approval bridge denies the request.

### Session-scoped edit auto-approval

ACP exposes a third tier between *allow once* and *allow always*: **Allow for session**. Picking it from the editor's permission prompt records the approval inside the current ACP session only — every subsequent matching command in that session goes through without prompting, but a new ACP session (or restarting the editor) resets the slate and re-prompts the first time.

| Option | Editor label | Scope | Persisted across restarts |
|---|---|---|---|
| `allow_once` | Allow once | This one tool call | No |
| `allow_session` | Allow for session | All matching calls in this ACP session | No — cleared when the session ends |
| `allow_always` | Allow always | All future sessions | Yes (written to the Hermes permanent allowlist) |
| `deny` | Deny | This one tool call | No |

`allow_session` is the right default for an editor workflow where you trust an agent for the duration of a task but don't want to grant a long-lived allowlist entry. The safety trade-off is straightforward: the broader the scope, the less the editor will interrupt you, and the more damage a misbehaving agent (or prompt injection) can do before you notice. Start with `allow_once` for unfamiliar commands; promote to `allow_session` once you've seen the agent run the same pattern correctly a few times; reserve `allow_always` for truly idempotent commands you trust forever (e.g. `git status`).

The ACP bridge maps these options onto Hermes' internal approval semantics — `allow_always` writes a permanent allowlist entry the same way the CLI does, while `allow_session` only affects the in-process approval cache for the current ACP session.

## Troubleshooting

### ACP agent does not appear in the editor

Check:

- For manual/local development, verify the host command points to `hermes acp`.
- Hermes is installed and on your PATH.
- The ACP extra is installed (`cd ~/.hermes/hermes-agent && uv pip install -e '.[acp]'`).

### ACP starts but immediately errors

Try these checks:

```bash
hermes acp --version
hermes acp --check
hermes doctor
hermes status
```

### Missing credentials

ACP mode uses Hermes' existing provider setup. Configure credentials with:

```bash
hermes model
```

or by editing `~/.hermes/.env`. The terminal auth flow (`hermes acp --setup`) can also trigger the interactive provider/model setup.

## See also

- [Buzz ACP harness](https://github.com/block/buzz/tree/main/crates/buzz-acp)
- [ACP Internals](../../developer-guide/acp-internals.md)
- [Provider Runtime Resolution](../../developer-guide/provider-runtime.md)
- [Tools Runtime](../../developer-guide/tools-runtime.md)
