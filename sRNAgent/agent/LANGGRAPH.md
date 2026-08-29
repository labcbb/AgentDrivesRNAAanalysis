# LangGraph / LangChain integration

## Phase 1 — top-level routing

```text
START → route (IntentRouter) → answer | plan → END
```

Files: `lc_graph.py`, `orchestrator.py`

## Phase 2 — inner tool-loop

```text
START → model → (tools → model)* → END
```

File: `lc_tool_loop.py` (wired via `SRNAgent._tool_loop`)

## Phase 3 — plan step DAG (this update)

```text
START → pick → execute → after_step → pick … → END
```

File: `lc_plan_graph.py`

- Plan **create / restore / amend / scope repair** stay in `PlanOrchestrator.run`
- Only the step execution loop is a LangGraph state machine
- Still calls `_execute_step`, `_replan`, approval prompts, persist/checkpoint helpers

```text
UI → LangGraph route
        ├─ answer → LC tool-loop
        └─ plan  → PlanOrchestrator.prepare
                      └─ LC plan step graph
                            └─ per-step LC tool-loop
```

## Toggles

```bash
export SRNAGENT_USE_LANGGRAPH=1          # phase 1 routing (default on)
export SRNAGENT_USE_LC_TOOL_LOOP=1       # phase 2 tool-loop (default on)
export SRNAGENT_USE_LC_PLAN_GRAPH=1      # phase 3 plan steps (default on)

# fall back any layer
export SRNAGENT_USE_LANGGRAPH=legacy
export SRNAGENT_USE_LC_TOOL_LOOP=legacy
export SRNAGENT_USE_LC_PLAN_GRAPH=legacy
```

Restart `serve.py` after changing flags.

## Still not migrated

- Planner LLM JSON synthesis (`_create_plan` / `_replan` prompts) — domain-heavy, kept as methods
- Supervisor branch chat
- Session JSON schema
- Tools/* and skills/* (intentionally unchanged)
