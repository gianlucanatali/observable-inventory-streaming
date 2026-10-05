# Account-wide cost and CCM resources

This Terraform root owns the Confluent cost-meter identity, Datadog integrations, and AWS CUR/CCM resources shared by all stacks. It is deliberately separate from stack lifecycle state.

## New account

All four adoption variables default to an empty string. With those defaults Terraform creates this account's own resources and never references another organisation's IDs.

## Existing private account adoption

The existing account keeps the existing Terraform addresses:

- `confluent_service_account.cost_meter`
- `confluent_role_binding.cost_meter_billing_admin`
- `confluent_role_binding.cost_meter_metrics_viewer`
- `datadog_integration_confluent_account.cost_meter`

For a first adoption, supply only the applicable opt-in IDs as Terraform variables:

- `cost_meter_service_account_import_id`
- `cost_meter_billing_admin_import_id`
- `cost_meter_metrics_viewer_import_id`
- `cost_meter_datadog_integration_import_id`

The cost-meter service-account display name is `dd-demo-sa-cost-meter`; its Terraform address is unchanged, so an already-managed private resource is renamed in place. No `moved` block, state move, or destructive replacement is required.

## Account teardown

Normal lifecycle protection remains enabled. The only supported destructive path is:

    make MODE=cloud STACK=account account-down CONFIRM=yes ACCOUNT_DOWN_DESTROY=yes

`CONFIRM=yes` is the normal lifecycle confirmation and `ACCOUNT_DOWN_DESTROY=yes` is a separate deliberate destruction acknowledgement. The command still creates a saved destroy plan, prints its summary, and uses the ordinary review/confirmation path; neither flag authorizes an apply in another session.

For this one invocation, `stack.sh` writes an ignored mode-600 temporary override that disables `prevent_destroy` only on the cost-meter Confluent service account and its two role bindings, and enables `force_destroy` only on the CUR bucket. It removes the override on successful completion and on failure or interruption. Account-down permanently removes account-wide cost history and integration resources; never use it as routine stack cleanup.
