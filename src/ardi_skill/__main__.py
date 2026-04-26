"""Allow `python -m ardi_skill` to dispatch to the reference agent's CLI.

This is the same entry as the `ardi-agent` console script defined in
pyproject.toml; both call `ardi_skill.agent.main()`.
"""
from .agent import main

if __name__ == "__main__":
    main()
