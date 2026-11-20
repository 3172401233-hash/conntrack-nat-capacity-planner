"""命令行入口: python -m nat_planner <scenario.json>"""

import json
import sys

from .models import Scenario
from .simulator import Simulator


def main(argv):
    if len(argv) != 2:
        print("用法: python -m nat_planner <scenario.json>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as fh:
        scenario = Scenario.from_dict(json.load(fh))
    report = Simulator(scenario).run()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
