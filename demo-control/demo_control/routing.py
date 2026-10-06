"""ALB release routing for the control panel: a Python port of compose/scripts/alb-routing.sh.

Presets mirror the Makefile targets (`route-baseline`, `canary-110-10`, `incident`, `canary-10/50/100`, `rollback`); the tests parse the
Makefile and run the shell script against a mock `aws` to keep weights and the forward action identical.
Only two AWS calls are made: elbv2 DescribeRules (read-back) and ModifyRule (apply) on one listener rule.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

log = logging.getLogger("demo_control")

RELEASES = ("1.0.0", "1.1.0", "1.2.0")
TG_KEYS = ("100", "110", "120")
# action name (= Makefile target) -> (button label, weights 1.0.0/1.1.0/1.2.0, confirmation text)
# Primary flow: Baseline -> Canary 1.1.0 (10%) -> Check canary fails -> Rollback (back to 100/0/0) -> fix 1.2.0 canary
# against 1.0.0, the release live after the rollback: 90/0/10 -> 50/0/50 -> 0/0/100.
# "Incident" (all to 1.1.0) stays for showing the full impact of the regression.
PRESETS: dict[str, tuple[str, tuple[int, int, int], str]] = {
    "canary-110-10": ("Canary 1.1.0 (10%)", (90, 10, 0), "canary: 10% to new release 1.1.0"),
    "incident": ("Incident (all to 1.1.0)", (0, 100, 0), "regressed 1.1.0 takes everything: the Incident"),
    "canary-10": ("Canary 1.2.0 (10%)", (90, 0, 10), "canary: 10% to fixed 1.2.0, the rest on 1.0.0"),
    "canary-50": ("Canary 1.2.0 (50%)", (50, 0, 50), "canary: 50% to fixed 1.2.0, the rest on 1.0.0"),
    "canary-100": ("Canary 1.2.0 (100%)", (0, 0, 100), "all traffic on 1.2.0"),
    "route-baseline": ("Baseline (all to 1.0.0)", (100, 0, 0), "healthy 1.0.0"),
}
ROLLBACK = "rollback"
ROUTING_ACTIONS = (*PRESETS, ROLLBACK)
ROUTING_KEY = "demo:routing"            # same value format as nginx/apply-routing.sh publishes
STATE_KEY = "demo:routing-state"        # {"current": "90 0 10", "previous": "100 0 0"}; like overlay/.state/routing-<stack>


class RoutingError(RuntimeError):
    pass


def validate_weights(weights) -> tuple[int, int, int]:
    """Same rules and wording as alb-routing.sh validate_weights."""
    weights = list(weights)
    if len(weights) != 3:
        raise RoutingError(f"expected 3 weights <w100> <w110> <w120>, got {len(weights)}")
    for w in weights:
        if not isinstance(w, int) or isinstance(w, bool) or w < 0:
            raise RoutingError(f"weight {w!r} is not a non-negative integer")
        if w > 100:
            raise RoutingError(f"weight {w} is above 100")
    if sum(weights) != 100:
        raise RoutingError(f"weights {weights[0]}/{weights[1]}/{weights[2]} sum to {sum(weights)}, must be 100")
    return weights[0], weights[1], weights[2]


def fmt_weights(weights) -> str:
    return " ".join(str(w) for w in weights)


def forward_actions(target_groups: dict[str, str], weights) -> list[dict]:
    """The exact `--actions` JSON alb-routing.sh writes (all three target groups, zero weights included, Order 1)."""
    return [{"Type": "forward", "ForwardConfig": {"TargetGroups": [
        {"TargetGroupArn": target_groups[k], "Weight": w} for k, w in zip(TG_KEYS, weights)]}, "Order": 1}]


def parse_live(rules_response: dict, target_groups: dict[str, str]) -> tuple[int, int, int]:
    """Port of the script's --sync parser: exactly the three inventory target groups, integer weights summing to 100."""
    rules = rules_response.get("Rules") or []
    if not rules:
        raise RoutingError("AWS returned no rule for the inventory rule ARN")
    forwards = [a for a in rules[0].get("Actions", []) if a.get("Type") == "forward"]
    groups = forwards[0].get("ForwardConfig", {}).get("TargetGroups") if forwards else None
    expected = [target_groups[k] for k in TG_KEYS]
    if not isinstance(groups, list) or len(groups) != len(expected):
        raise RoutingError("weighted forward action must contain exactly the three inventory target groups")
    weights: dict[str, int] = {}
    for g in groups:
        if not isinstance(g, dict) or not {"TargetGroupArn", "Weight"} <= set(g):
            raise RoutingError("AWS returned an invalid weighted target group")
        arn, weight = g["TargetGroupArn"], g["Weight"]
        if arn not in expected or arn in weights or not isinstance(weight, int) or isinstance(weight, bool):
            raise RoutingError("AWS returned unexpected target group weights")
        weights[arn] = weight
    if set(weights) != set(expected):
        raise RoutingError("AWS did not return every inventory target group")
    return validate_weights([weights[a] for a in expected])


def region_from_arn(arn: str) -> str:
    parts = arn.split(":")
    if len(parts) < 6 or parts[0] != "arn" or parts[2] != "elasticloadbalancing" or not parts[3]:
        raise RoutingError(f"could not derive AWS region from {arn!r} (expected an elasticloadbalancing listener-rule ARN)")
    return parts[3]


class AlbRouting:
    """Apply / read back the weighted forward action of this stack's inventory listener rule."""

    def __init__(self, elbv2, rule_arn: str, target_groups: dict[str, str], redis_client):
        self._elbv2, self.rule_arn, self.target_groups, self._redis = elbv2, rule_arn, dict(target_groups), redis_client

    # --- reading ---------------------------------------------------------------------------------------------------
    def live(self) -> tuple[int, int, int]:
        try:
            resp = self._elbv2.describe_rules(RuleArns=[self.rule_arn])
        except Exception as exc:  # botocore ClientError / BotoCoreError (credentials, network)
            raise RoutingError(f"could not read the weighted forward action for {self.rule_arn}: {exc}") from exc
        return parse_live(resp, self.target_groups)

    def state(self) -> dict:
        try:
            raw = self._redis.get(STATE_KEY)
        except Exception as exc:  # noqa: BLE001
            raise RoutingError(f"could not read {STATE_KEY} from Redis: {exc}") from exc
        if raw is None:
            return {"current": None, "previous": None}
        try:
            value = json.loads(raw)
            return {"current": value.get("current") or None, "previous": value.get("previous") or None}
        except (ValueError, AttributeError) as exc:
            raise RoutingError(f"{STATE_KEY} in Redis is not valid JSON: {raw!r}") from exc

    def view(self) -> dict:
        weights = self.live()
        state = self.state()
        return {"weights": dict(zip(RELEASES, weights)), "current": fmt_weights(weights),
                "previous": state["previous"], "rule_arn": self.rule_arn}

    # --- writing ---------------------------------------------------------------------------------------------------
    def run(self, name: str, progress: Callable[[str], None]) -> dict:
        if name == ROLLBACK:
            previous = self.state()["previous"]
            if not previous:
                raise RoutingError("no previous routing recorded; make a routing change first")
            weights = validate_weights([int(x) for x in previous.split()])
        elif name in PRESETS:
            weights = PRESETS[name][1]
        else:
            raise RoutingError(f"unknown routing action {name!r}")
        return self.apply(weights, progress)

    def apply(self, weights, progress: Callable[[str], None]) -> dict:
        weights = validate_weights(weights)
        progress("Reading live ALB weights")
        before = self.live()
        progress(f"Applying weights 1.0.0/1.1.0/1.2.0 = {fmt_weights(weights)}%")
        try:
            self._elbv2.modify_rule(RuleArn=self.rule_arn, Actions=forward_actions(self.target_groups, weights))
        except Exception as exc:  # botocore ClientError / BotoCoreError
            raise RoutingError(f"AWS rejected the weighted forward action for {self.rule_arn}: {exc}") from exc
        progress("Reading the ALB rule back")
        after = self.live()
        if after != weights:
            raise RoutingError(f"ALB rule reports {fmt_weights(after)} after requesting {fmt_weights(weights)}")
        new = fmt_weights(weights)
        # Like the script: remember the previous routing only when the routing actually changed.
        # The panel's "current" is the live ALB value, so make targets used in between cannot desynchronise it.
        if after != before:
            self._write_state({"current": new, "previous": fmt_weights(before)})
        elif self.state()["current"] != new:
            self._write_state({"current": new, "previous": self.state()["previous"]})
        progress("Publishing demo:routing to Redis")
        value = " ".join(f"{r}={w}" for r, w in zip(RELEASES, weights))
        try:
            self._redis.set(ROUTING_KEY, value)
        except Exception as exc:  # noqa: BLE001
            raise RoutingError(f"ALB weights {new} applied and verified, but Redis {ROUTING_KEY} was not updated: {exc}") from exc
        log.info("alb routing applied", extra={"fields": {"event": "demo_routing", "weights": new,
                                                          "before": fmt_weights(before), "rule_arn": self.rule_arn}})
        progress(f"ALB weights 1.0.0/1.1.0/1.2.0 = {new}% (verified)")
        state = self.state()
        return {"weights": dict(zip(RELEASES, weights)), "current": new, "previous": state["previous"]}

    def _write_state(self, value: dict) -> None:
        try:
            self._redis.set(STATE_KEY, json.dumps(value))
        except Exception as exc:  # noqa: BLE001
            raise RoutingError(f"ALB weights {value['current']} applied and verified, but {STATE_KEY} "
                               f"(rollback memory) was not saved: {exc}") from exc
