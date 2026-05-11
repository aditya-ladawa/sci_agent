# Anthropic Engineering Notes: Agents, Multi-Agent Research, and Long-Running Harnesses

This document consolidates the important ideas from three Anthropic engineering posts:

- `https://www.anthropic.com/engineering/building-effective-agents`
- `https://www.anthropic.com/engineering/multi-agent-research-system`
- `https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents`

The bias here is toward completeness. If a point looked operationally useful for architecture, prompting, tooling, reliability, evaluation, context management, or productionization, it is included.

## 1. Building Effective Agents


## Main cautions about agents

- Higher cost
- Compounding errors
- Need for strong guardrails
- Need for extensive testing, ideally in sandboxed environments

## Anthropic’s summary principles for effective agents

- Maintain simplicity in design.
- Prioritize transparency by explicitly showing planning steps.
- Carefully craft the agent-computer interface through tool documentation and testing.
- Add complexity only when it clearly improves outcomes.


## Appendix 2: Tool prompt engineering / ACI

- Tool definitions deserve as much prompt-engineering attention as the top-level system prompt.
- Different formats that are equivalent for humans are not equivalent for LLMs.
- Some representations are much harder for models to generate reliably, such as:
- diffs requiring correct line counts in headers
- code inside JSON requiring escaping
- Tool/interface design advice:
- give the model enough tokens to think before committing to an output format
- keep formats close to what appears naturally on the internet
- avoid formatting overhead and bookkeeping burdens
- Anthropic explicitly analogizes ACI to HCI: invest in agent-computer interfaces as seriously as human-computer interfaces.
- Good tool definitions should include:
- obvious usage
- clear parameter semantics
- examples
- edge cases
- input requirements
- boundaries from similar tools
- Rename parameters or descriptions to make usage more obvious.
- Test how the model uses the tools in practice and iterate.
- `Poka-yoke` the tool design so mistakes become harder.
- Concrete SWE-bench lesson:
- relative filepaths caused model mistakes when it moved directories
- requiring absolute filepaths fixed the issue
- Anthropic says they spent more time optimizing tools than the overall prompt in that project.

## 2. Multi-Agent Research System

## High-level purpose and motivation

- Claude Research is a multi-agent system that can search the web, Google Workspace, and integrations.
- Research tasks are open-ended, dynamic, and path-dependent.
- It is hard to predefine the exact sequence of steps.
- Human research continuously updates course based on discoveries; agentic systems are a natural fit for this style of work.
- A one-shot or strictly linear pipeline is not sufficient.

## Why multi-agent helps for research

- Search is framed as compression: distilling insights from a huge corpus.
- Subagents help because each can explore a different aspect with its own context window, then compress what matters back to the lead agent.
- Multi-agent systems also provide separation of concerns:
- distinct prompts
- distinct tools
- distinct exploration trajectories
- reduced path dependency
- more thorough and independent investigation
- Anthropic argues that once model capability passes a threshold, multi-agent systems become a way to scale performance through collective intelligence.

## Empirical findings and economics

- Internal evals showed multi-agent research systems excel especially on breadth-first queries where many independent directions can be pursued in parallel.
- Anthropic reports a multi-agent system with Opus 4 lead + Sonnet 4 subagents outperformed single-agent Opus 4 by `90.2%` on an internal research eval.
- Example: identifying all board members of Information Technology S&P 500 companies succeeded with decomposition into subtasks, while a single sequential agent failed.
- On BrowseComp, Anthropic found three factors explained `95%` of performance variance:
- token usage by itself explained `80%`
- number of tool calls
- model choice
- Implication: multi-agent systems often work because they let the system spend enough tokens/capacity to solve the problem.
- Model quality is an efficiency multiplier: upgrading to Sonnet 4 yielded larger gains than doubling the token budget on Sonnet 3.7.
- Cost downside is severe:
- regular agents use about `4x` the tokens of ordinary chat interactions
- multi-agent systems use about `15x` the tokens of chats
- Therefore economic viability requires tasks with high enough value.

## When multi-agent is a poor fit

- Tasks where all agents need to share the same context tightly
- Tasks with many dependencies between agents
- Many coding tasks today, because:
- fewer truly parallelizable subtasks than research
- LLMs are not yet great at real-time coordination/delegation across many coding subproblems
- Anthropic says multi-agent currently excels for high-value tasks with:
- heavy parallelization
- information exceeding single context windows
- many complex tools/integrations

## Architecture overview

- Research uses an orchestrator-worker pattern.
- A lead agent coordinates the task.
- Specialized subagents search in parallel.
- User query flow:
- lead agent analyzes query
- develops strategy
- spawns specialized subagents
- subagents iteratively use search tools
- subagents return findings
- lead agent synthesizes and decides whether more research is needed
- after enough information is gathered, a separate CitationAgent identifies precise citation locations
- final answer returned with citations

## Important architectural details

- The lead researcher stores its research plan to memory because if context exceeds `200,000` tokens, the context can be truncated and the plan must survive.
- The lead agent may create any number of subagents, not just a fixed pair.
- The lead agent can loop: synthesize results, decide whether more research is needed, then spawn additional or refined subagents.
- The CitationAgent is separated out rather than mixed into the search process, so citation placement is handled after research findings are assembled.

## Multi-step search vs static RAG

- Anthropic contrasts their approach with static retrieval-based RAG.
- Static RAG fetches chunks similar to the initial query.
- Their research architecture instead performs dynamic, multi-step search:
- find information
- adapt to findings
- analyze results
- formulate answers iteratively
- This is a major conceptual distinction: search is an evolving process, not just retrieval from an embedding neighborhood.

## Reported user value and usage patterns

- Anthropic says users reported that Research helped them:
- find business opportunities they had not considered
- navigate complex healthcare options
- resolve thorny technical bugs
- save up to days of work by uncovering research connections they would not have found alone
- Anthropic also included a usage clustering view for Research and reported top use-case categories:
- developing software systems across specialized domains: `10%`
- develop and optimize professional and technical content: `8%`
- develop business growth and revenue generation strategies: `8%`
- assist with academic research and educational material development: `7%`
- research and verify information about people, places, or organizations: `5%`

## Prompt engineering lessons for multi-agent systems

Anthropic says prompt engineering was their primary lever for improving bad multi-agent behavior.

### 1. Think like your agents

- To improve prompts, developers need an accurate mental model of the agent.
- Anthropic used simulations in Console with exact prompts and tools and watched step-by-step behavior.
- This exposed failure modes like:
- continuing after enough information was already found
- overly verbose search queries
- incorrect tool selection

### 2. Teach the orchestrator how to delegate

- The lead agent must provide subagents with:
- a clear objective
- expected output format
- tool/source guidance
- explicit task boundaries
- Short/vague delegation prompts caused duplication, gaps, or task misunderstanding.
- Example failure: one subagent explored the 2021 automotive chip crisis while two others duplicated 2025 supply-chain work instead of dividing labor properly.

### 3. Scale effort to query complexity

- Agents do not naturally calibrate effort well.
- Anthropic embedded scaling rules directly in prompts.
- Their example heuristics:
- simple fact-finding: `1` agent, `3-10` tool calls
- direct comparisons: `2-4` subagents, `10-15` calls each
- complex research: `10+` subagents with clearly divided responsibilities
- These budgets reduced overinvestment in simple tasks.

### 4. Tool design and tool selection are critical

- Agent-tool interfaces matter as much as user interfaces.
- Choosing the wrong tool can doom the task.
- With MCP servers and many tools, poor tool descriptions become a major source of failure.
- Anthropic gave agents explicit heuristics such as:
- inspect available tools first
- match tool to user intent
- use web search for broad external exploration
- prefer specialized tools over generic tools when applicable
- Each tool should have a distinct purpose and clear description.

### 5. Let agents improve themselves

- Claude 4 models can act as prompt engineers.
- Given a prompt and failure mode, they can diagnose failures and suggest improvements.
- Anthropic built a tool-testing agent that used a flawed MCP tool repeatedly and then rewrote the tool description.
- After dozens of tests, the improved description reduced task completion time by `40%` for future agents.

### 6. Start wide, then narrow down

- Agents often default to over-specific queries too early.
- Anthropic countered this by telling agents to begin with short, broad queries, inspect the landscape, then refine.
- This mirrors expert human research behavior.

### 7. Guide the thinking process

- Extended thinking mode acts as a controllable scratchpad.
- Lead agent uses thinking for:
- planning approach
- selecting tools
- judging complexity
- deciding subagent count
- defining subagent roles
- Subagents use interleaved thinking after tool results to:
- evaluate result quality
- detect gaps
- refine next queries
- Anthropic observed better instruction-following, reasoning, and efficiency with extended thinking.

### 8. Parallel tool calling strongly improves speed and performance

- Anthropic introduced two kinds of parallelism:
- lead agent spawns `3-5` subagents in parallel rather than serially
- subagents use `3+` tools in parallel
- This reportedly reduced research time by up to `90%` for complex tasks.
- Benefit was both speed and broader coverage.

## Prompting philosophy overall

- Anthropic focused on heuristics, not rigid rules.
- They encoded strategies used by expert human researchers:
- decompose hard questions
- evaluate source quality carefully
- adapt search approach based on findings
- choose between depth and breadth appropriately
- They also added explicit guardrails to prevent runaway behavior.
- Fast iteration loops, observability, and test cases were central.

## Evaluation lessons for multi-agent systems

### Flexible evaluation is necessary

- Traditional evals often assume one right sequence of steps.
- Multi-agent systems can take very different valid paths for the same input.
- Therefore evaluation should often focus on outcomes and reasonable process rather than exact trajectory matching.

### Start small and early

- Early development changes often have huge effect sizes.
- Anthropic started with around `20` representative queries.
- Small eval sets can be very useful early rather than waiting for hundreds of cases.

### LLM-as-judge works when designed well

- Free-form research output is hard to score programmatically.
- Anthropic used an LLM judge with rubric criteria including:
- factual accuracy
- citation accuracy
- completeness
- source quality
- tool efficiency
- They tried multiple judges but found a single LLM call with one prompt, outputting `0.0-1.0` scores plus pass/fail, was most consistent and most aligned with human judgment.
- This worked especially well when tasks still had clear answer criteria under the hood.

### Human evaluation is still essential

- Human testers found failures evals missed:
- hallucinations on unusual queries
- system failures
- subtle source biases
- Example: early agents over-preferred SEO content farms over more authoritative but lower-ranked sources like academic PDFs or personal blogs.
- Anthropic fixed this with source-quality heuristics in prompts.

### Evaluate emergent interaction patterns

- Small lead-agent changes can alter subagent behavior in unexpected ways.
- Success requires understanding collaboration patterns, not only individual agent behavior.
- Anthropic’s best prompts functioned as collaboration frameworks specifying:
- division of labor
- problem-solving approach
- effort budgets

## Production reliability and engineering lessons

### Agents are stateful and errors compound

- Long-running agents maintain state across many tool calls.
- Errors compound and cannot just be handled by restarting from scratch because that is costly and frustrating.
- Anthropic built resume-from-error systems.
- They combine model adaptability with deterministic safeguards such as:
- retry logic
- regular checkpoints
- They also explicitly inform the model when tools are failing and let it adapt.

### Debugging requires new observability

- Non-determinism makes debugging hard even with identical prompts.
- Anthropic added full production tracing to see why agents failed.
- They also monitored high-level decision patterns and interaction structures while avoiding monitoring the contents of individual user conversations for privacy.

### Deployment must account for statefulness

- Agent systems are long-running, stateful webs of prompts, tools, and execution logic.
- Deployments happen while agents are mid-flight.
- Anthropic therefore uses rainbow deployments to gradually shift traffic while keeping old and new versions alive simultaneously, preventing disruption to running agents.

### Synchronous subagent execution is currently a bottleneck

- Their lead agents currently wait for subagent sets to complete synchronously.
- This simplifies coordination.
- But it prevents:
- lead agent steering subagents mid-flight
- subagent coordination with one another
- continued progress while one slow subagent blocks the system
- Anthropic thinks asynchronous execution could improve performance further but would add challenges in:
- result coordination
- state consistency
- error propagation

## Appendix lessons

### End-state evaluation for state-mutating agents

- Turn-by-turn evaluation is difficult when agents mutate persistent state over many turns.
- Anthropic found success with end-state evaluation.
- Focus on whether the final state is correct, not whether the exact intermediate path matched expectations.
- For complex workflows, use discrete checkpoints rather than validating every intermediate action.

### Long-horizon conversation management

- Production agents may span hundreds of turns.
- Context windows become insufficient.
- Anthropic uses:
- summaries of completed phases
- external memory for essential information
- fresh subagents with clean contexts when limits approach
- retrieval of stored plans/context later
- This distributes context and avoids overflow while preserving continuity.

### Subagents writing to a filesystem to reduce game-of-telephone loss

- Rather than passing all content back through the coordinator, subagents can persist artifacts externally and send references.
- Benefits:
- less information loss
- lower token overhead
- better fidelity for structured outputs like code, reports, or visualizations
- Specialized subagent prompts may produce better artifacts directly than forcing everything through a general coordinator.

## 3. Effective Harnesses for Long-Running Agents

## Core problem

- As agents take on longer tasks, they must work across many context windows.
- Each new context window/session starts with no built-in memory of prior sessions.
- Anthropic compares this to engineers working in shifts with no memory handoff.
- Context management and compaction alone are not enough for reliable multi-session progress.

## Main failure modes Anthropic observed

### Failure mode 1: trying to do too much at once

- Given a high-level prompt like “build a clone of claude.ai,” the coding agent tried to one-shot the whole app.
- This often exhausted context mid-implementation.
- The next session inherited half-implemented, undocumented state.
- The new agent then had to guess what happened and often spent time restoring the app instead of progressing.
- Compaction helped but did not reliably produce clear enough instructions for the next session.

### Failure mode 2: premature victory

- Later in the project, an agent would inspect the codebase, see some progress, and incorrectly conclude the project was done.

## Anthropic’s two-part solution

- They built a two-fold harness around the Claude Agent SDK:
- `Initializer agent`: used only on the first run/session
- `Coding agent`: used on all subsequent sessions

### Initializer agent responsibilities

- Set up the initial environment so future sessions can work effectively.
- Write an `init.sh` script.
- Write a `claude-progress.txt` file to log what agents did.
- Create an initial git commit showing what files were added.
- Expand the high-level user prompt into a comprehensive feature list representing what “complete” means.

### Coding agent responsibilities

- Make incremental progress each session.
- Leave clear structured artifacts for the next session.
- End each session with the environment in a clean state.

## Clean state requirement

- Anthropic defines a clean state as code fit to merge to main:
- no major bugs
- orderly code
- documented enough for another developer to start new work without cleaning unrelated messes first

## Core insight for multi-context-window coding

- Fresh-session agents need to understand prior work quickly.
- Anthropic achieved this with:
- `claude-progress.txt`
- git history
- environment scaffolding
- These are essentially machine-readable/team-readable handoff artifacts modeled after effective human engineering practice.

## Environment management details

### Different first-context-window prompt

- Anthropic explicitly recommends a different prompt for the first context window.
- That prompt asks the initializer agent to prepare the environment future sessions need.

### Feature list

- To prevent one-shotting or premature completion, the initializer writes a comprehensive feature requirements file.
- In the claude.ai clone case, this was more than `200` features.
- Example feature granularity is end-to-end behavioral requirements, not vague themes.
- Each feature starts as failing.
- Coding agents only change the `passes` field after the feature is truly verified.
- Anthropic used strongly worded instructions forbidding deletion or weakening of tests/features to claim success.
- They found JSON worked better than Markdown because the model was less likely to overwrite or inappropriately edit it.

### Incremental progress model

- Once scaffolding existed, the coding agent was instructed to work on only one feature at a time.
- This was critical to reducing overreach.
- After code changes, the model should:
- commit progress to git with descriptive messages
- write a summary to the progress file
- This also lets the model revert bad changes and recover known-good states.

### Testing lessons

- Another failure mode was marking features complete without real testing.
- Without explicit prompting, Claude made code changes and maybe ran unit tests or `curl`, but still missed end-to-end failures.
- For web apps, Anthropic found that explicitly prompting the agent to use browser automation and test the app as a human user dramatically improved performance.
- Using browser automation let the agent detect and fix bugs not obvious from code alone.
- Remaining limitation:
- model vision and automation tools still miss some bug classes
- example: Claude could not see browser-native alert modals through the Puppeteer MCP, so modal-dependent features were buggier

## Session startup protocol / getting up to speed

- Every coding agent session is prompted to do a standard orientation sequence:
- run `pwd`
- read git logs and progress files
- read feature list
- choose the highest-priority unfinished feature
- This saves tokens and reduces drift.
- Anthropic also recommends that the initializer provide an `init.sh` so the coding agent can easily start the dev server and run a basic end-to-end test before changing code.
- In the claude.ai clone example, the startup test sequence included:
- start local dev server
- use Puppeteer MCP
- create a new chat
- send a message
- receive a response
- This immediate smoke test catches pre-existing breakage before the agent adds new code.
- Anthropic explicitly notes that if the app is already broken and the agent starts a new feature first, it will probably make things worse.

## Failure-mode table distilled

### Problem: declares project done too early

- Initializer writes a structured feature list derived from the user spec.
- Coding agent reads that list at session start and works on one unfinished feature.

### Problem: leaves environment buggy or undocumented

- Initializer sets up git repo and progress file.
- Coding agent reads progress and git log, runs a basic server test, and ends by writing a commit plus progress update.

### Problem: marks features done prematurely

- Initializer sets up feature list.
- Coding agent self-verifies and only marks `passes=true` after careful testing.

### Problem: wastes time learning how to run the app

- Initializer writes `init.sh`.
- Coding agent starts by reading/using `init.sh`.

## Open questions / future work

- Anthropic says it remains unclear whether a single general-purpose coding agent is optimal across contexts.
- A future direction is specialized multi-agent architectures for long-running coding, such as:
- testing agent
- QA agent
- cleanup/refactoring agent
- Another future direction is generalizing the harness lessons beyond full-stack web apps to other long-running domains like:
- scientific research
- financial modeling

## Important footnote-level clarification

- Anthropic notes that in this initializer-vs-coding setup, they call them separate agents mainly because they receive different initial user prompts.
- The system prompt, toolset, and overall harness were otherwise identical.

## 4. Cross-Cutting Synthesis Across the Three Articles

## Repeated Anthropic themes

### 1. Simplicity first, complexity only when justified

- Start with the smallest viable pattern.
- Use agents only when open-endedness or task structure requires them.
- Use multi-agent only when the task genuinely benefits from parallel exploration or separate contexts.

### 2. Tool and interface design are central

- Tools are not secondary implementation details.
- Tool definitions, parameter names, file path conventions, examples, and constraints are part of the prompt surface.
- ACI quality often determines agent success or failure.

### 3. Externalized state is critical

- Long-running or multi-agent systems cannot rely only on raw conversation context.
- Important state should be persisted externally in memory, files, git history, progress logs, feature lists, or artifacts.
- This is necessary both for continuity across context windows and to reduce coordinator bottlenecks.

### 4. Explicit decomposition and effort budgeting matter

- Agents do not reliably self-calibrate complexity, tool use, or stopping behavior without help.
- Useful prompt content includes:
- subtask boundaries
- output contracts
- source/tool guidance
- effort budgets
- stop conditions
- verification requirements

### 5. Verification should be grounded in the environment

- Agents need ground truth from tools and the environment, not just internal reasoning.
- In research this means search results, sources, and citations.
- In coding this means server startup, browser automation, tests, and reproducible state.

### 6. Observability and evaluation are not optional

- Anthropic repeatedly emphasizes tracing, fast iteration loops, and evaluation.
- For agentic systems, observability must extend beyond final outputs to process patterns and failure structures.
- Small evals are useful early.
- Human eval remains necessary even with automated judging.

### 7. Production agent systems are mostly reliability engineering problems

- Prompting matters, but productionization depends heavily on:
- retries
- checkpoints
- resumability
- deployment safety
- debugging infrastructure
- state migration and compatibility
- handling long-lived runs during deploys

## Implications for our own work on long-running research agents

- If the question is breadth-first and decomposable, multi-agent can be justified by Anthropic’s own findings.
- If the question is tightly coupled or needs heavy shared context, single-agent or more constrained workflows may still be preferable.
- For long-running research, external artifacts are not optional. Plans, evidence packets, progress logs, and review artifacts should be preserved explicitly.
- Multi-agent quality depends heavily on how the orchestrator specifies tasks and budgets, not just on having subagents.
- Citation or verification stages can be separated from discovery/synthesis stages instead of entangling everything in one loop.
- Measuring only final accuracy is insufficient; tool use, source quality, retries, summarization, context handling, and work distribution are also meaningful dimensions.

## 5. Compact Practical Takeaways

- Start with the simplest architecture that can work.
- Use workflows for predictable decomposition; use agents when paths cannot be hardcoded.
- Use multi-agent mainly when parallel exploration and separate contexts buy real performance.
- Engineer tools as if they are first-class interfaces.
- Persist plans, progress, and artifacts externally.
- Make agents work incrementally, not heroically.
- Force verification against the real environment.
- Give explicit delegation structure and effort budgets.
- Begin broad, then narrow research.
- Evaluate early with small samples, then scale.
- Use both automated judging and human review.
- Expect production difficulty to center on state, reliability, tracing, and safe deployment rather than just prompt wording.

## Sources

- Anthropic, `Building effective agents`, `https://www.anthropic.com/engineering/building-effective-agents`
- Anthropic, `A practical guide to building agents`, same article slug above and its appendices
- Anthropic, `How we built our multi-agent research system`, `https://www.anthropic.com/engineering/multi-agent-research-system`
- Anthropic, `Effective harnesses for long-running agents`, `https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents`
