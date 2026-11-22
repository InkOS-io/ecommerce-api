"""Validate the published contract and exercise an explicitly configured local sandbox."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import quote, urlsplit
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from storefront import Storefront, StorefrontError

SPEC = yaml.safe_load((ROOT / "openapi.yaml").read_text())


def matches(instance, schema):
    # A subschema can reference the components of the published document.
    Draft202012Validator({"components": SPEC["components"], **schema},
                         format_checker=FormatChecker()).validate(instance)


def payload(method, path, status):
    operation = SPEC["paths"][path][method.lower()]
    responses = operation["responses"]
    response = responses.get(str(status), responses["default"])
    if "$ref" in response:
        response = SPEC["components"]["responses"][response["$ref"].split("/")[-1]]
    return response["content"]["application/json"]["schema"]


class OwnerSandbox(Storefront):
    def auth_headers(self):
        location = urlsplit(self.base)
        origin = os.environ.get("INKOS_TEST_OWNER_ORIGIN", location.scheme + "://" + location.netloc)
        return {"Cookie": "inkos_session=" + self.token, "Origin": origin}


class Specification(unittest.TestCase):
    def test_document_is_valid_openapi(self):
        validate(SPEC)

    def test_published_examples_match_request_and_receipt_schemas(self):
        records = json.loads((ROOT / "examples" / "exchange.json").read_text())
        for record in records:
            with self.subTest(record=record["path"]):
                method, path = record["method"], record["path"]
                operation = SPEC["paths"][path][method.lower()]
                if "body" in record:
                    matches(record["body"], operation["requestBody"]["content"]["application/json"]["schema"])
                matches(record["response"], payload(method, path, record["status"]))


@unittest.skipUnless(os.getenv("INKOS_LIVE_CONTRACT") == "1",
                     "Set INKOS_LIVE_CONTRACT=1 for a disposable local shop.")
class LiveCheckout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = os.environ["INKOS_BASE"]
        if urlsplit(base).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Live tests create synthetic orders. Use a loopback sandbox.")
        cls.api = Storefront.from_env()
        cls.other = Storefront(base, os.environ["INKOS_OTHER_TOKEN"])
        cls.config_id = os.environ["INKOS_CONFIGURATION"]

    def success(self, method, path, status, *, body=None, route=None, key=None):
        actual, response = self.api.call(method, path, body, key)
        self.assertEqual(actual, status)
        matches(response, payload(method, route or path, actual))
        return response

    def problem(self, api, method, path, status, code, *, body=None, route=None, key=None):
        with self.assertRaises(StorefrontError) as raised:
            api.call(method, path, body, key)
        self.assertEqual(raised.exception.status, status, raised.exception.body)
        self.assertEqual(raised.exception.body["code"], code)
        matches(raised.exception.body, payload(method, route or path, status))

    def price(self, quantity):
        return self.success("POST", "/quotes", 201, body={
            "configurationId": self.config_id, "quantity": quantity,
        })

    def cart(self, *quotes):
        return {
            "externalOrderId": "contract-" + uuid4().hex,
            "customer": {"name": "Contract Buyer", "email": "contract@example.invalid"},
            "items": [{"quoteId": q["quoteId"]} for q in quotes],
            "paymentReference": "synthetic-reconciliation-" + uuid4().hex,
        }

    def test_connection_catalog_and_scoping(self):
        first = self.success("GET", "/connection", 200)
        other = self.other.call("GET", "/connection")[1]
        self.assertNotEqual(first["storefrontId"], other["storefrontId"])
        catalog = self.success("GET", "/configurations", 200)
        self.assertIn(self.config_id, [c["configurationId"] for c in catalog["configurations"]])
        # Choose a configuration ID published exclusively to the first shop's storefront.
        self.assertNotIn(self.config_id, [c["configurationId"] for c in
                                          self.other.call("GET", "/configurations")[1]["configurations"]])
        body = {"configurationId": self.config_id, "quantity": 1}
        self.problem(self.other, "POST", "/quotes", 404, "not_found", body=body)
        saved = self.price(2)
        path = "/quotes/" + quote(saved["quoteId"], safe="")
        self.assertEqual(self.success("GET", path, 200, route="/quotes/{quoteId}"), saved)
        self.problem(self.other, "GET", path, 404, "not_found", route="/quotes/{quoteId}")
        cart = self.cart(saved)
        self.problem(self.other, "POST", "/orders", 404, "not_found",
                     body=cart, key=cart["externalOrderId"])

    def test_concurrent_cart_replays_and_identity_conflicts(self):
        quotes = [self.price(2), self.price(3)]
        self.assertEqual([q["quantity"] for q in quotes], [2, 3])
        self.assertNotEqual(quotes[0]["quoteId"], quotes[1]["quoteId"])
        cart = self.cart(*quotes)
        key = cart["externalOrderId"]
        # Two independent workers transmit the stored payload simultaneously.
        def send(_):
            return self.api.call("POST", "/orders", cart, key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        self.assertEqual(sorted(s for s, _ in results), [200, 201])
        receipt = results[0][1]
        for status, response in results:
            matches(response, payload("POST", "/orders", status))
            self.assertEqual(response, receipt)
        self.assertEqual(receipt["externalOrderId"], key)
        self.assertEqual(receipt["currency"], quotes[0]["currency"])
        self.assertEqual(receipt["totalMinor"], sum(q["totalMinor"] for q in quotes))
        self.assertEqual([line["quoteId"] for line in receipt["items"]],
                         [q["quoteId"] for q in quotes])
        for field in ("orderId", "orderNumber", "workOrderId", "workOrderNumber"):
            self.assertEqual(len({line[field] for line in receipt["items"]}), 2)
        self.assertEqual(self.success("POST", "/orders", 200, body=cart, key=key), receipt)
        self.problem(self.api, "POST", "/orders", 409, "idempotency_conflict",
                     body={**cart, "paymentReference": "changed"}, key=key)
        self.problem(self.api, "POST", "/orders", 409, "idempotency_conflict",
                     body=cart, key="another-" + key)
        self.problem(self.api, "POST", "/orders", 409, "idempotency_conflict",
                     body={**cart, "externalOrderId": "different-" + key}, key=key)

    def test_failed_batch_can_be_submitted_without_the_bad_line(self):
        valid = self.price(1)
        invalid = {"quoteId": "unissued-" + uuid4().hex}
        failed = self.cart(valid, invalid)
        self.problem(self.api, "POST", "/orders", 404, "not_found",
                     body=failed, key=failed["externalOrderId"])
        retry = self.cart(valid)
        receipt = self.success("POST", "/orders", 201, body=retry, key=retry["externalOrderId"])
        self.assertEqual(receipt["totalMinor"], valid["totalMinor"])

    @unittest.skipUnless(os.getenv("INKOS_TEST_OWNER_SESSION"),
                         "Set INKOS_TEST_OWNER_SESSION to exercise owner setup and revocation.")
    def test_owner_publication_edits_and_revocation(self):
        admin = OwnerSandbox(self.api.base, os.environ["INKOS_TEST_OWNER_SESSION"])
        # Reuse the fixture shop's own synthetic priced template to publish to a sibling store.
        existing = self.api.call("GET", "/connection")[1]["storefrontId"]
        settings_route = "/admin/storefronts/{storefrontId}/configurations"
        existing_path = settings_route.replace("{storefrontId}", existing)
        status, settings = admin.call("GET", existing_path)
        matches(settings, payload("GET", settings_route, status))
        template = next(c["quoteTemplateId"] for c in settings["configurations"]
                        if c["configurationId"] == self.config_id)
        self.problem(self.api, "GET", "/admin/storefronts", 401, "unauthorized")
        status, identity = admin.call("POST", "/admin/storefronts", {"name": "Contract sibling"})
        self.assertEqual(status, 201)
        matches(identity, payload("POST", "/admin/storefronts", status))
        sibling = Storefront(self.api.base, identity["token"])
        store_path = "/admin/storefronts/" + identity["storefrontId"]
        try:
            variant = {"name": "Test matte cards", "options": {"Finish": "Matte"},
                       "quoteTemplateId": template, "enabled": True}
            path = store_path + "/configurations/contract-matte"
            route = settings_route + "/{configurationId}"
            status, published = admin.call("PUT", path, variant)
            matches(published, payload("PUT", route, status))
            _, saved = sibling.call("POST", "/quotes", {
                "configurationId": "contract-matte", "quantity": 25})
            lookup = "/quotes/" + quote(saved["quoteId"], safe="")
            self.problem(self.api, "GET", lookup, 404, "not_found", route="/quotes/{quoteId}")
            edited = {**variant, "options": {"Finish": "Gloss"}}
            admin.call("PUT", path, edited)
            self.assertEqual(sibling.call("GET", lookup)[1], saved)
            admin.call("PUT", path, {**edited, "enabled": False})
            self.assertEqual(sibling.call("GET", "/configurations")[1], {"configurations": []})
            self.problem(sibling, "GET", lookup, 409, "quote_unavailable", route="/quotes/{quoteId}")
            cart = self.cart(saved)
            self.problem(sibling, "POST", "/orders", 409, "quote_unavailable",
                         body=cart, key=cart["externalOrderId"])
        finally:
            status, revoked = admin.call("DELETE", store_path)
            matches(revoked, payload("DELETE", "/admin/storefronts/{storefrontId}", status))
        self.problem(sibling, "GET", "/connection", 401, "unauthorized")

    def test_quantity_and_credentials(self):
        self.problem(self.api, "POST", "/quotes", 400, "invalid_request",
                     body={"configurationId": self.config_id, "quantity": 0})
        self.problem(Storefront(self.api.base, "invalid-synthetic-token"), "GET", "/connection",
                     401, "unauthorized")


if __name__ == "__main__":
    unittest.main()
