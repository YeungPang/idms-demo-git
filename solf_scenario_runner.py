import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import solf_parser
from solf_interpreter import SOLFInterpreter


DEFAULT_SCRIPT_PATH = Path(__file__).with_name("solf_script.txt")
ASSIGNMENT_RE = re.compile(r"\(\s*(_[A-Za-z0-9_]+)\s*[≔=]\s*")


@dataclass
class ScenarioResult:
    scenario: str
    expression: str
    variable: str | None
    value: Any


class SolfScenarioRunner:
    def __init__(self, script_path: Path | str = DEFAULT_SCRIPT_PATH) -> None:
        self.script_path = Path(script_path)
        self.interpreter = SOLFInterpreter()
        self.interpreter.set_parser(type("Parser", (object,), {"parse": staticmethod(solf_parser.parse_script)})())
        self._loaded = False

    def load(self, clear_existing: bool = True) -> dict[str, int]:
        script = self.script_path.read_text(encoding="utf-8")
        summary = self.interpreter.load_program_script(script, clear_existing=clear_existing)
        self._loaded = True
        return summary

    def list_scenarios(self) -> dict[str, str]:
        if not self._loaded:
            self.load(clear_existing=True)

        examples = self.interpreter.variables.get("regression_examples")
        if not isinstance(examples, dict):
            return {}

        normalized: dict[str, str] = {}
        for key, value in examples.items():
            normalized[str(key)] = str(value)
        return normalized

    def run_expression(self, expression: str) -> Any:
        if not self._loaded:
            self.load(clear_existing=True)

        ast = solf_parser.parse_script(expression)
        self.interpreter.execute_predicate(ast)

        assignment = ASSIGNMENT_RE.search(expression)
        if assignment:
            return self.interpreter.variables.get(assignment.group(1))
        return None

    def run_scenario(self, scenario_name: str) -> ScenarioResult:
        examples = self.list_scenarios()
        if scenario_name not in examples:
            available = ", ".join(sorted(examples.keys())) or "<none>"
            raise KeyError(f"Unknown scenario '{scenario_name}'. Available: {available}")

        expression = examples[scenario_name]
        value = self.run_expression(expression)

        assignment = ASSIGNMENT_RE.search(expression)
        variable = assignment.group(1) if assignment else None
        return ScenarioResult(
            scenario=scenario_name,
            expression=expression,
            variable=variable,
            value=value,
        )


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SOLF regression scenarios from solf_script.txt")
    parser.add_argument("--script", default=str(DEFAULT_SCRIPT_PATH), help="Path to SOLF script file")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available regression scenarios")

    run_parser = sub.add_parser("run", help="Run one named scenario")
    run_parser.add_argument("scenario", help="Scenario name from regression_examples")

    expr_parser = sub.add_parser("expr", help="Run a raw SOLF expression")
    expr_parser.add_argument("expression", help="Example: (_ok ≔ run_regression_checks())")

    return parser


def main() -> int:
    parser = _build_cli()
    args = parser.parse_args()

    runner = SolfScenarioRunner(script_path=args.script)
    summary = runner.load(clear_existing=True)

    if args.command == "list":
        print("load_summary", summary)
        for name, expr in sorted(runner.list_scenarios().items()):
            print(f"{name}: {expr}")
        return 0

    if args.command == "run":
        result = runner.run_scenario(args.scenario)
        print("load_summary", summary)
        print("scenario", result.scenario)
        print("expression", result.expression)
        print("result", result.value)
        return 0

    if args.command == "expr":
        value = runner.run_expression(args.expression)
        print("load_summary", summary)
        print("expression", args.expression)
        print("result", value)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
