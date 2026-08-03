from __future__ import annotations

import argparse
import json
from pathlib import Path

from cn_fund_strategy.application.g6_dynamic_forward import (
    build_g6_dynamic_plan,
    capture_dynamic_input_batch,
    render_g6_dynamic_brief,
    validate_g6_dynamic_plan,
    write_g6_dynamic_plan,
)
from cn_fund_strategy.application.g6_forward_shadow import write_create_or_identical


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture current public inputs and create a dynamic G6 research plan"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture-input", help="capture one immutable read-only input batch")
    capture.add_argument("--output-root", type=Path, required=True)
    capture.add_argument("--received-at", required=True)
    capture.add_argument("--timeout-seconds", type=int, default=30)

    plan = commands.add_parser("plan", help="create a post-close next-session research plan")
    plan.add_argument("--project-root", type=Path, required=True)
    plan.add_argument("--contract", type=Path, required=True)
    plan.add_argument("--frozen-result", type=Path, required=True)
    plan.add_argument("--input-manifest", type=Path, required=True)
    plan.add_argument("--plan-for-session", required=True)
    plan.add_argument("--information-cutoff-at", required=True)
    plan.add_argument("--created-at", required=True)
    plan.add_argument("--previous-record", type=Path)
    plan.add_argument("--output-json", type=Path, required=True)
    plan.add_argument("--output-markdown", type=Path, required=True)

    verify = commands.add_parser("verify", help="verify a stored R2 plan")
    verify.add_argument("--record", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture-input":
        manifest = capture_dynamic_input_batch(
            output_root=args.output_root,
            received_at=args.received_at,
            timeout_seconds=args.timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "manifest": str((args.output_root / "manifest.json").resolve()),
                    "batch_status": manifest["batch_status"],
                    "errors": manifest["errors"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if manifest["batch_status"] == "signal-input-ready" else 2
    if args.command == "plan":
        record = build_g6_dynamic_plan(
            project_root=args.project_root,
            operation_contract_path=args.contract,
            frozen_result_path=args.frozen_result,
            input_manifest_path=args.input_manifest,
            plan_for_session=args.plan_for_session,
            information_cutoff_at=args.information_cutoff_at,
            created_at=args.created_at,
            previous_record_path=args.previous_record,
        )
        write_g6_dynamic_plan(args.output_json, record)
        markdown = render_g6_dynamic_brief(record).encode("utf-8")
        write_create_or_identical(args.output_markdown, markdown)
        print(
            json.dumps(
                {
                    "record": str(args.output_json.resolve()),
                    "brief": str(args.output_markdown.resolve()),
                    "record_hash": record["record_hash"],
                    "plan_for_session": record["plan_for_session"],
                    "economic_order_count": record["reconciliation"]["expected_model_orders"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    record = json.loads(args.record.read_text(encoding="utf-8"))
    validate_g6_dynamic_plan(record)
    print(json.dumps({"record": str(args.record.resolve()), "record_hash": record["record_hash"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
