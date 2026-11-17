# AWS deployment placeholders

This folder reserves entry points for AWS infrastructure-as-code. Each subfolder holds only a
`.gitkeep` file; no AWS deployment exists yet.

- `cdk/` - an AWS CDK app for ECS or EKS, queues, object storage and managed databases.
- `terraform/` - Terraform modules for platform-managed deployments.
- `step-functions/` - Step Functions workflow definitions for serverless ingestion
  orchestration.

Validate the local Docker Compose path before implementing cloud infrastructure; see
[Deployment](../../docs/developers/deployment/README.md).
