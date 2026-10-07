# AWS-native online side

This directory owns the AWS cloud half of one demo stack: a single-node `cache.t4g.small` ElastiCache Redis instance, Fargate ARM64 application services with Datadog Agent sidecars, and the public ALB. It uses the default VPC's subnets and has no NAT gateway. Task images come from the per-stack ECR repositories of `terraform/images`, tagged by content (`image_tags`).

## State isolation

Run from a workspace named exactly like `stack`; `terraform_data.workspace_guard` refuses an accidental cross-stack apply. The parent stack orchestration supplies `on_prem_security_group_id`, `presenter_cidr`, and the stack-scoped SSM parameter names.

## Secrets

`secret_parameter_names` contains parameter *names*, never values. `stack.sh` (W4) creates/updates those SecureString values outside Terraform; task definitions contain only parameter ARNs and execution-role access is limited to those ARNs. Do not add `aws_ssm_parameter` or `data.aws_ssm_parameter` here: both risk exposing secret values in Terraform state.

## Size calibration

The standard Make lifecycle exports `TF_VAR_service_sizing` from the tracked
`compose/calibration.fargate-<CPU>-<MiB>.env` selected by `FARGATE_SIZE` (default
`512-1024`, the size of the 1.1.0 release task, equal to 1.0.0 and 1.2.0). This map is the existing `service_sizing` input used by every ECS task;
there is no per-stack-name configuration. See [calibration selection and migration](../../compose/CALIBRATION.md).
Direct Terraform validation retains the original defaults, but deployment goes
through Make's fail-closed tracked-profile check.

## Fargate application logs

Every ECS application container sends stdout through FireLens to Datadog; the
custom Fluent Bit output in `../../log-router/extra.conf` independently writes
those same records to its existing CloudWatch log group. The router image is
built and pushed with the ARM64 task images by `compose/scripts/stack.sh`; it is
not an application image and is never started by Docker Compose.

The Datadog output takes `DD_API_KEY` only from the stack-local SSM SecureString
through ECS `secretOptions`; no key is tracked, placed in task environment, or
exposed as a Terraform output. `DD_LOGS_INJECTION=true` is present in every
application environment, so Python trace/log correlation fields `dd.trace_id`
and `dd.span_id` remain usable; the FireLens output provides `env`, `service`,
`version`, `source:python`, `project`, and `stack` tags.

Primary source: Datadog, [Amazon ECS on AWS Fargate — Log collection](https://docs.datadoghq.com/integrations/ecs_fargate/?tab=webui#log-collection)
(accessed 2026-10-05). It documents FireLens with Datadog's Fluent Bit output,
the EU intake hostname, `dd_service`/`dd_source`/`dd_tags`, and `secretOptions`
for `apikey`. The paired CloudWatch output follows AWS's documented [FireLens
multi-destination Fluent Bit pattern](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/firelens-docker-buffer-limit.html).

## Demo-control routing and store feed

demo-control runs with its own task role, `<name>-demo-control-task`, instead of the shared task role:

- `elasticloadbalancing:ModifyRule` on `aws_lb_listener_rule.inventory` only (Release routing card; same weights as `alb-routing.sh`);
- `elasticloadbalancing:DescribeRules` on `*` (read-back; the action has no resource types, so it cannot be ARN-scoped);
- FireLens log writes to the demo-control CloudWatch group only.

The task also carries the identity-only security group `<name>-demo-control`; the VM security group admits Kafka
Connect REST (TCP 8083, published by `compose.hybrid.yaml`) from that group alone (Store feed card). The task gets
`CONNECT_URL` and the `ALB_INVENTORY_*` ARNs as plain environment values; credentials come from the task role.

Primary source for the IAM resource types: AWS Service Authorization Reference for Elastic Load Balancing V2
(machine-readable `https://servicereference.us-east-1.amazonaws.com/v1/elasticloadbalancing/elasticloadbalancing.json`,
accessed 2026-10-05): `ModifyRule` lists `listener-rule/app` and `listener-rule/net`; `DescribeRules` lists none.

## Offline validation

```sh
terraform -chdir=terraform/aws init -backend=false
terraform -chdir=terraform/aws validate
python3 terraform/aws/tests/test_static_guards.py
```

A real plan requires ordinary non-secret input values and AWS credentials but must not be used to write or inspect SSM secret values.
