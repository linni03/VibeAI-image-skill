# VibeAI Sub2API Image Skill

`sub2api-image` lets Codex generate and edit local PNG, JPEG, or WebP files through a user's own Sub2API key. It uses the project's synchronous OpenAI-compatible Images API, validates the returned file content and dimensions, and never depends on Codex's built-in image tool.

## Install With Codex

After publishing this repository and creating the `v1.0.0` tag, ask Codex:

```text
Use $skill-installer to install sub2api-image from
https://github.com/<owner>/vibeai-image-skill/tree/v1.0.0/skills/sub2api-image
Then start the skill's configure.py in an interactive terminal, configure my
Sub2API endpoint, and run its 1K smoke test. Do not put my API key in a shell
command or repeat it in the reply.
```

The installer places the skill at `$CODEX_HOME/skills/sub2api-image` or `~/.codex/skills/sub2api-image`. It becomes available on the next Codex turn.

The default endpoint is `https://vibeai.tech/v1` and the default model is `gpt-image-2`. Each user must have an individual Sub2API user key whose group permits image generation.

## Configure Securely

The safest setup is to run this command yourself so the key never enters chat history:

```bash
python3 ~/.codex/skills/sub2api-image/scripts/configure.py
```

The script reads the key without echo and stores JSON at `~/.config/sub2api-image/config.json` with permission `0600`. A key pasted into a Codex conversation may remain in local or remote session records; revoke any key exposed in a public/shared transcript.

Inspect non-secret settings or remove local configuration with:

```bash
python3 ~/.codex/skills/sub2api-image/scripts/configure.py --show
python3 ~/.codex/skills/sub2api-image/scripts/configure.py --revoke
```

## Use

Invoke `$sub2api-image`, or ask naturally after Codex has reloaded its skill list:

```text
Generate a 2K landscape cyberpunk city image and save it in the current project.
Edit /path/to/source.png so the background becomes watercolor paper.
```

The skill defaults to 1K square when no size is supplied. 2K and 4K remain channel-dependent; the client reports actual pixels and refuses to label a smaller returned file as the requested tier.

## Requirements

- Python 3.10 or newer
- A reachable Sub2API deployment with `/v1/images/generations` and `/v1/images/edits`
- An individual Sub2API user key in a group with image generation enabled

No third-party Python packages are required. Client smoke tests cannot read administrator-only billing logs; the Sub2API operator must verify image count, size source, size breakdown, and charge in the admin UI.
