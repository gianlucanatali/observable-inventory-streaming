# Container image repositories of one stack, separate from terraform/aws so that they can outlive it.
# stack-down destroys this root unless keep_images is true in demo.yaml (KEEP_IMAGES=true); then the images stay
# and the next stack-up skips every build whose content tag is already here.
# Workspace = stack. Names keep the earlier layout: dd-demo-<stack>/<image>.

locals {
  name       = "dd-demo-${var.stack}"
  layer_tags = { layer = "images" }

  # Must match app_images, log_router_image and vm_images in compose/scripts/stack.sh.
  images = toset([
    # ECS Fargate tasks
    "inventory-api", "storefront", "stock-projector", "offer-worker", "demo-control", "cost-meter", "log-router",
    # on-prem VM (compose), pulled instead of rebuilt when unchanged
    "connect", "jr", "freshness-probe", "scenario", "smoke", "supplier-sim",
  ])
}

resource "terraform_data" "workspace_guard" {
  input = var.stack

  lifecycle {
    precondition {
      condition     = terraform.workspace == var.stack
      error_message = "terraform.workspace is '${terraform.workspace}' but stack is '${var.stack}'. Create/select the matching workspace first."
    }
  }
}

resource "aws_ecr_repository" "image" {
  for_each = local.images
  name     = "${local.name}/${each.value}"
  # Content tags never change meaning; MUTABLE only so that REBUILD_IMAGES=1 can push the same tag again.
  image_tag_mutability = "MUTABLE"
  # Destroy (keep_images false) removes the images with the repository.
  force_delete = true
  tags         = local.layer_tags

  image_scanning_configuration {
    scan_on_push = true
  }

  depends_on = [terraform_data.workspace_guard]
}

# Keep only the newest content tags. Untagged images are not expired here: stack.sh never re-pushes an existing tag
# (except REBUILD_IMAGES=1), and an untagged rule could expire children of an image index (unverified for ECR).
resource "aws_ecr_lifecycle_policy" "image" {
  for_each   = aws_ecr_repository.image
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the newest ${var.kept_tags_per_image} content-tagged images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["c-"]
        countType     = "imageCountMoreThan"
        countNumber   = var.kept_tags_per_image
      }
      action = { type = "expire" }
    }]
  })
}
