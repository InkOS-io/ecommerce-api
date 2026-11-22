"""Inspect, prepare and submit a synthetic checkout. See README.md for commands."""

import argparse
import json
import os
import sys
import uuid
from urllib.parse import quote as encode

from storefront import Storefront, StorefrontError


def prepare(api, args):
    if len(args.line) > 50:
        raise ValueError("Use 1 to 50 lines.")
    quotes = []
    for spec in args.line:
        configuration, sep, raw_quantity = spec.partition("=")
        if not sep or not raw_quantity.isdigit():
            raise ValueError("Each --line must be CONFIGURATION=QUANTITY.")
        _, quote = api.call("POST", "/quotes", {
            "configurationId": configuration, "quantity": int(raw_quantity),
        })
        if quotes and quote["currency"] != quotes[0]["currency"]:
            raise ValueError("The shop currency changed. Prepare a new cart.")
        quotes.append(quote)

    reference = "sandbox-" + uuid.uuid4().hex
    cart = {
        "externalOrderId": reference,
        "customer": {"name": "Example Print Buyer", "email": "buyer@example.invalid"},
        "items": [{"quoteId": quote["quoteId"]} for quote in quotes],
        "paymentReference": "test-payment-" + reference,
    }
    checkout = {"key": reference, "body": cart, "quotes": quotes}
    # Never overwrite the only copy of a key still waiting for its receipt.
    with open(args.file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(checkout, stream, indent=2)
        stream.write("\n")
    return {"terms": quotes, "currency": quotes[0]["currency"],
            "agreedTotalMinor": sum(q["totalMinor"] for q in quotes), "savedTo": args.file}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="Test the connection and list published options.")
    prep = commands.add_parser("prepare", help="Show the terms and save an unsubmitted cart.")
    prep.add_argument("file")
    prep.add_argument("--line", action="append", required=True, metavar="CONFIGURATION=QUANTITY")
    verify = commands.add_parser("verify", help="Recheck each saved quote before buyer confirmation.")
    verify.add_argument("file")
    send = commands.add_parser("submit", help="Submit the same saved, accepted sandbox cart.")
    send.add_argument("file")
    args = parser.parse_args()
    api = Storefront.from_env()
    if args.command == "catalog":
        return {"connection": api.call("GET", "/connection")[1],
                "catalog": api.call("GET", "/configurations")[1]}
    if args.command == "prepare":
        return prepare(api, args)

    with open(args.file) as stream:
        checkout = json.load(stream)
    if args.command == "verify":
        for quote in checkout["quotes"]:
            _, verified = api.call("GET", "/quotes/" + encode(quote["quoteId"], safe=""))
            if verified != quote:
                raise ValueError("The saved quote no longer matches. Ask the buyer to review new terms.")
        return {"verified": [q["quoteId"] for q in checkout["quotes"]]}
    status, receipt = api.call("POST", "/orders", checkout["body"], checkout["key"])
    return {"httpStatus": status, "receipt": receipt}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2))
    except (KeyError, OSError, ValueError, StorefrontError) as exc:
        sys.exit(str(exc))
