import logging
import os

os.environ.setdefault("GSK_RENDERER", "gl")

if os.environ.get("DZLL_DEBUG_JOIN") == "1":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

from .app import main

if __name__ == "__main__":
    main()
