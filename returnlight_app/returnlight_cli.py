"""Returnlight entry point: never move money without explicit merchant approval."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

from .returnlight import (
    CaseError, OutcomeUnknown, OllamaReviewer, approve_case, approval_phrase,
    case_snapshot, execute_case, new_case, real_gateway, refresh_case, review_case, human_review_case
)


def parser():
    p = argparse.ArgumentParser(description="Returnlight: source-grounded merchant refund review")
    group = p.add_subparsers(dest="command", required=True)
    create = group.add_parser("create")
    create.add_argument("file")
    create.add_argument("--capture", required=True)
    create.add_argument("--ceiling", required=True)
    create.add_argument("--complaint", required=True)
    for name in ("show", "review", "approve", "execute", "refresh"):
        cmd = group.add_parser(name)
        cmd.add_argument("file")
        if name == "review":
            cmd.add_argument("--model", default="qwen2.5:3b")
            cmd.add_argument("--confirm-current", action="store_true", help="Confirm the complete current request and conditions after reading the case")
            cmd.add_argument("--case-revision", type=int)
        if name == "approve":
            cmd.add_argument("--amount", required=True)
            cmd.add_argument("--confirm", required=True)
        if name == "refresh":
            cmd.add_argument("--refund-id")
            cmd.add_argument("--confirm-capture-link", action="store_true")
    group.add_parser("demo", help="Offline rehearsal with synthetic model and provider." )
    return p


def demo():
    class FixtureModel:
        def review(self, complaint):
            return {"category": "damaged", "summary": "Cracked mug handle reported.",
                    "quote": "the handle arrived cracked", "recommended_percent": 50,
                    "reason": "Evidence supports discussing a partial refund."}

    class FixtureGateway:
        def capture(self, capture_id):
            return {"id": capture_id, "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "42.00"}}
        def refund(self, capture_id, cents, request_id):
            return {"id": "FAKEREFUND000001", "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": f"{cents / 100:.2f}"}}

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "case.json"
        case = new_case(path, "FAKECAPTURE00001", "42.00",
                        "I ordered a ceramic mug but the handle arrived cracked on delivery.")
        case = review_case(path, FixtureModel())
        case = human_review_case(path, case["complaint"], True, expected_case_revision=len(case["history"]))
        case = approve_case(path, "21.00", approval_phrase(case, "21.00"))
        case = execute_case(path, FixtureGateway())
        print("SYNTHETIC REHEARSAL: NO MODEL OR PAYPAL PROVIDER CALLED")
        print(json.dumps({"status": case["state"], "approved_usd": "21.00",
                          "refund_id": case["refund_id"], "history": case["history"]}, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            demo()
            return 0
        if args.command == "create":
            result = new_case(args.file, args.capture, args.ceiling, args.complaint)
        elif args.command == "show":
            result = case_snapshot(args.file)
        elif args.command == "review":
            if args.confirm_current:
                case = case_snapshot(args.file)
                latest = (case.get("buyer_followups") or [{"text": case["complaint"]}])[-1]["text"]
                result = human_review_case(args.file, latest, True, expected_case_revision=args.case_revision)
            else:
                result = review_case(args.file, OllamaReviewer(model=args.model))
        elif args.command == "approve":
            result = approve_case(args.file, args.amount, args.confirm)
        elif args.command == "execute":
            result = execute_case(args.file, real_gateway())
        elif args.command == "refresh":
            result = refresh_case(args.file, real_gateway(), args.refund_id, args.confirm_capture_link)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OutcomeUnknown as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    except (CaseError, RuntimeError, OSError, ValueError) as exc:
        print(f"Returnlight refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
