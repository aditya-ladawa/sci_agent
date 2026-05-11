# Anthropic Notes: Effective Context Engineering for AI Agents

Source:

- `https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents`

This file is a comprehensive notes document oriented toward what we should incorporate into our Deep agent architecture and benchmark setup.

## Core thesis

- The center of gravity is shifting from prompt engineering to `context engineering`.
- Prompt engineering is about writing/organizing instructions.
- Context engineering is about curating and maintaining the best possible set of tokens available at inference time.
- In agent systems, this includes not just the system prompt, but also:
- tools
- MCP tools
- external data
- message history
- memory
- retrieved artifacts
- runtime state
- Context engineering is iterative, not one-time. The curation problem reappears every time we decide what to pass into the model.

## Definitions

- `Context` = the set of tokens included when sampling from an LLM.
- `Context engineering` = optimizing the utility of those tokens under model constraints to consistently produce desired behavior.
- Effective engineering requires `thinking in context`: reasoning about the model’s full available state and what behaviors that state is likely to induce.

## Why context engineering matters

### Context is finite and degrades with size

- LLMs lose focus as context grows.
- Anthropic points to `context rot`: as token count increases, recall quality and precision degrade.
- This is a general phenomenon across models, even if some degrade more gracefully than others.
- Therefore context should be treated as a finite resource with diminishing marginal returns.

### Attention budget framing

- Anthropic frames this like a limited attention budget or working memory.
- Every token consumes some share of that budget.
- More tokens increase the need for careful curation, because irrelevant or low-signal tokens dilute attention from important ones.

### Architectural reasons

- Transformer attention creates `n^2` pairwise token relationships.
- As contexts grow, the model’s ability to model all useful relationships becomes stretched thinner.
- Training data usually contains more short sequences than long ones, so models have less specialized experience with very long-range dependencies.
- Long-context extensions like position interpolation help, but can degrade positional understanding.
- So longer context windows help, but they do not remove the need for context engineering.

## The main principle

- Good context engineering means finding the `smallest possible set of high-signal tokens` that maximizes the probability of the desired behavior.
- `Minimal` does not necessarily mean `short`.
- It means no unnecessary tokens, while still including enough information to reliably induce the required behavior.

## System prompt lessons

### Where this lesson belongs

- This lesson belongs primarily in our `Deep` architecture documentation and prompt-design guidance, because it directly governs how the main Deep agent prompt and subagent prompts should be written.
- The most suitable places for this guidance are:
- `papers/anthropic_context_engineering_notes.md` as the source notes / rationale
- a future Deep-specific implementation checklist or design doc if we create one
- the actual Deep prompt files when we revise wording, especially:
- `agent_arcs/deep_agent_arc/deep_agent_prompts.py`
- secondarily, the same principle also applies to `react_agent_arc` and `multi_agent_arc` prompt wording, but it is most central to `deep_agent_arc` because Deep depends more heavily on prompt-mediated context engineering behavior

### Right altitude

- System prompts should be clear, direct, and written at the right altitude.
- Anthropic’s wording here is important enough to preserve almost verbatim: the system prompt should sit in the Goldilocks zone between brittle over-specification and vague under-specification.
- Anthropic also explicitly says system prompts should use simple, direct language and present ideas at the right altitude for the agent.
- Anthropic describes two failure modes:
- brittle, over-hardcoded prompts that encode complex if-else style behavior
- vague, high-level prompts that fail to provide concrete guidance or assume too much shared context
- At one extreme, engineers hardcode complex, brittle logic into prompts to elicit exact agent behavior. Anthropic says this creates fragility and increases maintenance complexity over time.
- At the other extreme, engineers provide vague high-level guidance that fails to give the LLM concrete signals for desired outputs or falsely assumes shared context.
- The right prompt altitude is the balance point between those extremes:
- specific enough to steer behavior well
- flexible enough to remain robust and not become brittle maintenance debt
- Anthropic’s formulation is that the optimal altitude should be specific enough to guide behavior effectively, yet flexible enough to provide the model with strong heuristics rather than brittle scripts.

### Structural organization

- Anthropic recommends organizing prompts into clear sections, for example:
- `<background_information>`
- `<instructions>`
- `## Tool guidance`
- `## Output description`
- XML tags or Markdown headers can be used to mark boundaries.
- They also note formatting details may become less important as models improve, but structure still helps.

### Prompt development strategy

- Start with the minimal prompt that should work.
- Test it on the best available model.
- Inspect failures.
- Add clear instructions and examples only where failure modes justify them.

## Tool design lessons

### Tools are part of context engineering

- Tools are not only action interfaces; they are context interfaces.
- They determine how the agent can bring new information into context and how efficiently it can do so.
- Tool design should encourage token-efficient behavior.
- Tool outputs should also be token-efficient.

### Minimal, self-contained, unambiguous tools

- Tools should be:
- self-contained
- robust to error
- extremely clear in intended use
- minimally overlapping in functionality
- input parameters should be descriptive and unambiguous
- tool design should play to model strengths rather than imposing awkward formats or ambiguous choices

### Common failure mode: bloated tool sets

- Anthropic sees many agents fail because they are given too many overlapping tools.
- If a human engineer cannot clearly say which tool should be used in a given situation, the model is unlikely to do better.
- Minimal viable toolsets improve both execution quality and long-run context pruning/maintenance.

## Example selection lessons

- Anthropic still strongly recommends examples / few-shot prompting.
- But they explicitly warn against stuffing a prompt with a laundry list of edge cases.
- Instead, curate a set of diverse, canonical examples that efficiently portray expected behavior.
- Their phrasing: examples are the “pictures” worth a thousand words.

## Overall context advice

- Across prompts, tools, examples, and message history, context should be informative but tight.
- The agent should receive the highest-signal information, not the largest possible amount of information.

## Runtime retrieval and agentic search

## Shift from preloaded retrieval to just-in-time context

- Anthropic describes a shift from static pre-inference retrieval toward more agentic, runtime context acquisition.
- Traditional embedding-based retrieval can still be useful, but increasingly agents work better when they can fetch context `just in time`.

### Just-in-time context strategy

- Rather than loading all candidate data upfront, maintain lightweight references such as:
- file paths
- stored queries
- web links
- identifiers
- Then give the agent tools to load only the needed content at runtime.

### Example from Claude Code

- Claude Code can analyze large databases without loading full objects into context.
- It writes targeted queries, stores results, and uses utilities like `head` and `tail` to inspect only needed slices.
- This mirrors human cognition: humans rely on file systems, bookmarks, inboxes, and indexes instead of storing everything in working memory.

### Metadata as signal

- Lightweight references are useful not only for indirection, but because their metadata is informative.
- Examples of metadata cues:
- file paths
- folder hierarchies
- file names
- timestamps
- size/shape of artifacts
- Example given: `test_utils.py` in `tests/` implies something different from the same file in `src/core_logic/`.

### Progressive disclosure

- Autonomous retrieval enables progressive disclosure.
- Agents can discover context layer by layer through exploration.
- Each interaction yields signals that guide the next one.
- Examples:
- file size implies complexity
- naming conventions suggest purpose
- timestamps hint at recency/relevance
- This lets the agent keep only relevant subsets in working memory instead of drowning in exhaustive context.

### Tradeoff of runtime exploration

- It is slower than precomputed retrieval.
- It requires opinionated engineering so the model has:
- the right tools
- the right heuristics
- the right information landscape
- Without that guidance, the agent can waste context by:
- misusing tools
- following dead ends
- failing to identify key information

### Hybrid strategy

- Anthropic explicitly says a hybrid approach is often best.
- Some context can be dropped in up front for speed.
- Additional context can be retrieved autonomously at runtime for precision and adaptivity.
- Claude Code is given `CLAUDE.md` files up front, but uses `glob` and `grep` to navigate just in time.
- Anthropic suggests hybrid approaches may be especially useful for less dynamic domains like law or finance.

### Guiding advice

- Even as models improve, “do the simplest thing that works” remains Anthropic’s advice.

## Context engineering for long-horizon tasks

- Long-horizon tasks exceed the context window.
- They require maintaining coherence, context, and goal direction over long sequences of actions spanning tens of minutes or hours.
- Anthropic says simply waiting for larger context windows is not enough.
- Even very large contexts remain vulnerable to context pollution and relevance problems if you want strongest performance.

Anthropic highlights three key techniques:

- compaction
- structured note-taking
- multi-agent architectures

## Compaction

### Definition

- Compaction means summarizing a conversation that is nearing context limit and reinitializing a fresh context window with the summary.

### Role

- Anthropic describes compaction as the first lever for long-term coherence.
- Its goal is to preserve continuity with minimal performance degradation.

### Claude Code example

- Claude Code summarizes message history and preserves:
- architectural decisions
- unresolved bugs
- implementation details
- It discards:
- redundant tool outputs
- redundant messages
- The new context also includes the five most recently accessed files.

### What makes compaction hard

- The art is in deciding what to keep versus discard.
- Over-aggressive compaction can remove subtle but important details whose future relevance is not obvious yet.

### Anthropic’s recommended tuning method

- Tune compaction prompts on complex traces.
- Start by maximizing recall: ensure the summary captures every potentially relevant detail.
- Then improve precision by removing superfluous content.

### Safe low-touch form of compaction

- Clearing old tool results is one of the safest lightweight approaches.
- Once a tool result is deep in history, the raw result often does not need to remain verbatim in context.
- Anthropic notes tool result clearing is a productized feature on their platform.

## Structured note-taking / agentic memory

### Definition

- Structured note-taking means the agent regularly writes notes outside the context window and later reloads them.

### Benefits

- Persistent memory with low overhead
- Progress tracking across complex tasks
- Retention of dependencies, decisions, and critical state across many tool calls or resets

### Examples

- Claude Code maintaining a todo list
- a custom agent maintaining `NOTES.md`
- Claude Plays Pokemon developing maps, progress tallies, unlocked milestones, and battle strategy notes across very long sequences

### Important point

- Anthropic emphasizes that the memory structure can emerge naturally.
- The Pokémon example was not preprompted with a specific note schema, yet the model still learned useful memory patterns.

### Platform product implication

- Anthropic released a memory tool in beta to make it easier to store and consult information outside the context window through a file-based system.

## Sub-agent architectures

### Role in context engineering

- Subagents are not only a coordination pattern; they are a context-management pattern.
- Instead of one agent trying to hold the entire project state, specialized subagents work in clean, focused contexts.

### Pattern

- Main agent keeps a high-level plan.
- Subagents handle focused tasks or deep searches.
- A subagent may spend tens of thousands of tokens exploring.
- It returns only a condensed summary, often `1,000-2,000` tokens.
- This isolates detailed search context inside subagents while allowing the lead agent to focus on synthesis and decisions.

### Fit

- Anthropic cites this as especially effective for complex research tasks.
- It also directly connects this pattern to their multi-agent research system article.

## Choosing among compaction, notes, and subagents

- Anthropic says the right approach depends on task characteristics.
- Compaction is best when conversational continuity matters.
- Note-taking is best for iterative development with milestones and durable state.
- Multi-agent architectures are best when parallel exploration or focused independent contexts pay off.

## Article conclusion

- Context engineering is framed as the next major engineering discipline around LLMs.
- The key recurring principle remains:
- find the smallest set of high-signal tokens that maximizes the chance of the desired behavior
- Even as models improve and become more autonomous, context remains a precious finite resource.

## What we should incorporate into our Deep agent

## 1. Treat context as a budget, not as a dump

- Do not equate larger context with better performance.
- Keep only high-signal information in the main agent’s active context.
- Be aggressive about excluding bulky low-value content.
- Evaluate context additions by expected utility, not by “might be relevant.”

## 2. Keep prompts at the right altitude

- Avoid brittle over-hardcoded prompts that simulate code in natural language.
- Avoid vague generalities that leave too much unstated.
- Our Deep prompts should provide:
- strong heuristics
- explicit operating norms
- clear artifact expectations
- but should not try to fully script every branch of behavior

## 3. Structure prompts clearly

- Use explicit sections in Deep prompts.
- Good sections for our use case include:
- background / objective
- research standards
- tool guidance
- delegation rules
- artifact-writing rules
- citation rules
- stopping conditions
- review / self-check expectations

## 4. Prefer just-in-time retrieval over dumping everything up front

- Our Deep architecture should continue relying on filesystem references and targeted reads instead of loading full artifacts into the active context unless necessary.
- Subagents should work from references, not giant preloaded documents.
- The lead agent should retrieve specifics when needed rather than carry them continuously.

## 5. Exploit metadata and filesystem structure

- Paths, filenames, folders, and timestamps are useful semantic signals.
- We should maintain consistent artifact organization so agents can infer file purpose quickly.
- For example:
- `/report/` for final outputs
- `/tmp/review/` for audit/review artifacts
- `/tmp/evidence/` for bounded evidence artifacts if used
- `/conversation_history/` and `/large_tool_results/` for offloaded state

## 6. Keep the toolset minimal and non-overlapping

- Deep agents should not be given redundant tools with ambiguous scope.
- Every tool should have a distinct role.
- Tool descriptions should explicitly state:
- when to use it
- when not to use it
- expected output shape
- common failure handling

## 7. Make tool outputs token-efficient

- Prefer tools and workflows that return references, summaries, slices, or bounded outputs rather than giant blobs.
- Reading/searching in slices is better than loading full outputs into context.
- This is especially important for search extraction, citation checks, and large intermediate results.

## 8. Compaction should preserve plans, decisions, open issues, and obligations

- If we compact, the summary must preserve at minimum:
- architecture/research plan
- unresolved questions
- open evidence gaps
- citation obligations
- review obligations
- key file paths
- critical findings and constraints
- We should explicitly optimize compaction prompts for recall first, then precision.

## 9. Tool-result clearing is a safe default compaction strategy

- Old raw tool outputs should usually not stay verbatim in active context.
- The fact that a tool was run and the distilled outcome often matters more than the full payload.
- We should continue leaning on offloading and history files rather than carrying raw results indefinitely.

## 10. Structured note-taking should be first-class

- Deep agents should externalize progress and state deliberately.
- Valuable note types include:
- current plan
- completed work
- remaining work
- important evidence
- open contradictions
- source-quality concerns
- unresolved citation obligations
- These notes should be durable and reloadable after summarization or long sessions.

## 11. Subagents are a context-isolation mechanism, not just a decomposition mechanism

- The Deep architecture should keep detailed search context inside subagents and only return condensed summaries or structured handoffs to the main agent.
- This is one of the main context-engineering reasons to keep Deep distinct from shared-history supervisor patterns.

## 12. Choose context strategy by task type

- For back-and-forth continuity: compaction
- For iterative milestone-driven work: structured notes
- For broad research / parallel exploration: subagents
- Deep should combine all three, but the task should determine which mechanism dominates.

## 13. Hybrid retrieval is likely best

- Some context should be available immediately up front.
- Other context should be navigated and loaded at runtime.
- For our setup, system prompt + key run metadata + artifact conventions up front, then selective file/search retrieval during execution, is consistent with Anthropic’s advice.

## 14. Simpler still wins

- Even with sophisticated Deep architecture, the article repeatedly points toward restraint.
- We should only add context-management machinery when it clearly improves outcomes.
- Each additional memory artifact, prompt section, tool, or retrieval step should justify itself.

## Concrete checklist for our Deep architecture

- Keep the main agent’s active context small and high-signal.
- Preserve plans and unresolved obligations across summarization.
- Ensure subagents return compressed handoffs, not raw giant dumps.
- Keep filesystem and artifact conventions clean and semantically meaningful.
- Avoid overlapping tools and unclear tool descriptions.
- Prefer targeted reads/searches over whole-document loads.
- Clear or offload stale bulky tool results.
- Maintain structured durable notes for long-running work.
- Tune summarization/compaction prompts on real hard traces.
- Use hybrid context loading rather than assuming everything should be preloaded.

## Relevance to our benchmark thesis

- This article strongly supports the idea that architecture quality is not just about “which agent pattern” but about how context is curated over time.
- It provides direct theoretical support for evaluating Deep-style context engineering separately from simpler ReAct or shared-history multi-agent setups.
- It also suggests that long-running research performance should be sensitive to:
- summarization quality
- note persistence
- context isolation
- retrieval strategy
- artifact structure
- tool output design
- That makes these dimensions legitimate explanatory variables in our thesis, not implementation trivia.

## Source

- Anthropic, `Effective context engineering for AI agents`, `https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents`
