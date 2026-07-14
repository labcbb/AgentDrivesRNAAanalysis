# sRNAgent

Agent-driven small RNA-seq analysis toolkit.

## Environment

sRNAgent detects the **active conda environment** and runs `execute_code` in a **Jupyter kernel** (`conda-env-{env}-py`), same pattern as omicverse.

```bash
conda env create -f conda_env.yml
conda env update -f conda_env.yml --prune
conda activate srnagent
python -m ipykernel install --user --name srnagent

# Start UI backend inside the same env
cd ui && python3 serve.py --work_space /path/to/your/project
```

Expected env name defaults to `srnagent` (override with `SRNAGENT_CONDA_ENV`).

Execution modes:
- **notebook** (default): persistent Jupyter kernel with auto-recovery (interrupt → restart → new session)
- **in-process**: fallback when notebook init/execution fails (`WARN_AND_FALLBACK` policy)
- **RAISE**: set `sandbox_fallback_policy=SandboxFallbackPolicy.RAISE` to disable fallback

Check runtime:

```bash
curl http://127.0.0.1:8765/api/agent/status
```

## Setup

```bash
conda env create -f conda_env.yml
conda activate srnagent
```

## Function registry

Functions are registered via `@register_function` and discovered automatically:

```python
import sRNAgent as sa

sa.list_functions()
sa.find_function("fastq download")
sa.fastq.fastq_dl("SRR26304152", output_dir="srna_fastq")
```

## Skill registry

Skills live under `sRNAgent/skills/*/SKILL.md`. Workspace overrides:

- `./skills/*/SKILL.md`
- `./.claude/skills/*/SKILL.md`

```python
from sRNAgent.skill_registry import build_skill_registry

registry = build_skill_registry()
print(list(registry.skill_metadata.keys()))  # ['fastq-dl-srna', 'fastq-qc']
```

## Web UI

Static chat UI + LLM config lives under `ui/`:

```bash
conda activate srnagent
cd /path/to/AgentDrivesRNAAanalysis/ui
python3 serve.py
# Open http://127.0.0.1:8765/index.html
```

### serve.py options

| Option | Description |
|--------|-------------|
| `--local` | Bind `127.0.0.1` only (localhost) |
| `--lan` | Bind `0.0.0.0` (allow access via server IP) |
| `--host HOST` | Custom bind address (default: `0.0.0.0`) |
| `--port PORT` | HTTP port (default: `8765`, or `UI_PORT` env) |
| `--work_space PATH` | Agent workspace directory — downloads, data processing, and code execution use this path as cwd (default: the directory where you run `serve.py`) |

Examples:

```bash
# Local only
python3 serve.py --local

# LAN / remote access via server IP
python3 serve.py --lan
# Open http://<server-ip>:8765/index.html

# Isolated workspace (recommended for production runs)
mkdir -p ~/srnagent-runs/project1
python3 serve.py --lan --work_space ~/srnagent-runs/project1

# Custom port (e.g. when 8765 is already in use)
python3 serve.py --lan --port 8877 --work_space /path/to/your/project
# Open http://<server-ip>:8877/index.html
```

Environment variables (optional):

- `UI_PORT` — default HTTP port when `--port` is omitted (default `8765`)
- `UI_HOST` — default bind host when `--host` is not set
- `UI_WORK_SPACE` — same as `--work_space`

When using `--work_space`, all Agent file I/O (FASTQ download, reference genome, pipeline outputs, etc.) is confined to that directory. Jupyter kernel metadata still lives under `~/.srnagent/chat_kernels/`.

The UI status bar shows the active workspace path (`ws: ...`). After changing `--work_space`, restart `serve.py` and start a new chat for a clean kernel cwd.

Remote dev (SSH port forward): forward the same port you pass to `--port` (default `8765`), e.g. `ssh -L 8765:127.0.0.1:8765 user@server`, then open `http://127.0.0.1:8765/index.html` locally. If you use `--port 8877`, forward `8877` instead. Config in the browser is per-origin — `127.0.0.1` and server IP each have separate localStorage; re-save API keys when switching URLs.

## Agent

The agent connects both registries to LLM tools (`search_functions`, `search_skills`, `execute_code`, `finish`):

```bash
export SRNAGENT_API_KEY="your-minimax-or-openai-key"
# optional:
# export SRNAGENT_BASE_URL="https://api.minimax.chat/v1"
# export SRNAGENT_MODEL="MiniMax-M2.5-highspeed"

cd /path/to/AgentDrivesRNAAanalysis
python -m sRNAgent.agent --status
python -m sRNAgent.agent "帮我查一下有哪些 sRNA 下载相关的 skill 和函数"
```

```python
from sRNAgent import SRNAgent

agent = SRNAgent()
print(agent.status())
answer = agent.run("列出已注册的 skill 和 fastq 相关函数")
print(answer)
```
