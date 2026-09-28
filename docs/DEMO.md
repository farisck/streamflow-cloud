# StreamFlow Cloud — Demo Script

A repeatable walkthrough that proves each claim in the proposal against the
live AWS deployment. Run it top to bottom in **Windows PowerShell** from the
repo root. Total time is roughly 20 minutes, most of it short waits for
alarms and log delivery.

Nothing here hardcodes account IDs, endpoints, or secrets. Section 0 reads
everything from the CloudFormation stack outputs.

## What each step proves

| # | Proof point (from the proposal) | Section | Needs |
|---|---|---|---|
| 1 | A user signs in, uploads, and the video appears in the catalog with correct metadata | 1 | Phases 1, 3 |
| 2 | Bad requests are rejected before any work is done | 2 | Phase 3 |
| 3 | Uploads are re-validated server-side, and rejected files are deleted | 3 (optional) | Phase 3 |
| 4 | Each function has only the permissions it needs | 4 | Phases 1, 3 |
| 5 | The bucket is private and data is encrypted at rest | 5 | Phase 1 |
| 6 | Playback is served from a CloudFront edge, and S3 cannot be read directly | 6 | Phase 2 |
| 7 | The system absorbs a burst of requests with no manual scaling | 7 | Phase 3 |
| 8 | Failures alert, actions are audited, and spend is watched | 8 | Phase 4 |

---

## 0. Setup

Reads endpoints and resource names from the stacks, signs in as the demo
user, and defines a few helpers used throughout.

```powershell
$REGION     = "us-east-1"
$ACCOUNT_ID = aws sts get-caller-identity --query Account --output text

function Get-Out($stack, $key) {
  aws cloudformation describe-stacks --stack-name $stack --region $REGION `
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}

$API       = Get-Out "streamflow-dev-compute"    "ApiEndpoint"
$POOL_ID   = Get-Out "streamflow-dev-foundation" "UserPoolId"
$CLIENT_ID = Get-Out "streamflow-dev-foundation" "UserPoolClientId"
$BUCKET    = Get-Out "streamflow-dev-foundation" "VideoBucketName"
$TABLE     = Get-Out "streamflow-dev-foundation" "VideoMetadataTableName"
"$API`n$BUCKET`n$TABLE"        # sanity check: all three should print
```

Sign in (the ID token is valid for one hour; re-run this block if it expires):

```powershell
$DEMO_USER = "demo@example.com"
$DEMO_PASS = Read-Host "Demo user password"
$auth = aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH `
  --client-id $CLIENT_ID --auth-parameters "USERNAME=$DEMO_USER,PASSWORD=$DEMO_PASS" `
  --region $REGION | ConvertFrom-Json
$idToken = $auth.AuthenticationResult.IdToken
$idToken.Substring(0,20)       # should print the start of a JWT (eyJ...)
```

Helpers. JSON goes through small files because PowerShell mangles inline
quotes when calling native tools like `curl.exe` and `aws`:

```powershell
function New-Upload([string]$Title = "Demo video", [string]$Type = "video/mp4") {
  Set-Content -Path body.json -Value ('{"title": "' + $Title + '", "contentType": "' + $Type + '"}') -Encoding ascii -NoNewline
  curl.exe -s -X POST "$API/videos/upload-url" -H "Authorization: Bearer $idToken" `
    -H "Content-Type: application/json" -d "@body.json" | ConvertFrom-Json
}

function Send-Upload($Resp, [string]$File = "C:\Windows\win.ini") {
  $u = $Resp.uploadUrl
  curl.exe -s -o NUL -w "PUT -> HTTP %{http_code}`n" -X PUT "$u" -H "Content-Type: video/mp4" --data-binary "@$File"
}

function Get-VideoStatus([string]$VideoId) {
  Set-Content -Path key.json -Value ('{"videoId": {"S": "' + $VideoId + '"}}') -Encoding ascii -NoNewline
  aws dynamodb get-item --table-name $TABLE --key file://key.json --region $REGION `
    --query "Item.{title:title.S,status:status.S,sizeBytes:fileSizeBytes.N}" --output json | ConvertFrom-Json
}
```

> `win.ini` is used as a tiny stand-in file. Any small file works because the
> pipeline checks size and declared content type, not the video codec. To use
> a real clip, pass `-File "C:\path\to\small.mp4"` to `Send-Upload`.

---

## 1. Sign in, upload, validate, browse

**Shows:** Cognito auth, pre-signed upload, S3-triggered validation, and the
catalog, end to end.

```powershell
# 1a. Ask the API for an upload URL (writes a "pending" row)
$up = New-Upload "Demo video"
$up | Select-Object videoId, expiresIn, objectKey

# 1b. Upload directly to S3 (the video bytes never touch Lambda)
Send-Upload $up                # expect: PUT -> HTTP 200

# 1c. S3 fires process_upload; give it a few seconds, then check the row
Start-Sleep 6
Get-VideoStatus $up.videoId    # expect: status = validated, sizeBytes = 92
```

Browse the catalog (all validated videos) and the caller's own uploads:

```powershell
curl.exe -s "$API/videos" -H "Authorization: Bearer $idToken" | ConvertFrom-Json |
  Select-Object -ExpandProperty videos | Format-Table title, status, fileSizeBytes, videoId

curl.exe -s "$API/videos?mine=true" -H "Authorization: Bearer $idToken" | ConvertFrom-Json |
  Select-Object -ExpandProperty videos | Format-Table title, status, fileSizeBytes, videoId
```

Also check the inbox for the SNS email, subject "Video validated: ...".

---

## 2. Bad requests are stopped early

**Shows:** API Gateway's JWT authorizer and the Lambda's input validation.

```powershell
# 2a. No token -> rejected by API Gateway before any code runs
curl.exe -s -i "$API/videos" | Select-Object -First 1          # expect: HTTP/2 401

# 2b. Unsupported content type -> 400 with the list of allowed types
New-Upload "Bad type" "video/avi"                              # expect: "Unsupported contentType"

# 2c. Missing title -> 400
Set-Content -Path body.json -Value '{"contentType": "video/mp4"}' -Encoding ascii -NoNewline
curl.exe -s -w "`nHTTP %{http_code}`n" -X POST "$API/videos/upload-url" `
  -H "Authorization: Bearer $idToken" -H "Content-Type: application/json" -d "@body.json"
```

---

## 3. Server-side rejection (optional)

**Shows:** `process_upload` re-checks the real object rather than trusting the
client, marks it `rejected`, and deletes it from S3.

The real limit is 500 MB, which is impractical to demo. This step temporarily
lowers it to 50 bytes so the 92-byte test file fails, then restores it. The
change is made outside CloudFormation, so **always run the restore block**.

```powershell
$FN  = "streamflow-dev-process-upload"
$cfg = aws lambda get-function-configuration --function-name $FN --region $REGION `
         --query "Environment.Variables" --output json | ConvertFrom-Json
$cfg                                   # confirm MAX_UPLOAD_SIZE_BYTES is listed
$orig = $cfg.MAX_UPLOAD_SIZE_BYTES

# Lower the limit
$cfg.MAX_UPLOAD_SIZE_BYTES = "50"
Set-Content -Path env.json -Value (@{Variables = $cfg} | ConvertTo-Json -Compress) -Encoding ascii -NoNewline
aws lambda update-function-configuration --function-name $FN --environment file://env.json --region $REGION --query LastUpdateStatus
aws lambda wait function-updated --function-name $FN --region $REGION

# Upload a file that is now "too big"
$bad = New-Upload "Too big demo"
Send-Upload $bad
Start-Sleep 6
Get-VideoStatus $bad.videoId                       # expect: status = rejected
aws s3 ls "s3://$BUCKET/$($bad.objectKey)"         # expect: no output (object deleted)
```

The rejected row is visible under `?mine=true` but absent from the public
catalog. Then **restore**:

```powershell
$cfg.MAX_UPLOAD_SIZE_BYTES = $orig
Set-Content -Path env.json -Value (@{Variables = $cfg} | ConvertTo-Json -Compress) -Encoding ascii -NoNewline
aws lambda update-function-configuration --function-name $FN --environment file://env.json --region $REGION --query LastUpdateStatus
aws lambda wait function-updated --function-name $FN --region $REGION
aws lambda get-function-configuration --function-name $FN --region $REGION --query "Environment.Variables.MAX_UPLOAD_SIZE_BYTES"
```

---

## 4. Least privilege

**Shows:** each function's role allows exactly its job and nothing else.
`simulate-principal-policy` asks IAM directly what a role may do.

```powershell
$tableArn = aws dynamodb describe-table --table-name $TABLE --region $REGION --query Table.TableArn --output text

# The catalog function can read the table but not write to it
$listRole = aws iam get-role --role-name streamflow-dev-list-videos-role --query Role.Arn --output text
aws iam simulate-principal-policy --policy-source-arn $listRole `
  --action-names dynamodb:Query dynamodb:PutItem dynamodb:DeleteItem --resource-arns $tableArn `
  --query "EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}" --output table
# expect: Query = allowed, PutItem/DeleteItem = implicitDeny

# The upload-URL function can put objects under uploads/ but not read or delete them
$putRole = aws iam get-role --role-name streamflow-dev-presigned-url-role --query Role.Arn --output text
aws iam simulate-principal-policy --policy-source-arn $putRole `
  --action-names s3:PutObject s3:GetObject s3:DeleteObject --resource-arns "arn:aws:s3:::$BUCKET/uploads/test.mp4" `
  --query "EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}" --output table
# expect: PutObject = allowed, GetObject/DeleteObject = implicitDeny
```

Show the actual policy document for one role:

```powershell
aws iam get-role-policy --role-name streamflow-dev-list-videos-role --policy-name scoped-access --query PolicyDocument
```

---

## 5. Private bucket and encryption at rest

**Shows:** no public access to the video bucket, and encryption on both stores.

```powershell
aws s3api get-public-access-block --bucket $BUCKET --query PublicAccessBlockConfiguration
# expect: all four settings true

aws s3api get-bucket-encryption --bucket $BUCKET `
  --query "ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm"
# expect: AES256

aws dynamodb describe-table --table-name $TABLE --region $REGION --query "Table.SSEDescription.{Status:Status,Type:SSEType}"
# expect: ENABLED / KMS. If null, the table is still encrypted with the default AWS-owned key.

# An unauthenticated request straight to S3 is refused
curl.exe -s -i "https://$BUCKET.s3.amazonaws.com/$($up.objectKey)" | Select-Object -First 1   # expect: HTTP/1.1 403
```

---

## 6. Edge streaming through CloudFront

**Needs Phase 2 deployed.** Skip until the account verification clears.

**Shows:** playback comes from a CloudFront edge (cache miss, then hit), and
the S3 origin is only reachable through the distribution.

```powershell
$CF = Get-Out "streamflow-dev-cloudfront" "DistributionDomainName"
$key = $up.objectKey

# First request pulls from S3 (Miss); the second is served from the edge (Hit)
1..2 | ForEach-Object {
  curl.exe -s -I "https://$CF/$key" | Select-String "HTTP|x-cache|content-type"
  "---"
}
# expect: HTTP/2 200, then x-cache: Miss from cloudfront -> Hit from cloudfront

# Direct S3 access is still denied
curl.exe -s -i "https://$BUCKET.s3.amazonaws.com/$key" | Select-Object -First 1   # expect: 403
```

Once the distribution exists, switch its pricing plan to **Free** in the
CloudFront console (CloudFormation cannot set this yet).

---

## 7. Absorbing a burst

**Shows:** Lambda and DynamoDB scale automatically. Twenty parallel catalog
requests, with no capacity provisioned or changed.

```powershell
$jobs = 1..20 | ForEach-Object {
  Start-Job -ScriptBlock {
    param($u, $t)
    curl.exe -s -o NUL -w "%{http_code}`n" -H "Authorization: Bearer $t" $u
  } -ArgumentList "$API/videos", $idToken
}
$jobs | Wait-Job | Receive-Job | Group-Object | Select-Object Name, Count   # expect: 200 x 20
$jobs | Remove-Job
```

Then look at the invocation metric (CloudWatch lags by a minute or two):

```powershell
$start = (Get-Date).AddMinutes(-15).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$end   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations `
  --dimensions Name=FunctionName,Value=streamflow-dev-list-videos `
  --start-time $start --end-time $end --period 300 --statistics Sum --region $REGION `
  --query "Datapoints[].{Time:Timestamp,Invocations:Sum}" --output table
```

---

## 8. Alerting, audit, and cost

**Needs Phase 4 deployed.** The SNS email subscription must be confirmed.

**Shows:** a real failure raises an alarm and an email, actions are
recorded, and spend is capped.

```powershell
# 8a. The alarms exist and are healthy
aws cloudwatch describe-alarms --alarm-name-prefix streamflow-dev --region $REGION `
  --query "MetricAlarms[].{Alarm:AlarmName,State:StateValue}" --output table
```

Force a genuine Lambda error by invoking `process_upload` with a malformed
event:

```powershell
Set-Content -Path bad-event.json -Value '{"Records":[{"bogus":true}]}' -Encoding ascii -NoNewline
aws lambda invoke --function-name streamflow-dev-process-upload --payload file://bad-event.json `
  --cli-binary-format raw-in-base64-out --region $REGION out.json `
  --query "{Status:StatusCode,Error:FunctionError}"          # expect: Error = Unhandled
```

Alarms evaluate on a 5-minute period, so wait 5 to 6 minutes, then:

```powershell
aws cloudwatch describe-alarms --alarm-names streamflow-dev-process-upload-errors --region $REGION `
  --query "MetricAlarms[0].StateValue"                        # expect: ALARM
```

An email titled `ALARM: "streamflow-dev-process-upload-errors"` should arrive.
The alarm returns to OK on its own after a clean period.

```powershell
# 8b. Audit trail: recording, multi-region, tamper-evident
aws cloudtrail get-trail-status --name streamflow-dev-trail --region $REGION --query "{Logging:IsLogging,LatestDelivery:LatestDeliveryTime}"
aws cloudtrail get-trail --name streamflow-dev-trail --region $REGION --query "Trail.{MultiRegion:IsMultiRegionTrail,LogValidation:LogFileValidationEnabled}"

# Who changed what on the function (events can lag up to ~15 minutes)
aws cloudtrail lookup-events --lookup-attributes AttributeKey=ResourceName,AttributeValue=streamflow-dev-process-upload `
  --max-results 5 --region $REGION --query "Events[].{Time:EventTime,Event:EventName,User:Username}" --output table

# 8c. Budget and spend to date
aws budgets describe-budgets --account-id $ACCOUNT_ID `
  --query "Budgets[].{Name:BudgetName,Limit:BudgetLimit.Amount,Spent:CalculatedSpend.ActualSpend.Amount}" --output table
```

Finish by opening **Billing and Cost Management > Cost Explorer** in the
console to show the account is still at or near $0.

---

## Cleanup after a run

```powershell
Remove-Item body.json, key.json, env.json, bad-event.json, out.json -ErrorAction SilentlyContinue
```

The demo uploads stay in S3 and DynamoDB. That is fine and keeps the catalog
populated. To remove one: `aws s3 rm s3://$BUCKET/<objectKey>` and
`aws dynamodb delete-item --table-name $TABLE --key file://key.json`.

## Troubleshooting

- **`401` on every call:** the ID token expired (one-hour lifetime). Re-run
  the sign-in block in section 0.
- **Empty `$API` or `$BUCKET`:** the stack name or output key doesn't match.
  Run `aws cloudformation list-stacks` and adjust `Get-Out`.
- **Status stays `pending`:** the S3 notification did not fire. Check the
  `streamflow-dev-process-upload` log group in CloudWatch.
- **Inline JSON errors:** PowerShell strips quotes when calling native tools.
  Always pass JSON through a file (`-d "@file"`, `file://file`) as above.
- The helper files (`body.json`, `key.json`, `env.json`, ...) are created in
  the current folder. Add them to `.gitignore` so they are never committed.
