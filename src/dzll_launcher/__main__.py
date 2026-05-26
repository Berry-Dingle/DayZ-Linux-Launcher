import os

os.environ.setdefault("GSK_RENDERER", "gl")

from .app import main

if __name__ == "__main__":
    main()
