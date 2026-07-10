import sys
from pathlib import Path

# bid-service ルート(main.py, modules/ と同じ階層)をsys.pathに追加する。
# pytestの実行場所やimportmodeに依存せず `import modules...` / `import main` を
# 可能にするため、ルート直下のconftest.pyで行う。
sys.path.insert(0, str(Path(__file__).resolve().parent))
