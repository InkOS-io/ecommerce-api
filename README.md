# InkOS E-commerce API v1

A storefront can show an InkOS-calculated price and submit the terms the customer accepted at checkout. One checkout can contain several print jobs. InkOS commits every line together and returns the order and work-order references for each one. [OpenAPI 3.1 contract](openapi.yaml) · [Two-line example exchange](examples/exchange.json) · [WooCommerce plugin](https://github.com/InkOS-io/woocommerce-plugin)

The base path is `/api/ecommerce/v1` on the shop's InkOS installation. The shop operator supplies the origin and provisions a connection. Production connections use HTTPS. All examples below use synthetic records.

## Which InkOS interface to use

| Developer's task | Interface |
| --- | --- |
| Publish shop-approved variants, price a cart and submit checkout | E-commerce API v1. Each credential belongs to one storefront and one shop. |
| Sync a CRM or dashboard with quotes, orders and work orders | The customer API is a proposed read contract with its own permissions and sync cursor. Its public repository describes deployment status. [Customer API](https://github.com/InkOS-io/customer-api) |
| Sell with WooCommerce | Install the plugin, connect it to this API and map WooCommerce product or variation IDs to the shop's published configurations. [WooCommerce plugin](https://github.com/InkOS-io/woocommerce-plugin) |
| Run an InkOS-hosted shop site | The optional InkOS storefront is a separate application. A shop using its own website connects directly to this contract. |
| Source work from a wholesale production partner | Use the supplier discovery, offer and dispatch contract. [Trade API](https://github.com/InkOS-io/trade-api) |

## Connect a shop

Enable the production module in the shop’s organization settings. A signed-in InkOS shop owner creates the connection and publishes its configurations. The administration routes use the shop session. For browser mutations, use the trusted InkOS origin and a JSON content type for requests with bodies. The storefront server receives only the opaque bearer credential. Keep the credential server-side and store it separately for each installation. A customer's browser calls your storefront.

| Shop owner action | Method and relative path | Body or result |
| --- | --- | --- |
| Connect a descriptive store name | `POST /admin/storefronts` | Send `{"name":"Example retail store"}`. Save the returned `storefrontId` and one-time `token`. |
| Publish or update a sellable variant | `PUT /admin/storefronts/{storefrontId}/configurations/{configurationId}` | Send a name, fixed option map, the ID of a saved, priced shop quote as `quoteTemplateId`, and `enabled`. |
| Inspect a connection’s publications | `GET /admin/storefronts/{storefrontId}/configurations` | Lists enabled and withdrawn variants with their template IDs. |
| Withdraw a variant | Same `PUT` route | Supply the configuration and set `enabled` to `false`. |
| List connections | `GET /admin/storefronts` | See names, IDs and revocation status. |
| Revoke a credential | `DELETE /admin/storefronts/{storefrontId}` | Subsequent bearer requests receive HTTP 401. |

For example, save a priced business-card template in the shop, choose `cards-matte` as its public configuration ID and publish the following body. The quote template supplies the shop's pricing recipe and production specifications. Ensure the option labels describe that recipe accurately. A distinct selection such as gloss requires its own configured recipe and identifier.

```json
{
  "name": "Matt business cards",
  "options": {"Stock": "16 pt", "Finish": "Matt"},
  "quoteTemplateId": "replace-with-your-shop-quote-id",
  "enabled": true
}
```

The storefront sends `Authorization: Bearer <token>` on every API call. `GET /connection` tests the credential. `GET /configurations` returns the enabled IDs, names and fixed option values that this connection can sell. Map a complete shopper selection to one of those IDs. The catalog and pricing responses contain only storefront-facing fields.

## From a selection to a work ticket

1. Send `POST /quotes` for each cart line using its `configurationId` and integer `quantity`. Keep the returned `quoteId`, quantity, name, options, currency, `totalMinor` and `expiresAt`. The price covers the **entire line**. For example, 250 cards at `4599` CAD minor units cost CAD 45.99 for the run. Deriving a rounded per-card price and multiplying back changes that total.
2. Display the extended line totals and their sum. Changing quantity or configuration requires a new quote for that line. One quote represents one line, even when two lines use identical selections. The quote lasts 30 minutes. Update the displayed terms and obtain the buyer's agreement when a replacement is needed.
3. Recheck stored terms server-side with `GET /quotes/{quoteId}` before accepting checkout. Keep the accepted terms, stable store order ID, idempotency key and complete transmission body in durable storage with the store order.
4. Submit `POST /orders` using the saved `Idempotency-Key` header and body. Supply the ordered list of distinct quote IDs, customer contact and external order reference. InkOS validates all lines, calculates the total from their saved terms and commits the cart atomically. A validation error on any line leaves all unconsumed lines available. The response pairs each submitted quote with an InkOS order and its linked work order.
5. Save the receipt. Staff can use the order numbers in the InkOS register and work-order numbers on the production board. Intake leaves each job subject to the shop's production release, proof and commercial requirements. An optional `paymentReference` is a reconciliation reference. A payment provider or authorized staff workflow handles funds and ledger entries separately.

Quote terms freeze the quoted price, options and quantity. Publishing a revised enabled configuration affects subsequent quotes. Expiry, withdrawal of a configuration, an unavailable shop currency or previous consumption of a quote can prevent a new acceptance. The server checks current availability again when it commits the cart.

The v1 cart represents configured print merchandise, in one shop currency. Shipping, tax, coupons, unconfigured goods and separate fee lines have no fields in the order contract. Account for any such amounts explicitly in the storefront and show the buyer which amount InkOS covers. A checkout that promises an InkOS total must reconcile against the integer sum of accepted InkOS lines.

## Retry and reconciliation

| Outcome | Storefront action |
| --- | --- |
| 201 with receipt | Save all InkOS identifiers and mark synchronized. |
| 200 with receipt | A previous transmission already committed. Save the same receipt and mark synchronized. |
| Timeout, disconnect, or 5xx | The client might have lost the response after commit. Schedule a durable retry with the identical key, external order ID and body. Back off with jitter. For a gateway 429 or 503, respect a supplied `Retry-After` value. |
| 409 `quote_expired` or `quote_unavailable` | Stop the worker. For an open cart, obtain fresh terms and show them to the buyer. For an already-placed order, surface a reconciliation case for staff. |
| 409 `idempotency_conflict` | The key or external order ID conflicts with an earlier acceptance. Reconcile against the original saved payload and receipt. Generating a fresh identifier can obscure the original order. |
| 401 `unauthorized`, 403 `forbidden` | Check the connection, revocation state or shop-owner authority appropriate to the route. |
| 404 `not_found` | Check the mapped configuration or quote under the issuing credential. Out-of-scope and unknown IDs have the same response. |
| 400, 405, 413, 415 | Correct input, method, size or content type before retrying. |

A successful cart can be replayed with the same key and body after its original quote expires. A matching external order ID with a different key conflicts. Preserve the line order, customer contact and payment reference across retries. A subsequent external payment event must not generate a second intake. Error bodies have a stable `code` for branching and an `error` message for staff.

## Run the synthetic checkout

Use a disposable InkOS installation with a connected test storefront and at least one published configuration. Supply `INKOS_TOKEN` from your local secret manager or a hidden prompt. The Python example has no third-party dependencies. `INKOS_BASE` includes the versioned prefix.

```sh
export INKOS_BASE='http://127.0.0.1:8080/api/ecommerce/v1'
python3 examples/checkout.py catalog
python3 examples/checkout.py prepare checkout.json --line cards-matte=250 --line posters-satin=2
python3 examples/checkout.py verify checkout.json
```

Use IDs printed by `catalog`. You may use the same configuration with two quantities. Review the displayed line prices and currency. The following commands create real test jobs in that sandbox and replay the accepted cart. The first response should be 201 and the second 200, with the same identifiers.

```sh
python3 examples/checkout.py submit checkout.json
python3 examples/checkout.py submit checkout.json
```

`checkout.json` retains the request and key for recovery. `prepare` requires a new filename if the previous file exists. A store implementation should persist the equivalent data in its order database.

## Run the contract tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The default run validates the OpenAPI document and checks the sample requests and responses against its schemas. To run the same suite against HTTP handlers, provision two independent synthetic shops on a loopback InkOS instance. Publish a configuration exclusive to the first shop and leave it enabled. Export the two server credentials as `INKOS_TOKEN` and `INKOS_OTHER_TOKEN`. Set `INKOS_BASE` as above, then run the suite.

```sh
INKOS_LIVE_CONTRACT=1 INKOS_CONFIGURATION=cards-matte \
  .venv/bin/python -m unittest discover -s tests -v
```

Optionally export the first sandbox shop’s owner session cookie value as `INKOS_TEST_OWNER_SESSION` to exercise publication, immutable saved terms, withdrawal, same-shop storefront isolation and revocation. The test creates and revokes a separate storefront in that shop. If the configured InkOS dashboard origin differs from the API origin, set `INKOS_TEST_OWNER_ORIGIN` to the dashboard origin.

The live suite creates synthetic orders and checks response schemas, tenant isolation, quote verification, invalid quantities, multi-line totals and atomicity, concurrent delivery, replay and conflicting reuse of order identifiers. Point it at a running InkOS sandbox.
