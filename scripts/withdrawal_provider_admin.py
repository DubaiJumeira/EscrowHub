from __future__ import annotations

import argparse
import json
import os

from withdrawal_provider_service.app import ProviderStore, ValidationError


def main() -> None:
    parser = argparse.ArgumentParser(description="EscrowHub withdrawal provider admin tool")
    parser.add_argument("command", choices=["list", "set-status"], help="admin command")
    parser.add_argument("--provider-ref", help="provider_ref to update")
    parser.add_argument("--status", help="new provider status")
    parser.add_argument("--txid", help="on-chain txid for broadcasted/confirmed")
    parser.add_argument("--message", help="operator message")
    parser.add_argument("--external-status", help="external status label")
    parser.add_argument("--metadata-json", help="optional metadata JSON object")
    parser.add_argument("--filter-status", help="list only one status")
    parser.add_argument("--limit", type=int, default=20, help="row limit")
    parser.add_argument("--db-path", default=os.getenv("WITHDRAWAL_PROVIDER_DB_PATH", "/var/lib/escrowhub-withdrawal-provider/provider.db"))
    args = parser.parse_args()

    store = ProviderStore(args.db_path)

    if args.command == "list":
        for row in store.list_records(status=args.filter_status, limit=args.limit):
            print(
                json.dumps(
                    {
                        "provider_ref": row.provider_ref,
                        "withdrawal_id": row.withdrawal_id,
                        "asset": row.asset,
                        "amount": row.amount,
                        "status": row.status,
                        "external_status": row.external_status,
                        "txid": row.txid,
                        "message": row.message,
                        "submitted_at": row.submitted_at,
                        "broadcasted_at": row.broadcasted_at,
                    },
                    sort_keys=True,
                )
            )
        return

    if not args.provider_ref or not args.status:
        raise SystemExit("set-status requires --provider-ref and --status")
    metadata = None
    if args.metadata_json:
        metadata = json.loads(args.metadata_json)
        if not isinstance(metadata, dict):
            raise SystemExit("--metadata-json must decode to an object")
    try:
        row = store.update_status(
            args.provider_ref,
            status=args.status,
            txid=args.txid,
            message=args.message,
            external_status=args.external_status,
            metadata=metadata,
        )
    except ValidationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"provider_ref": row.provider_ref, "status": row.status, "txid": row.txid, "external_status": row.external_status}, sort_keys=True))


if __name__ == "__main__":
    main()
