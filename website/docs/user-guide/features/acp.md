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

The Buzz **identity** is treated as **one credential, not three**.
`BUZZ_AUTH_TAG` is an owner attestation bound to the key in `BUZZ_PRIVATE_KEY`,
and `BUZZ_RELAY_URL` is carried in the same signed auth event. So when a managed
host supplies `BUZZ_PRIVATE_KEY` or `BUZZ_AUTH_TAG`, the profile may not supply
the others: those three names, plus `BUZZ_CREDENTIALS_FILE` (a credentials record
is itself a key and attestation), are dropped rather than merged, since an agent
that signs with one identity and presents another fails relay verification. That
covers every route the profile has into the environment (`.env`, the project
`.env`, `.op.env` and external secret sources), not just the `.env` file.

The rest of the `BUZZ_*` namespace is **plugin configuration, not identity**, and
is left alone. `BUZZ_CHANNELS`, `BUZZ_HOME_CHANNEL`, `BUZZ_ALLOWED_USERS`,
`BUZZ_CLI_PATH` and `BUZZ_POLL_INTERVAL` are written into the profile's `.env` by
`hermes setup`, and a managed agent that lost them would sign correctly and still
be unable to send or watch anything. The host still *wins* on any of them it
passes; it just does not delete the ones it did not.

`BUZZ_RELAY_URL` is a member of the identity group but does not by itself claim
it: it is a non-secret endpoint, so a host passing only a relay URL leaves the
profile's own key and tag in place rather than deleting them.

`BUZZ_AUTH_TAG` is not symmetric with that. It **does** claim the group, so a
host that supplies an attestation and no `BUZZ_PRIVATE_KEY` drops the profile's
key and supplies no replacement, and Buzz sends then fail with a generic
"must be configured" error. Hermes logs a warning naming that case at load time.
Pass the signing key that owns the attestation, unset `BUZZ_AUTH_TAG` on the
host, or set `HERMES_ACP_HOST_ENV=0` to hand precedence back to the profile.

Without `BUZZ_MANAGED_AGENT` the host is a plain editor (Zed, VS Code) and
nothing changes: the shell-export flow documented above for `buzz-acp` keeps
working, and `.env` keeps its usual precedence over it.

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
