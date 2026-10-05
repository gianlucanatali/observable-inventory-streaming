# offer-worker

Consumes `carts.at-risk` (Flink, [`flink/cart_at_risk.sql`](../flink/cart_at_risk.sql)) and publishes compacted `offers`. The contract is in [`contracts/README.md`](../contracts/README.md), sections 2, 7 and 9. The worker keeps business-policy decisions deterministic while optional AI services choose among bounded candidates or generate descriptive text.

Per record: skip tombstones (a null value means the risk ended), drop risks older than `MAX_RISK_AGE_S`, dedup on `risk_id` (bounded in-memory LRU, `offer_id = "offer|" + risk_id`), then

1. **Candidates (policy)**: up to 2 in-stock same-category alternatives (closest price first, stock read from Redis `sellable:{product_id}`, catalogue from `products.json`) at `OFFER_DISCOUNT_PCT` (hard cap 15), plus notify-me. `SAME_PRODUCT_OTHER_STORE` is never produced (online sellable is 0 everywhere).
2. **Decision lane**: Jev picks one candidate id. Accepted only if the id is in the candidate set and `confidence >= JEV_MIN_CONFIDENCE`; everything else is `RULE_DEFAULT` (first candidate: closest in-stock alternative, else notify-me). Only product facts are sent to Jev, never cart or shopper ids.
3. **Revalidate**: re-read `sellable:{alt}`; not `> 0` (or unreadable) becomes notify-me, route `RULE_DEFAULT`, reason `invalid_choice`.
4. **Text lane**: Bedrock (if enabled) writes one descriptive body line; rejected if it has more than one line, over 200 chars, any currency/percent/discount wording, or any number not found in the product names/sizes. Rejected or failed text uses the deterministic template. Headline is always a template. Terms (type, product, `discount_pct`) come from policy only.
5. Publish, wait for delivery, commit the offset (at least once).

## Environment

Required: `KAFKA_BOOTSTRAP`, `SR_URL`, `REDIS_URL`, `DD_AGENT_HOST`, `DD_ENV`, `DD_SERVICE`, `DD_VERSION`; with `KAFKA_SECURITY_PROTOCOL=SASL_SSL` (default) also `KAFKA_API_KEY`, `KAFKA_API_SECRET`, `SR_API_KEY`, `SR_API_SECRET` (`PLAINTEXT` needs none). With `BEDROCK_ENABLED=true`: `AWS_REGION`, `BEDROCK_MODEL_ID` (instance role credentials). Missing or invalid values exit at start naming the variable.

Optional: `JEV_API_KEY` (unset = no Jev, reason `disabled`), `JEV_URL` (default `https://api.typesafe.ai/v1/systemone`), `JEV_MODEL` (`jev-latest`), `JEV_TIMEOUT_MS` (800), `JEV_MIN_CONFIDENCE` (0.8), `OFFERS_KILL_SWITCH` (env, default false), `OFFERS_KILL_SWITCH_KEY` (Redis key, default `offers:kill_switch`), `BEDROCK_ENABLED` (false), `BEDROCK_TIMEOUT_MS` (3000), `OFFER_DISCOUNT_PCT` (10), `MAX_RISK_AGE_S` (300), `DEDUP_SIZE` (10000), `KAFKA_GROUP_ID`, `RISK_TOPIC`, `OFFERS_TOPIC`, `SCHEMA_DIR`, `CATALOGUE_FILE`. `STORE_HOSTS` is not used.

**Kill switch, live**: set the env var (restart) or, without a restart, `redis-cli SET offers:kill_switch 1` (back: `SET offers:kill_switch 0`). It is read per record; an unreadable key counts as engaged. It stops Jev calls only (decision lane); the text lane has `BEDROCK_ENABLED`.

## Telemetry

DogStatsD (contract section 7): `offer.decision{route,reason}`, `offer.text{route,reason}`, `offer.completed{offer_type}`. Reasons: decision `accepted, low_confidence, invalid_choice, timeout, error, rate_limited, kill_switch` plus two additions not in `offer.avsc`'s doc string: `disabled` (no `JEV_API_KEY`) and `no_choice` (only notify-me available); text `accepted, invalid_text, timeout, error, disabled`. JSON logs carry `risk_id`, `cart_id`, `scenario_id`, `product_id`, `offer_id` and Datadog trace ids. APM: `offer.process` span per record, child spans `offer.jev.call` (service `jev`) and `offer.bedrock.call` (service `bedrock`), plus ddtrace's automatic Kafka/Redis spans and Data Streams Monitoring.

Routes: none (no HTTP server, no health endpoint; compose should use restart policy, a crash is the failure signal).

## Tests and build

`uv run --python 3.12 pytest -q` (Jev, Bedrock and Kafka are mocked; Redis is fakeredis). Image: `docker build -f offer-worker/Dockerfile .` from the repository root.

## Jev documentation status

- **Jev request shape verified.** The official TypeSafe API and Choice docs confirm the endpoint, Bearer authentication, top-level `state`/`model`/`questions`, and Choice `criteria` as an option-id map whose values may be strings, objects, arrays, or null. The worker emits the valid string-description subset. A mismatch still shows as `reason=error`, never as a wrong offer.
- **Still unverified:** the account's access to `jev-latest`, live response behavior, and latency. The worker's response parsing and timeout behavior are not changed by the request-shape check.
- Jev latency from eu-west-1 (the service is described as West Coast hosted): measure before fixing `JEV_TIMEOUT_MS`. The timeout is httpx's per-phase timeout, not one overall deadline.
- Bedrock: model access granted in the account, `BEDROCK_MODEL_ID` offered in eu-west-1 (or an inference profile id), latency, and that the model accepts the Converse `system` and `inferenceConfig` fields. The Converse call shape is from boto3's `bedrock-runtime` API.
- `boto3==1.43.108` is pinned in the dependency file; refresh it when the dependency policy changes.
- The Flink statement was never run; verify its temporal filter, upsert table options and ABANDON handling in the first cloud run.
