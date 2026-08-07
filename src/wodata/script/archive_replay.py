"""Compatibility wrapper for :mod:`wodata.script.fetch_replay_data`.

Use ``python -m wodata.script.fetch_replay_data`` for new invocations.
"""

from wodata.script.fetch_replay_data import *  # noqa: F403
from wodata.script.fetch_replay_data import main


if __name__ == "__main__":
    main()
