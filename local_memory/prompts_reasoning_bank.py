"""Pinned ReasoningBank prompt ports audited through the Synth CLI manifest."""

# Source: reasoning-bank | third_party/src/minisweagent/memory/instruction.py:16-38 | commit ed80611 | PORTED (verbatim).
SUCCESSFUL_SI = """You are an expert in coding, specifically fixing a given issue in a code repository. You will be given an issue to be fixed, the corresponding trajectory that represents **how an agent successfully resolved the issue**.

## Guidelines
You need to extract and summarize useful insights in the format of memory items based on the agent's successful trajectory.
The goal of summarized memory items is to be helpful and generalizable for future similar tasks.

## Important notes
  - You must first think why the trajectory is successful, and then summarize the insights.
  - You can extract *at most 3* memory items from the trajectory.
  - You must not repeat similar or overlapping items.
  - Do not mention specific websites, queries, or string contents, but rather focus on the generalizable insights.

## Output Format
Your output must strictly follow the Markdown format shown below:

```
# Memory Item i
## Title <the title of the memory item>
## Description <one sentence summary of the memory item>
## Content <1-3 sentences describing the insights learned to successfully resolve the issue in the future>
```"""


# Source: reasoning-bank | third_party/src/minisweagent/memory/instruction.py:40-62 | commit ed80611 | PORTED (verbatim).
FAILED_SI = """You are an expert in coding, specifically fixing a given issue in a code repository. You will be given a user query, the corresponding trajectory that represents **how an agent attempted to resolve the issue but failed**.

## Guidelines
You need to extract and summarize useful insights in the format of memory items based on the agent's failed trajectory.
The goal of summarized memory items is to be helpful and generalizable for future similar tasks.

## Important notes
  - You must first reflect and think why the trajectory failed, and then summarize what lessons you have learned or strategies to prevent the failure in the future.
  - You can extract *at most 3* memory items from the trajectory.
  - You must not repeat similar or overlapping items.
  - Do not mention specific websites, queries, or string contents, but rather focus on the generalizable insights.

## Output Format
Your output must strictly follow the Markdown format shown below:

```
# Memory Item i
## Title <the title of the memory item>
## Description <one sentence summary of the memory item>
## Content <1-3 sentences describing the insights learned to successfully resolve the issue in the future>
```"""


# Source: reasoning-bank | third_party/src/minisweagent/memory/instruction.py:33-36 | commit ed80611 | PORTED (verbatim).
MEMORY_ITEM_SCHEMA = """# Memory Item i
## Title <the title of the memory item>
## Description <one sentence summary of the memory item>
## Content <1-3 sentences describing the insights learned to successfully resolve the issue in the future>"""


# Source: reasoning-bank | third_party/src/minisweagent/memory/instruction.py:23-26,47-50 | commit ed80611 | PORTED (verbatim).
MAX_MEMORY_ITEMS = 3


# Source: reasoning-bank | third_party/src/minisweagent/memory/memory_management.py:123-124 | commit ed80611 | PORTED (verbatim).
# The source f-string's task_description and query values are exposed as named format placeholders.
INSTRUCTION_AWARE_QUERY_FORMAT = "Instruct: {task_description}\nQuery: {query}"


# Source: reasoning-bank | WebArena/autoeval/prompts.py:111-133 | commit ed80611 | ADAPTED (software-engineering evidence categories).
AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT = '''You are an expert in evaluating the performance of a software engineering agent. The agent is designed to help a human user inspect and modify a code repository. Given the user's intent, the agent's action history, the final observed repository and verification state, and the agent's response to the user, your goal is to decide whether the agent's execution is successful or not.

There are three types of tasks:
1. Information seeking: The user wants to obtain information from the repository, runtime, documentation, or tests. The agent's response must contain the requested information, or explicitly state that the information is not available. An exception or ungrounded guess is a failure. Be careful about sufficiency: if the task asks for an exhaustive result, the agent must inspect the complete relevant surface rather than a convenient subset.
2. Repository navigation and diagnosis: The user wants to locate, explain, or diagnose a specific behavior. Carefully examine the action history and observed evidence to determine whether the agent identified the correct code path and cause. A plausible explanation without evidence from the named system is a failure.
3. Content modification: The user wants to modify code, configuration, tests, or documentation. Carefully examine the action history, final diff, and test/build evidence to determine whether every requested change was completed without regressions. A claimed change without verification evidence is not sufficient.

*Strictness rules*
Before calling a task successful, verify all three:
- Completeness: every constraint in the intent is satisfied, and if the task implies an exhaustive result (a list, a range, an aggregate), the agent inspected the full source, not just the first page.
- Grounding: every factual claim, path, result, or reported behavior is traceable to a specific repository or runtime observation; inferred or guessed values without visible evidence count as failures.
- Right target: when the task names a specific package, file, interface, behavior, or acceptance criterion, confirm the agent acted on that exact target and not an adjacent one.
When uncertain on any of these, mark failure. A false success is more harmful than a false failure, because memory induction amplifies it into future behavior.

*IMPORTANT*
Format your response into two lines as shown below:

Thoughts: <your thoughts and reasoning process>"
Status: "success" or "failure"'''


# Source: reasoning-bank | WebArena/autoeval/prompts.py:134-146 | commit ed80611 | ADAPTED (repository trajectory fields).
# The source f-string's runtime values are exposed as named format placeholders; callers supply "N/A" for an absent response.
AUTOEVAL_TRAJECTORY_JUDGE_PROMPT = """User Intent: {intent}

Action History:
{last_actions}

The detailed final observed state of the repository and verification evidence:

```md
{cap}
```

Agent response to the user: {response}."""


# Source: reasoning-bank | WebArena/autoeval/prompts.py:128-133; WebArena/autoeval/evaluator.py:94-97 | commit ed80611 | PORTED (verbatim).
AUTOEVAL_STATUS_PARSE_CONTRACT = '''Thoughts: <your thoughts and reasoning process>"
Status: "success" or "failure"'''


# Source: reasoning-bank | WebArena/prompts/memory_instruction.py:63-90; WebArena/induce_scaling.py:193-196 | commit ed80611 | PORTED (verbatim). Stretch item.
MATTS_CONTRASTIVE_SCALING_SI = """You are an expert in web navigation. You will be given a user query and multiple trajectories showing how an agent attempted the task.
Some trajectories may be successful, and others may have failed.

## Guidelines
Your goal is to **compare and contrast** these trajectories to identify the most useful and generalizable strategies as memory items.
Use **self-contrast reasoning**:
  - Identify patterns and strategies that consistently led to success.
  - Identify mistakes or inefficiencies from failed trajectories and formulate preventative strategies.
  - Prefer strategies that generalize beyond specific pages or exact wording.

## Important notes
  - Think first: Why did some trajectories succeed while others failed?
  - You can extract *at most 5* memory items from all trajectories combined.
  - Do not repeat similar or overlapping items.
  - Do not mention specific websites, queries, or string contents — focus on generalizable behaviors and reasoning patterns.
  - Make sure each memory item captures **actionable** and **transferable** insights.

## Output Format
Your output must strictly follow the Markdown format shown below:

```
# Memory Item i
## Title <the title of the memory item>
## Description <one sentence summary describing when or when NOT to use the memory item>
## Content <1-5 sentences describing the insights learned to avoid such failures and successfully accomplishing similar tasks in the future>
```"""


# Source: reasoning-bank | WebArena/induce_scaling.py:188-189 | commit ed80611 | PORTED (verbatim). Stretch item.
# The source f-string's query value is exposed as a named format placeholder.
MATTS_SCALING_QUERY_FORMAT = "**Query:** {query}\n\n"


# Source: reasoning-bank | WebArena/induce_scaling.py:190-191 | commit ed80611 | PORTED (verbatim). Stretch item.
# The source f-string's sample number and joined steps are exposed as named format placeholders.
MATTS_SCALING_TRAJECTORY_FORMAT = "**Trajectory {sample_number} :**\n{steps}\n\n"
