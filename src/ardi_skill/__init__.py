"""ardi-skill — agent SDK + reference miner for the Ardi WorkNet.

Public API:
  from ardi_skill import ArdiClient, CommitTicket, Riddle, CurrentEpoch
  from ardi_skill.agent import run_loop, ClaudeSolver

CLI:
  ardi-agent --help
  python -m ardi_skill --help
"""
from .sdk import ArdiClient, CommitTicket, CurrentEpoch, Riddle

__version__ = "0.2.0"
__all__ = ["ArdiClient", "CommitTicket", "CurrentEpoch", "Riddle", "__version__"]
