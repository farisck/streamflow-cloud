# StreamFlow Cloud

A scalable, serverless media streaming platform on AWS — Cloud Computing capstone project.

Users sign in, upload a video, and stream it back through a global CDN, with no
persistently running server anywhere in the stack. Built to stay inside AWS
Free Tier limits.

📄 Full proposal: [`docs/StreamFlow_Cloud_Proposal.docx`](docs/StreamFlow_Cloud_Proposal.docx)

## Architecture

![Architecture diagram](docs/architecture.png)

```
User → Cognito (auth) → API Gateway → Lambda → S3 (video) + DynamoDB (metadata)
                                                     │
                                                     ▼
                                              CloudFront (edge streaming)
                                                     │
                                        CloudWatch · CloudTrail · SNS · Budgets
```

## Status

| Phase | What it builds | Status |
|---|---|---|
| 1 — Foundation | Cognito user pool, S3 video bucket, DynamoDB table, IAM role, SNS topic | ✅ Done |
| 2 — CloudFront | CDN distribution with Origin Access Control, locked-down bucket policy | ✅ Done |
| 3 — Compute | Lambda functions (upload validation, metadata write) + API Gateway routes | 🔜 Next |
| 4 — Monitoring | CloudWatch alarms, CloudTrail, Budgets alerts | 🔜 Planned |

## Repo layout

```
streamflow-cloud/
├── infra/
│   └── cloudformation/
│       ├── 01-foundation.yaml   # Cognito, S3, DynamoDB, IAM, SNS
│       └── 02-cloudfront.yaml   # CloudFront distribution + bucket policy
├── lambda/                      # Lambda function source (Phase 3)
├── docs/
│   ├── StreamFlow_Cloud_Proposal.docx
│   └── architecture.png
└── .github/workflows/           # CI (added when Phase 3 lands)
```

## Prerequisites

- An AWS account with the CLI configured (`aws configure`)
- AWS CLI v2
- Region: `us-east-1` (used throughout the templates/commands below)

## Deploying

Deploy the stacks in order — Phase 2 imports outputs from Phase 1.

```bash
# Phase 1 — Foundation
aws cloudformation deploy \
  --template-file infra/cloudformation/01-foundation.yaml \
  --stack-name streamflow-dev-foundation \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# Phase 2 — CloudFront
aws cloudformation deploy \
  --template-file infra/cloudformation/02-cloudfront.yaml \
  --stack-name streamflow-dev-cloudfront \
  --region us-east-1

# Check outputs (bucket name, table name, CloudFront domain, etc.)
aws cloudformation describe-stacks \
  --stack-name streamflow-dev-foundation \
  --query "Stacks[0].Outputs" --region us-east-1

aws cloudformation describe-stacks \
  --stack-name streamflow-dev-cloudfront \
  --query "Stacks[0].Outputs" --region us-east-1
```

CloudFront distributions take 10–15 minutes to fully deploy on first creation.

**One manual step:** in the CloudFront console, switch the distribution's
pricing plan from Pay-as-you-go to the **Free** flat-rate plan (not yet
settable via CloudFormation).

## Tearing down

Delete in reverse order, since Phase 2 depends on Phase 1's exports:

```bash
aws cloudformation delete-stack --stack-name streamflow-dev-cloudfront --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name streamflow-dev-cloudfront --region us-east-1

aws cloudformation delete-stack --stack-name streamflow-dev-foundation --region us-east-1
```

Note: S3 buckets with objects in them won't delete via CloudFormation until
emptied first (`aws s3 rm s3://<bucket-name> --recursive`).

## Cost notes

Designed to run inside AWS Free Tier. See the "Estimated Cost / Free Tier
Usage" section of the proposal for details. The main things to watch:
CloudFront data transfer past its free allowance, and a customer-managed KMS
key if one is added later (~$1/month) — both AWS-managed default encryption
keys are used for now, which carry no extra charge.

## License

This is a student capstone project; no license is asserted.
