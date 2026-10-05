# Size-keyed calibration (Task 12.4)

Cloud calibration is selected independently of `STACK`. The standard command remains
`make MODE=cloud STACK=<name> stack-up CONFIRM=yes` from `overlay/`; a new stack name
requires no calibration file. Resource creation still requires the cost note and
explicit approval described in the root guide.

Selectors (Make arguments or exported environment variables):

| Selector | Default | Tracked profile |
|---|---|---|
| `TF_VAR_instance_type` | `t4g.xlarge` | `calibration.t4g.xlarge.env` |
| `FARGATE_SIZE` | `2048-4096` | `calibration.fargate-2048-4096.env` |

The Fargate selector is the regression service's CPU units and MiB, not a stack
name. Its profile contains the complete eight-service `TF_VAR_service_sizing`
JSON map consumed by `terraform/aws/variables.tf` and `ecs.tf`. Other services keep
their existing smaller task sizes. The VM selector is exported as the actual
Terraform VM `instance_type`; both selectors and the sizing map survive recursive
Make calls from the lifecycle script. Do not separately override `service_sizing`
with Terraform flags/tfvars: add a reviewed size profile instead.

Make validates both cloud profiles before including either or invoking the
lifecycle script. Missing, untracked, symlinked or incorrectly named Fargate-size
profiles fail loudly, including on `stack-preflight` and `stack-up`. The Fargate
profile is layered after the VM profile for shared load/catalogue settings. Dev
mode keeps its original defaults and does not require cloud calibration.

The migrated values are 3000 catalogue products, 5 rps for 120 seconds. The VM
profile preserves rehearsal's 2 CPU / 768m / four workers; the Fargate profile
preserves hybrid's existing sizing, with the regression task at 2048 CPU units /
4096 MiB. These are migrated settings, not new performance measurements: rehearsal
has Task 1 evidence in the autonomous brief, while hybrid still needs its live
latency acceptance. Catalogue size is a Docker build argument, so changing it
requires a rebuild, not merely restarting a task.

## Jev confidence proposal (Q6)

Keep `JEV_MIN_CONFIDENCE=0.8` until the presenter decides otherwise. Three live
sell-out-to-render runs produced confidence `0.73`, `0.75`, and `0.72`; the
post-expiry proof produced `0.79`, with `jev_choice=notify_me` while the safe
rule default selected the eligible P0160 alternative. These four observations
are useful diagnostics but are not enough to recalibrate a decision boundary.

Proposal: retain `0.8` for the presentation so low-confidence choices continue
to demonstrate the deterministic rule-default path. Before lowering it, collect
at least 30 labelled decisions spanning notify-me and eligible alternatives,
then compare choice validity and agreement with policy by confidence band. Use
the live `jev_min_confidence` control only for an explicitly announced demo of
both routes; do not silently change the deployed default.

Offline checks (no credentials read, no cloud calls):

    make -C overlay MODE=cloud STACK=any-name calibration-check
    python3 -m unittest discover -s overlay/compose/scripts/tests -p test_calibration.py

For a new size, add the appropriate non-secret profile to Git before invoking
Make, keep Terraform sizing and the filename consistent, and record measured
latency/error/throughput gates before calling it performance-validated. No fallback
to a differently sized profile is allowed. The lifecycle topology selection is a
separate Task 12 integration concern; calibration never chooses topology by name.
