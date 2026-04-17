"""Central config — paths, agent registry, env vars."""
from pathlib import Path
import os
import tomllib
from dataclasses import dataclass

# Paths
HOME = Path.home()
RANCH_HOME = HOME / ".ranch"
RANCH_HOME.mkdir(exist_ok=True)
DB_PATH = RANCH_HOME / "ranch.db"
CONFIG_FILE = RANCH_HOME / "config.toml"
LOG_DIR = RANCH_HOME / "logs"
LOG_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.environ.get("RANCH_DATABASE_URL", f"sqlite:///{DB_PATH}")


@dataclass
class Agent:
    name: str
    worktree: Path
    description: str = ""


def _load_agents() -> dict[str, Agent]:
    """Load agent registry from ~/.ranch/config.toml. Returns empty dict if not found."""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)
    agents = {}
    for name, cfg in data.get("agents", {}).items():
        agents[name] = Agent(
            name=name,
            worktree=Path(cfg["worktree"]).expanduser(),
            description=cfg.get("description", ""),
        )
    return agents


def _default_config_toml() -> str:
    """Generate a starter config.toml for the user to edit."""
    return """\
# Ranch agent registry
# Add one [[agents.*]] section per Claude Code worktree you want to track.

# [agents.my-agent]
# worktree = "/path/to/worktree"
# description = "Optional label"
"""


def write_default_config():
    """Write a starter config.toml if one doesn't exist yet."""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(_default_config_toml())


AGENTS: dict[str, Agent] = _load_agents()


def reload_agents() -> dict[str, Agent]:
    """Re-read config.toml and update the global AGENTS registry."""
    global AGENTS
    AGENTS = _load_agents()
    return AGENTS


def agent_for_cwd(cwd: Path) -> Agent | None:
    """Detect which agent is making a request based on the current directory."""
    agents = _load_agents()  # always fresh read so hooks pick up config changes
    cwd = cwd.resolve()
    for agent in agents.values():
        try:
            cwd.relative_to(agent.worktree.resolve())
            return agent
        except ValueError:
            continue
    return None


# Anthropic API key (read by claude-code-sdk automatically)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
