"""Central config — paths, agent registry, env vars."""
from pathlib import Path
import os
from dataclasses import dataclass

# Paths
HOME = Path.home()
RANCH_HOME = HOME / ".ranch"
RANCH_HOME.mkdir(exist_ok=True)
DB_PATH = RANCH_HOME / "ranch.db"
DATABASE_URL = os.environ.get("RANCH_DATABASE_URL", f"sqlite:///{DB_PATH}")

CITEMED_ROOT = Path(os.environ.get("CITEMED_ROOT", HOME / "code" / "citemed"))


# Agent registry — the three worktrees
@dataclass
class Agent:
    name: str
    worktree: Path
    description: str = ""


AGENTS: dict[str, Agent] = {
    "max":    Agent("max",    CITEMED_ROOT / "max",    "Ranch hand #1"),
    "jeffy":  Agent("jeffy",  CITEMED_ROOT / "jeffy",  "Ranch hand #2"),
    "arnold": Agent("arnold", CITEMED_ROOT / "arnold", "Ranch hand #3"),
}


def agent_for_cwd(cwd: Path) -> Agent | None:
    """Detect which agent is making a request based on the current directory."""
    cwd = cwd.resolve()
    for agent in AGENTS.values():
        try:
            cwd.relative_to(agent.worktree.resolve())
            return agent
        except ValueError:
            continue
    return None


# Anthropic API key (read by claude-code-sdk automatically)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
