# AGENTS.md

## Repo Shape

- This repo is a Python 3.12 `uv` project. Use `uv run ...`; there is no Makefile, no CI workflow, no pytest/ruff/mypy config, and no existing test suite to lean on.
- The real code lives in `agent_arcs/`. `deep_research_bench/` is the benchmark codebase invoked by the runners, not the place to edit the agent architectures.
- The three benchmarked architectures are:
- `agent_arcs/deep_agent_arc/`: Deep-style main agent + subagents on a `FilesystemBackend`
- `agent_arcs/react_agent_arc/`: single-agent baseline with repo-local file tools
- `agent_arcs/multi_agent_arc/`: `langgraph-supervisor` baseline; supervisor reuses the React file tools

## Source Of Truth Commands

- Cheap syntax check for touched files: `uv run python -m py_compile <files...>`
- Deep smoke test: `uv run python -m agent_arcs.deep_agent_arc.smoke_test "<prompt>" --thread-id <id>`
- ReAct smoke test: `uv run python -m agent_arcs.react_agent_arc.smoke_test "<prompt>" --thread-id <id>`
- Multi smoke test: `uv run python -m agent_arcs.multi_agent_arc.smoke_test "<prompt>" --thread-id <id>`
- Single benchmark question:
- `uv run python -m agent_arcs.deep_agent_arc.run_benchmark_question --q-no <n> [--thread-id <id>] [--skip-agent] [--skip-eval] [--force-eval] [--langfuse]`
- `uv run python -m agent_arcs.react_agent_arc.run_benchmark_question --q-no <n> ...`
- `uv run python -m agent_arcs.multi_agent_arc.run_benchmark_question --q-no <n> ...`
- TU model smoke test: `uv run python -m agent_arcs.tu_model_smoke_test --role main`

## Verification Reality

- There is no formal unit/integration test suite in this repo. Practical verification is:
- `py_compile` for edited files
- the relevant architecture smoke test for wiring/streaming
- optionally one focused benchmark question if the change affects runner behavior, prompts, evaluation plumbing, or metrics

## Env Loading Quirks

- The runners and smoke tests do **not** use `python-dotenv`; they each call a small `_load_env_file(PROJECT_ROOT / ".env")` helper that line-parses `.env` into `os.environ`.
- That means `uv run python -m ...` is enough for repo scripts to see `.env`; you do not need to `source` it for those commands.
- Core runtime env expected by architectures:
- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `AI_MODEL`
- `SUB_MODEL` for Deep and Multi
- Optional Langfuse tracing needs `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`; `--langfuse-base-url` overrides the default `http://localhost:3000`.
- Benchmark judge settings are read in `deep_research_bench/utils/api.py`; OpenRouter is supported there via `BENCH_JUDGE_PROVIDER`, `OPENROUTER_RACE_MODEL`, and `OPENROUTER_FACT_MODEL`.

## Workspace And Output Layout

- All benchmark artifacts are written under `dr_bench/q<n>/<arch>/...`.
- Per-architecture workspace roots default to:
- Deep: `dr_bench/q0/deep_agent_arc`
- ReAct: `dr_bench/q0/react_agent_arc`
- Multi: `dr_bench/q0/multi_agent_arc`
- Runners override these with env vars like `DEEP_AGENT_WORKSPACE_ROOT`, `REACT_AGENT_WORKSPACE_ROOT`, and `MULTI_AGENT_WORKSPACE_ROOT` when targeting a question.
- Benchmark runners also create and expect these outputs per architecture:
- `report/`
- `tmp/review/`
- `run_metrics/`
- `race/race_result.txt`
- `fact/fact_result.txt`
- `summary.json`
- `<q_root>/q<n>_bm.md` is regenerated from each architecture’s `summary.json`

## Architecture-Specific Facts That Matter

- Deep agent uses `FilesystemBackend(root_dir=..., virtual_mode=True)` plus `FilesystemMiddleware`, `SubAgentMiddleware`, and `TodoListMiddleware`.
- Deep subagents are named exactly `scout-agent` and `research-agent`; they use DDGS MCP tools plus `think_tool`.
- ReAct does **not** use the Deep filesystem backend. It exposes its own local file tools (`list_files`, `read_file`, `write_file`, `edit_file`, `search_files`, `find_files`) with virtual path enforcement under the per-run workspace root.
- Multi uses `langgraph-supervisor` with supervisor name `multi_agent_supervisor` and worker names `scout_agent` / `research_agent`.
- Multi handoff tools are expected to be named `delegate_to_scout_agent` and `delegate_to_research_agent`; smoke-test metrics depend on that exact prefix.

## Context / Summarization / Diagnostics

- Deep and ReAct summarize at `80%` of a `262_000` token budget.
- Multi also targets `262_000`, but supervisor summarization is implemented as a custom `pre_model_hook`; subagents use `SummarizationMiddleware`.
- Custom runtime diagnostics live in `agent_arcs/diagnostic_metrics.py`. If you change retry/summarization behavior, keep those counters in sync.
- Retry helper logic in diagnostics is local on purpose; do not reintroduce imports from LangChain private modules like `langchain.agents.middleware._retry`.

## DDGS / Research Constraints

- DDGS MCP is the active search stack. `agent_arcs/mcp_and_tools.py` opens a persistent stdio MCP session with retries and augments tool descriptions with repo-specific guidance.
- The workflow is intentionally text-only. Prompts and tool descriptions forbid using image search or image URLs as report content even though DDGS exposes more tools.
- `DDGS_MCP_COMMAND` and `DDGS_PROXY` are supported env overrides if the local `ddgs mcp` command path or proxying is non-standard.

## Benchmark / Eval Plumbing

- Runners call DeepResearch Bench through subprocesses, not by importing a shared Python API.
- RACE is executed by running `deepresearch_bench_race.py` in `deep_research_bench/`.
- FACT is a 5-step pipeline run from the same directory via `python -m utils.extract`, `utils.deduplicate`, `utils.scrape`, `utils.validate`, `utils.stat`.
- If an eval step fails, inspect the generated logs in the architecture-specific `race/` or `fact/` directory; each runner writes command logs like `race_command.log` and `fact_step_<n>.log`.
- `--skip-agent` reuses an existing report and only runs validation/eval.
- `--force-eval` removes prior RACE/FACT outputs before re-running evaluation.

## Small But Easy-To-Miss Conventions

- Use fresh `--thread-id` values after changing prompts, models, or runtime behavior; the runners and smoke tests are checkpointed LangGraph apps.
- The OpenRouter cache-control extra body is only attached for Anthropic model names (`anthropic/claude...`).
- Langfuse session IDs are tied to `thread_id`; retry paths update the session/trace metadata automatically in the runners.
