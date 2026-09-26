"""
generate_presigned_url

Triggered by API Gateway (HTTP API) behind a Cognito JWT authorizer.
POST /videos/upload-url  { "title": "...", "contentType": "video/mp4" }

- Reads the caller's identity from the verified JWT claims (API Gateway has
  already checked the token's signature/issuer/audience before this runs).
- Generates a pre-signed S3 PUT URL, scoped to one object key under the
  caller's own prefix, valid for a short time window.
- Writes a "pending" metadata row to DynamoDB so the catalog can show an
  in-progress upload before process_upload validates it.

This function's IAM role only has s3:PutObject on this bucket and
dynamodb:PutItem on this table - nothing else.
"""
import json
import os
import time
import uuid

import boto3
from botocore.config import Config

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
dynamodb = boto3.resource("dynamodb")

BUCKET_NAME = os.environ["VIDEO_BUCKET_NAME"]
TABLE_NAME = os.environ["METADATA_TABLE_NAME"]
URL_EXPIRY_SECONDS = int(os.environ.get("URL_EXPIRY_SECONDS", "300"))
MAX_TITLE_LENGTH = 200

table = dynamodb.Table(TABLE_NAME)

# Extension is derived from content type so the object key always has one,
# which process_upload and any future player relies on.
ALLOWED_CONTENT_TYPES = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        owner_id = claims["sub"]
    except (KeyError, TypeError):
        return _response(401, {"message": "Missing or invalid authorization token"})

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"message": "Request body must be valid JSON"})

    title = (payload.get("title") or "").strip()
    content_type = payload.get("contentType", "")

    if not title or len(title) > MAX_TITLE_LENGTH:
        return _response(
            400, {"message": f"'title' is required (max {MAX_TITLE_LENGTH} characters)"}
        )

    if content_type not in ALLOWED_CONTENT_TYPES:
        return _response(
            400,
            {
                "message": "Unsupported contentType",
                "allowed": sorted(ALLOWED_CONTENT_TYPES.keys()),
            },
        )

    video_id = str(uuid.uuid4())
    extension = ALLOWED_CONTENT_TYPES[content_type]
    # Prefix by owner so IAM/S3-level per-user scoping is possible later,
    # and so process_upload can recover the video_id from the key alone.
    object_key = f"uploads/{owner_id}/{video_id}.{extension}"

    try:
        upload_url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": BUCKET_NAME,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=URL_EXPIRY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - log and return a clean 500
        print(f"generate_presigned_url failed: {exc}")
        return _response(500, {"message": "Could not generate an upload URL"})

    now_epoch = int(time.time())
    try:
        table.put_item(
            Item={
                "videoId": video_id,
                "ownerId": owner_id,
                "title": title,
                "objectKey": object_key,
                "contentType": content_type,
                "status": "pending",
                "uploadedAt": str(now_epoch),
            },
            ConditionExpression="attribute_not_exists(videoId)",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"DynamoDB put_item failed: {exc}")
        return _response(500, {"message": "Could not create the video record"})

    return _response(
        200,
        {
            "videoId": video_id,
            "uploadUrl": upload_url,
            "expiresIn": URL_EXPIRY_SECONDS,
            "objectKey": object_key,
        },
    )
