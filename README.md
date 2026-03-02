# nanocode

Minimal coding assistant. Single Python file, zero dependencies, ~300 lines.

Built using Claude Code, then used to build itself.

## Experimental Fork

This repository is an experimental fork of the original project by [1rgs/nanocode](https://github.com/1rgs/nanocode).

Fork updates in this repo:
- Improved with Codex
- Added Inception Mercury 2 support (`INCEPTION_API_KEY`, `NANOCODE_PROVIDER=inception`)
- Added a Mercury 2-powered feature: per-turn and session token usage summaries

![screenshot](screenshot.png)

## Features

- Full agentic loop with tool use
- Inception Mercury 2 provider support
- Tools: `read`, `write`, `edit`, `glob`, `grep`, `bash`
- Per-turn and session token usage summaries
- Conversation history
- Colored terminal output

## Usage

```bash
export ANTHROPIC_API_KEY="your-key"
python nanocode.py
```

### Inception (Mercury)

```bash
export INCEPTION_API_KEY="your-key"
python nanocode.py
```

Optional:

```bash
export NANOCODE_PROVIDER="inception"
export MODEL="mercury-2"
python nanocode.py
```

### OpenRouter

Use [OpenRouter](https://openrouter.ai) to access any model:

```bash
export OPENROUTER_API_KEY="your-key"
python nanocode.py
```

To use a different model:

```bash
export OPENROUTER_API_KEY="your-key"
export MODEL="openai/gpt-5.2"
python nanocode.py
```

### Dry Run (No API key)

```bash
NANOCODE_DRY_RUN=1 NANOCODE_PROVIDER=inception python nanocode.py
```

`NANOCODE_PROVIDER` supports: `anthropic`, `openrouter`, `inception`.

## Commands

- `/h` or `/help` - Show commands
- `/stats` - Show session token stats
- `/c` - Clear conversation
- `/q` or `exit` - Quit

## Tools

| Tool | Description |
|------|-------------|
| `read` | Read file with line numbers, offset/limit |
| `write` | Write content to file |
| `edit` | Replace string in file (must be unique) |
| `glob` | Find files by pattern, sorted by mtime |
| `grep` | Search files for regex |
| `bash` | Run shell command |

## Example

```
────────────────────────────────────────
❯ what files are here?
────────────────────────────────────────

⏺ Glob(**/*.py)
  ⎿  nanocode.py

⏺ There's one Python file: nanocode.py
```

## License

MIT
