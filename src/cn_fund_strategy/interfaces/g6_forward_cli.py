from __future__ import annotations

import argparse
import json
from pathlib import Path

from cn_fund_strategy.application.g6_forward_shadow import (
    build_g6_forward_plan,
    capture_current_status_batch,
    render_g6_trade_brief,
    validate_g6_forward_plan,
    write_create_or_identical,
    write_g6_forward_plan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture current fund status and generate a G6 research-only forward plan"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture-status", help="capture read-only public product status")
    capture.add_argument("--output-root", type=Path, required=True)
    capture.add_argument("--fund-code", action="append", dest="fund_codes")

    plan = commands.add_parser("plan", help="build an immutable next-session research plan")
    plan.add_argument("--project-root", type=Path, required=True)
    plan.add_argument("--contract", type=Path, required=True)
    plan.add_argument("--frozen-result", type=Path, required=True)
    plan.add_argument("--status-manifest", type=Path, required=True)
    plan.add_argument("--plan-for-session", required=True)
    plan.add_argument("--information-cutoff-at", required=True)
    plan.add_argument("--created-at", required=True)
    plan.add_argument("--previous-record", type=Path)
    plan.add_argument("--output-json", type=Path, required=True)
    plan.add_argument("--output-markdown", type=Path, required=True)

    verify = commands.add_parser("verify", help="verify a stored plan hash and invariants")
    verify.add_argument("--record", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture-status":
        kwargs: dict[str, object] = {"output_root": args.output_root}
        if args.fund_codes:
            kwargs["fund_codes"] = tuple(args.fund_codes)
        manifest = capture_current_status_batch(**kwargs)  # type: ignore[arg-type]
        print(
            json.dumps(
                {
                    "manifest": str((args.output_root / "manifest.json").resolve()),
                    "row_count": len(manifest["rows"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "plan":
        record = build_g6_forward_plan(
            project_root=args.project_root,
            observation_contract_path=args.contract,
            frozen_result_path=args.frozen_result,
            status_manifest_path=args.status_manifest,
            plan_for_session=args.plan_for_session,
            information_cutoff_at=args.information_cutoff_at,
            created_at=args.created_at,
            previous_record_path=args.previous_record,
        )
        write_g6_forward_plan(args.output_json, record)
        markdown = render_g6_trade_brief(record).encode("utf-8")
        write_create_or_identical(args.output_markdown, markdown)
        print(
            json.dumps(
                {
                    "record": str(args.output_json.resolve()),
                    "brief": str(args.output_markdown.resolve()),
                    "record_hash": record["record_hash"],
                    "economic_order_count": sum(
                        row["direction"] != "HOLD" for row in record["order_plan"]
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    record = json.loads(args.record.read_text(encoding="utf-8"))
    validate_g6_forward_plan(record)
    print(json.dumps({"record": str(args.record.resolve()), "status": "valid"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
