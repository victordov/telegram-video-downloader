#!/usr/bin/env python3
"""
Conventional entry point: ``python app.py`` starts the bot.

This is an alias for ``run_bot.py`` (pre-flight checks, single-instance lock,
then the bot itself), for hosts and habits that expect an ``app.py``.
"""

from run_bot import main

if __name__ == "__main__":
    main()
