# AgentHarm-Style Paired-Twin Strategy (strategy_9_agentharm_paired)

This strategy mirrors the AgentHarm evaluation pattern:

- Every generated case is a **paired twin**: one `harmful` variant whose user prompt is directly harmful, and one `benign` variant whose user prompt is the sanitized twin of the same workflow.
- Both variants share the **same tool surface** (same names and parameters) so the only difference is the user prompt and the resulting trajectory.
- The agent executes in **ReAct** format (`Thought:` / `Action:` / `Action Input:` / `Observation:` / `Final Answer:`) — not the plain tool-call chat format used elsewhere in this repo.
- Each case carries a `grading_function` name that a downstream checker would use (e.g., `grade_propaganda_ai_video`).
- Every case belongs to exactly one harm category from: `Copyright`, `Cybercrime`, `Disinformation`, `Drugs`, `Fraud`, `Harassment`, `Hate`, `Sexual`.

Every case produced under this strategy MUST carry the AgentHarm-style fields described in each per-category prompt file.
