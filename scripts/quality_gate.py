import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / 'clawd' / 'DevOps_Practice' / 'tools' / 'quality_gate'))
from quality_gate import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
