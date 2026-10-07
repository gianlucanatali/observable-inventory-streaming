output "repositories" {
  description = "ECR repository URL by image name."
  value       = { for name, repository in aws_ecr_repository.image : name => repository.repository_url }
}
