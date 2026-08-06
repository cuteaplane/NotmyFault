import os
import sys

# notmyfault/tests 位于包内，需把项目根加入 sys.path 才能导入 notmyfault 包
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
