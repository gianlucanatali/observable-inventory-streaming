# One EC2 instance running the whole overlay stack with Docker Compose (plan section 8).
# Default VPC and subnet, public IPv4, no NAT gateway, no RDS, no managed Redis.

locals {
  name = "dd-demo-${var.stack}"
  # Resources of the VM belong to layer core; project, stack and owner come from provider default_tags.
  layer_tags = { layer = "core" }

  # t4g/m6g/c6g... = Graviton (arm64); everything else treated as x86_64.
  arch = can(regex("^[a-z]+[0-9]+g[a-z]*\\.", var.instance_type)) ? "arm64" : "amd64"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${local.arch}-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "main" {
  key_name   = local.name
  public_key = file(pathexpand(var.ssh_public_key_path))
  tags       = local.layer_tags

  # Guard: the state in use must be the one of this stack (terraform workspace new <stack>).
  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but var.stack is '${var.stack}'. Select the stack's own workspace: terraform workspace select ${var.stack} (create it once with terraform workspace new ${var.stack}). This stops one stack's state being applied to another."
    }
  }
}

resource "aws_security_group" "main" {
  name        = local.name
  description = "dd-demo: SSH, HTTP, HTTPS from the presenter only"
  vpc_id      = data.aws_vpc.default.id
  tags        = merge(local.layer_tags, { Name = local.name })
}

resource "aws_vpc_security_group_ingress_rule" "ports" {
  for_each = toset(["22", "80", "443"])

  security_group_id = aws_security_group.main.id
  description       = "TCP ${each.value} from the presenter"
  cidr_ipv4         = var.presenter_cidr
  tags              = local.layer_tags
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "control_center" {
  count = var.enable_control_center ? 1 : 0

  security_group_id = aws_security_group.main.id
  description       = "TCP 9021 from the presenter (Control Center)"
  cidr_ipv4         = var.presenter_cidr
  tags              = { layer = "control-center" }
  from_port         = 9021
  to_port           = 9021
  ip_protocol       = "tcp"
}

# ---------------------------------------------------------------------------------------------
# Layer dd-synthetics (contracts section 13b): Datadog-managed public locations call the VM on port 80.
# Opened only to the published Synthetics IPv4 ranges of the chosen location, only while the layer is on.
# Source: https://ip-ranges.datadoghq.eu/synthetics.json (EU site), key synthetics.prefixes_ipv4_by_location.
# ---------------------------------------------------------------------------------------------
data "http" "dd_synthetics_ranges" {
  count = var.enable_dd_synthetics ? 1 : 0

  url = "https://ip-ranges.datadoghq.eu/synthetics.json"

  request_headers = {
    Accept = "application/json"
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Fetching Datadog's Synthetics IP ranges (https://ip-ranges.datadoghq.eu/synthetics.json) returned HTTP ${self.status_code}. Port 80 stays closed to Synthetics until this works."
    }
  }
}

locals {
  dd_synthetics_cidrs = var.enable_dd_synthetics ? toset(
    lookup(jsondecode(data.http.dd_synthetics_ranges[0].response_body).synthetics.prefixes_ipv4_by_location, var.synthetics_location, [])
  ) : toset([])
}

resource "aws_vpc_security_group_ingress_rule" "dd_synthetics" {
  for_each = local.dd_synthetics_cidrs

  security_group_id = aws_security_group.main.id
  description       = "TCP 80 from Datadog Synthetics ${var.synthetics_location}"
  cidr_ipv4         = each.value
  tags              = { layer = "dd-synthetics" }
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

# Fails the plan loudly when the location has no ranges (an empty for_each would silently open nothing).
resource "terraform_data" "dd_synthetics_guard" {
  count = var.enable_dd_synthetics ? 1 : 0

  lifecycle {
    precondition {
      condition     = length(local.dd_synthetics_cidrs) > 0
      error_message = "No Synthetics IPv4 prefixes found for location '${var.synthetics_location}' in https://ip-ranges.datadoghq.eu/synthetics.json (keys of synthetics.prefixes_ipv4_by_location, for example aws:eu-central-1)."
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.main.id
  description       = "Outbound to Confluent Cloud, Datadog, package repos"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  tags              = local.layer_tags
}

# ---------------------------------------------------------------------------------------------
# Instance role: SSM for administration, optional Bedrock invoke on named models only
# ---------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "main" {
  name               = local.name
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.layer_tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.main.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "bedrock" {
  count = length(var.bedrock_model_arns) > 0 ? 1 : 0

  statement {
    sid       = "InvokeNamedModels"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = var.bedrock_model_arns
  }
}

resource "aws_iam_role_policy" "bedrock" {
  count = length(var.bedrock_model_arns) > 0 ? 1 : 0

  name   = "bedrock-invoke"
  role   = aws_iam_role.main.id
  policy = data.aws_iam_policy_document.bedrock[0].json
}

resource "aws_iam_instance_profile" "main" {
  name = local.name
  role = aws_iam_role.main.name
  tags = local.layer_tags
}

# ---------------------------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------------------------
resource "aws_instance" "main" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  key_name                    = aws_key_pair.main.key_name
  vpc_security_group_ids      = [aws_security_group.main.id]
  iam_instance_profile        = aws_iam_instance_profile.main.name
  associate_public_ip_address = true

  user_data                   = file("${path.module}/user_data.sh")
  user_data_replace_on_change = true

  credit_specification {
    cpu_credits = "unlimited"
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.layer_tags, { Name = local.name })

  volume_tags = merge(local.layer_tags, { Name = local.name })
}
