| item | source file:lines | PORTED/ADAPTED | divergence summary |
|---|---|---|---|
| `BATS_V17_4_SYSTEM_INSTRUCTION` | `budget-aware-agent/src/prompts_bats.py:120-247` | ADAPTED | Replaced web search/browse domain wording with repository search/file inspection wording; retained section order, tags, thresholds, and every numeric limit. Original phrases are adjacent comments. |
| `BATS_BUDGET_BLOCK_TEMPLATE` | `budget-aware-agent/src/agent_budget_tracker.py:90-96` | PORTED | None; retains the leading/trailing newlines, four Used/Remaining fields, and resource-use exhortation. |
| `BATS_PLAN_DISCIPLINE` | `budget-aware-agent/src/prompts_bats.py:228-234` | PORTED | None. |
| `BATS_NO_QUERY_BUDGET_LEFT` | `budget-aware-agent/src/agent_bats.py:453-458` | PORTED | None. |
| `BATS_NO_URL_BUDGET_LEFT` | `budget-aware-agent/src/agent_bats.py:584-589` | PORTED | None. |
| `PR_PROMPT_GENERATE_FINAL_ANSWER` | `budget-aware-agent/src/prompts_bats.py:278-288` | PORTED | Ports the value returned by `pr_prompt_generate_final_answer`. |
| `PR_CHAT_GENERATE_FINAL_ANSWER_LAST_ROUND` | `budget-aware-agent/src/prompts_bats.py:270-275` | PORTED | Ports the actual last-round prompt used by the BATS exhaustion path. |
| `BATS_REMAINING_BUDGET_CONTINUATION_NUDGE` | `budget-aware-agent/src/agent_bats.py:358-362` | PORTED | None. |
| `BATS_NO_TOOL_CONTINUATION_NUDGE` | `budget-aware-agent/src/agent_bats.py:641-648` | PORTED | None; retained separately from the remaining-budget variant. |
| `BATS_CNT_NO_TOOL_TRIGGER` | `budget-aware-agent/src/agent_bats.py:643-645` | PORTED | Exposes the `cnt_no_tool >= 1` trigger value. |
| `BATS_NONE_ANSWER_THRESHOLD_DEFAULT` | `budget-aware-agent/src/agent_bats.py:850` | PORTED | Exposes the parser default of 3. |
| `PR_CHAT_VERIFICATION` | `budget-aware-agent/src/prompts_bats.py:322-386` | PORTED | None; includes the complete SUCCESS/CONTINUE/PIVOT JSON contract. |
| `BATS_OLD_TOOL_RESPONSE_ELISION_MARKER` | `budget-aware-agent/src/agent_bats.py:597-606` | PORTED | None. |
| `SUCCESSFUL_SI` | `reasoning-bank/third_party/src/minisweagent/memory/instruction.py:16-38` | PORTED | None; selected the coding-domain minisweagent variant. |
| `FAILED_SI` | `reasoning-bank/third_party/src/minisweagent/memory/instruction.py:40-62` | PORTED | None; selected the coding-domain minisweagent variant. |
| `MEMORY_ITEM_SCHEMA` | `reasoning-bank/third_party/src/minisweagent/memory/instruction.py:33-36` | PORTED | Extracted the shared Markdown item body as a standalone constant; text is unchanged. |
| `MAX_MEMORY_ITEMS` | `reasoning-bank/third_party/src/minisweagent/memory/instruction.py:23-26,47-50` | PORTED | Exposes the shared at-most-three limit as an integer. |
| `INSTRUCTION_AWARE_QUERY_FORMAT` | `reasoning-bank/third_party/src/minisweagent/memory/memory_management.py:123-124` | PORTED | Exposes the source f-string values as named format placeholders; literal labels and newline are unchanged. |
| `AUTOEVAL_TRAJECTORY_JUDGE_SYSTEM_PROMPT` | `reasoning-bank/WebArena/autoeval/prompts.py:111-133` | PORTED | None; retains the strictness rules and two-line success/failure contract. |
| `AUTOEVAL_TRAJECTORY_JUDGE_PROMPT` | `reasoning-bank/WebArena/autoeval/prompts.py:134-146` | PORTED | Exposes runtime f-string values as named format placeholders; callers must supply `N/A` when the response is absent. |
| `AUTOEVAL_STATUS_PARSE_CONTRACT` | `reasoning-bank/WebArena/autoeval/prompts.py:128-133`; `reasoning-bank/WebArena/autoeval/evaluator.py:94-97` | PORTED | Extracted the two output lines used by the parser; text is unchanged. |
| `MATTS_CONTRASTIVE_SCALING_SI` | `reasoning-bank/WebArena/prompts/memory_instruction.py:63-90`; `reasoning-bank/WebArena/induce_scaling.py:193-196` | PORTED | Stretch item; prompt is defined in `memory_instruction.py` and imported/called by `induce_scaling.py`. |
| `MATTS_SCALING_QUERY_FORMAT` | `reasoning-bank/WebArena/induce_scaling.py:188-189` | PORTED | Stretch item; exposes the source f-string query value as a named format placeholder. |
| `MATTS_SCALING_TRAJECTORY_FORMAT` | `reasoning-bank/WebArena/induce_scaling.py:190-191` | PORTED | Stretch item; exposes the source f-string sample number and joined steps as named format placeholders. |
