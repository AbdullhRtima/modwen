import sys

from modwen.cli import main

if __name__ == "__main__":  # the speaker-detection worker process imports this module; it must not start the app
    sys.exit(main())
