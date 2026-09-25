# Lambda functions (Phase 3 — not yet built)

This folder will hold the two core functions:

- `upload-handler/` — issues a pre-signed S3 URL for a validated upload request
- `metadata-writer/` — triggered on S3 object creation, writes the video's
  metadata record to DynamoDB

Coming in Phase 3, along with the API Gateway routes that front them.
