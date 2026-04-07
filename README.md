## sci-agent

CLI research agent for generating long-form reports with delegated web research and sandboxed code execution.

### What it does

- Uses a main coordinator agent for planning, report writing, and artifact download
- Delegates web research to a research subagent
- Delegates plotting, calculations, and file generation to a code subagent running in Daytona
- Stores downloaded outputs under `THREADS/<thread_id>/sandbox_artifacts/`

### Requirements

- Python `3.12+`
- A configured `.env` with OpenRouter and Daytona settings
- A running Daytona API/runner if using the local self-hosted sandbox setup

### Setup

```bash
uv sync
```

Create `.env` with the required values, for example:

```env
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=...
SUB_MODEL=...
DAYTONA_API_URL=http://localhost:3000/api
DAYTONA_API_KEY=...
THREAD_ID=t23
```

### Run

Interactive CLI:

```bash
python main.py
```

Single prompt:

```bash
python main.py "Write a report on ..."
```

### Useful CLI flags

These are environment variables, not command-line switches:

```bash
CLI_SHOW_TOOL_CALLS=true
CLI_SHOW_TODOS=false
CLI_SHOW_REASONING=false
CLI_VERBOSE_TOOLS=false
```

### Notes

- Final markdown files should live at the sandbox workspace root.
- Final figures should live under `figures/`.
- Reports should reference figures with relative paths so downloaded artifacts still render locally.
