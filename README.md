```text
     ____          __           ____        _ ____       ________    ____
    /  _/___  ____/ /_  _______/ __ )__  __(_) / /_     / ____/ /   /  _/
    / // __ \/ __  / / / / ___/ __  / / / / / / __/    / /   / /    / /
  _/ // / / / /_/ / /_/ (__  ) /_/ / /_/ / / / /_     / /___/ /____/ /
 /___/_/ /_/\__,_/\__,_/____/_____/\__,_/_/_/\__/     \____/_____/___/
```

IndusBuilt is a lightweight CLI coding agent with strict sandboxing and multi-provider LLM support. It can read, list, and edit files only inside the directory where you launch it.

## Features

- Sandboxed file operations limited to your launch directory and sub-directories
- Multi-provider runtime support: OpenAI, Anthropic (Claude), and Gemini
- Provider-aware API key management with saved keys and environment variable fallback
- In-app command palette and slash commands for settings
- Interactive provider and model selection (arrow keys + Enter)
- Streaming model responses with tool call traces
- Cross-platform terminal support (Windows, macOS, Linux)

## Installation

### Prerequisites

- Python 3.9+
- At least one API key for a supported provider

### Install from source

```bash
cd indusbuilt
pip install -e .
```

### Verify installation

```bash
indusbuilt --version
```

## Quick Start

```bash
# 1) Go to your project (this becomes the sandbox root)
cd /path/to/your/project

# 2) Launch
indusbuilt
```

At startup, IndusBuilt uses:
- Active provider from saved settings (default: `openai`)
- Provider model from saved settings (or provider default)
- API key from saved settings first, then environment variable fallback

## Slash Commands

Use these inside the running app:

- `/` or `/menu` Open command palette
- `/key` Set API key for a provider
- `/provider` Switch active provider
- `/model` Select model for active provider
- `/show` Show current provider and model
- `/help` Show command help
- `/exit` Exit the agent

## Providers and Models

Supported providers:
- `openai`
- `anthropic`
- `gemini`

Model choices:
- `openai`: `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`
- `anthropic`: `claude-3-5-sonnet-latest`, `claude-3-7-sonnet-latest`
- `gemini`: `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-1.5-pro`

Provider default models:
- `openai` -> `gpt-4o`
- `anthropic` -> `claude-3-5-sonnet-latest`
- `gemini` -> `gemini-2.0-flash`

## API Key Setup

IndusBuilt supports both saved keys and environment variables.

Environment variable names:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

### Windows (PowerShell)

```powershell
$env:OPENAI_API_KEY = "sk-your-openai-key"
$env:ANTHROPIC_API_KEY = "sk-ant-your-anthropic-key"
$env:GEMINI_API_KEY = "AIza..."
```

### macOS / Linux

```bash
export OPENAI_API_KEY="sk-your-openai-key"
export ANTHROPIC_API_KEY="sk-ant-your-anthropic-key"
export GEMINI_API_KEY="AIza..."
```

### Using `.env`

Create a `.env` file in your project:

```env
OPENAI_API_KEY=sk-your-openai-key
ANTHROPIC_API_KEY=sk-ant-your-anthropic-key
GEMINI_API_KEY=AIza...
```

## Example UI Session

```text
$ indusbuilt

     ____          __           ____        _ ____       ________    ____
    /  _/___  ____/ /_  _______/ __ )__  __(_) / /_     / ____/ /   /  _/
    / // __ \/ __  / / / / ___/ __  / / / / / / __/    / /   / /    / /
  _/ // / / / /_/ / /_/ (__  ) /_/ / /_/ / / / /_     / /___/ /____/ /
 /___/_/ /_/\__,_/\__,_/____/_____/\__,_/_/_/\__/     \____/_____/___/

  The fastest coding agent
  Sandbox: C:\Projects\my-app

  Provider: openai
  Model:    gpt-4o
  Type '/' for settings and commands.
  Type 'exit' or Ctrl+C to quit.

You > /provider
[arrow-key selector opens]

You > /model
[model selector opens for active provider]

You > add a .gitignore for python and pytest caches
IndusBuilt > I will create a Python-focused .gitignore for this project.
[tool] edit_file  state=creating
       args: {"path": ".gitignore", "old_str": "", "new_str": "..."}
       result: created_file
IndusBuilt > Done. I created `.gitignore` with Python and pytest cache ignores.
```

## Core Tool Methods

IndusBuilt exposes these internal tool methods to the model:

- `read_file(filename: str) -> { file_path, content }`
- `list_files(path: str = ".") -> { path, files[] }`
- `edit_file(path: str, old_str: str, new_str: str) -> { path, action }`

Behavior notes:
- Paths are resolved against the sandbox root and blocked if outside it
- `edit_file` with `old_str == ""` creates or overwrites a file
- `edit_file` otherwise replaces only the first occurrence of `old_str`

## CLI Options

| Flag | Short | Description |
|------|-------|-------------|
| `--version` | `-v` | Show version |
| `--help` | `-h` | Show help |

## Security Model

- Every file path is validated against sandbox boundaries
- Absolute or relative path traversal outside sandbox is denied
- Tool calls return structured error responses for safe handling

