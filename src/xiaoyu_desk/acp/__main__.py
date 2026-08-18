"""Allow ``python -m xiaoyu_desk.acp`` alongside the console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
