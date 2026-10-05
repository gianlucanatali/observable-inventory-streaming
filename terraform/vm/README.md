# terraform/vm

One EC2 instance for the whole overlay stack. Default VPC and subnet, no NAT gateway, no RDS, no managed Redis.

## Creates

- EC2 `t4g.xlarge` (Graviton, arm64, var `instance_type`) with `cpu_credits = "unlimited"`. Alternative: `t3.xlarge` (x86_64). Price and architecture fit are being verified: confirm before apply. The AMI architecture follows the instance family.
- Latest Ubuntu 24.04 LTS AMI (Canonical, owner 099720109477) for that architecture.
- gp3 root volume (default 40 GB), encrypted. IMDSv2 required.
- `user_data.sh`: Docker Engine and compose plugin from Docker's official apt repo; log at `/var/log/dd-demo-bootstrap.log`.
- Security group: TCP 22, 80, 443 from `presenter_cidr` only; all egress.
- Key pair from `ssh_public_key_path`.
- IAM role + instance profile: `AmazonSSMManagedInstanceCore`; Bedrock `InvokeModel`/`InvokeModelWithResponseStream` only on `bedrock_model_arns` (default empty, no statement).
- Names `dd-demo-<stack>`. Tags (provider `default_tags`): `project=dd-demo`, `stack=<stack>`, `owner=<var>`; per resource `layer=core`.

## Items to price before apply (do not assume figures)

- EC2 hourly for the chosen type, plus unlimited-credit surplus CPU exposure.
- EBS gp3 capacity.
- Public IPv4 hourly charge and data transfer out.

## Commands for the human

```sh
export AWS_PROFILE=dd-demo   # aws login --profile dd-demo if expired (~12 h)
cd terraform/vm
terraform init
terraform workspace new <stack>   # once per stack; later: terraform workspace select <stack>
terraform plan -var stack=<stack> -var owner=<your-name> -var presenter_cidr="$(curl -s https://checkip.amazonaws.com)/32"
terraform apply  -var stack=<stack> -var owner=<your-name> -var presenter_cidr="<your-ip>/32"   # explicit approval + cost note first
terraform output -raw docker_context_hint        # then wait for cloud-init, see below
ssh ubuntu@$(terraform output -raw public_ip) cloud-init status --wait
```

After a destroy, list leftover EC2, EBS, Elastic IPs, NAT gateways and RDS resources.

## Notes

- Only `terraform init -backend=false`, `fmt` and `validate` were run. Before deployment, run a plan to confirm that the `t4g` Ubuntu arm64 AMI name filter (`ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*`) still matches an available 24.04 image.
- Reaching the VM on 80/443 requires `presenter_cidr` to match your current public IP; update and re-apply if it changes.
- `stack` is required and must equal the Terraform workspace (precondition on the key pair). The variable `name` is gone: names are `dd-demo-<stack>`. No layer toggles here: the Synthetics private location runs as a container on this host. Run: `fmt`, `init -backend=false`, `validate` only.

## Layer dd-synthetics (contracts section 13b)

- Variable `enable_dd_synthetics` (default false). When true, one security-group ingress rule per IPv4 prefix is added on **TCP 80 only**, from Datadog's published Synthetics ranges for `synthetics_location` (default `aws:eu-central-1`, four /32 prefixes at the time of writing), tagged `layer = dd-synthetics`. This opens port 80 only to Datadog's Synthetics ranges and only while the layer is on; turning it off removes the rules.
- Source: `data "http"` on `https://ip-ranges.datadoghq.eu/synthetics.json` (EU site), key `synthetics.prefixes_ipv4_by_location[<location>]`. The URL and key layout were checked with a read-only fetch on 2026-10-03; the plan needs internet access. Adds the `hashicorp/http` provider (`~> 3.4`, lock file updated by `init`). A guard resource fails the plan when the location has no prefixes.
- New outputs: `ingress_base_url` (`http://<public ip>`) and `dd_synthetics_cidrs`. The public IP changes if the instance is replaced (no Elastic IP): re-run the datadog dir with the new `ingress_base_url`.
- Ranges change over time: re-run `terraform apply` of this dir before a session to refresh them.
- Run: `fmt`, `init -backend=false`, `validate` only. A live plan still needs to confirm the `http` data source behaviour with a provider download.
