"""
process_upload

Triggered directly by S3 on s3:ObjectCreated:* under the uploads/ prefix
(wired via a CloudFormation custom resource, since a bucket created in a
different stack can't have its NotificationConfiguration edited by a plain
CloudFormation property without a circular Bucket<->Lambda dependency).

For each newly-created object:
  1. HEAD the object to get its *real* size and content-type (never trust
     what the client claimed when requesting the pre-signed URL).
  2. Mark the DynamoDB row "validated" or "rejected" accordingly.
  3. Delete the object from S3 if it was rejected, so bad/oversized uploads
     don't sit around accumulating storage cost.
  4. Publish an SNS notification either way.

This function's IAM role only has s3:GetObject + s3:DeleteObject on this
bucket, dynamodb:UpdateItem on this table, and sns:Publish on this topic.
"""
import os
import urllib.parse

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

TABLE_NAME = os.environ["METADATA_TABLE_NAME"]
TOPIC_ARN = os.environ["NOTIFICATIONS_TOPIC_ARN"]
# 500 MB default cap - generous for a capstone demo, cheap to change via env var.
MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_BYTES", str(500 * 1024 * 1024)))
ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/webm"}

table = dynamodb.Table(TABLE_NAME)


def _video_id_from_key(object_key: str):
    # Keys are written as uploads/{ownerId}/{videoId}.{ext} by
    # generate_presigned_url, so the id can be recovered without a lookup.
    try:
        filename = object_key.rsplit("/", 1)[-1]
        return filename.rsplit(".", 1)[0]
    except Exception:  # noqa: BLE001
        return None


def handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        video_id = _video_id_from_key(object_key)
        if not video_id:
            print(f"Could not derive videoId from key: {object_key}")
            continue

        try:
            head = s3.head_object(Bucket=bucket, Key=object_key)
        except ClientError as exc:
            print(f"head_object failed for {object_key}: {exc}")
            continue

        size_bytes = head["ContentLength"]
        content_type = head.get("ContentType", "")
        is_valid = size_bytes <= MAX_UPLOAD_SIZE_BYTES and content_type in ALLOWED_CONTENT_TYPES
        new_status = "validated" if is_valid else "rejected"

        try:
            table.update_item(
                Key={"videoId": video_id},
                UpdateExpression="SET #s = :status, fileSizeBytes = :size",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": new_status, ":size": size_bytes},
            )
        except ClientError as exc:
            print(f"DynamoDB update_item failed for {video_id}: {exc}")
            continue

        if not is_valid:
            try:
                s3.delete_object(Bucket=bucket, Key=object_key)
                print(
                    f"Deleted rejected upload {object_key} "
                    f"(size={size_bytes}, contentType={content_type})"
                )
            except ClientError as exc:
                print(f"Failed to delete rejected object {object_key}: {exc}")

        try:
            sns.publish(
                TopicArn=TOPIC_ARN,
                Subject=f"Video {new_status}: {video_id}"[:100],
                Message=(
                    f"videoId={video_id}\n"
                    f"status={new_status}\n"
                    f"sizeBytes={size_bytes}\n"
                    f"contentType={content_type}"
                ),
            )
        except ClientError as exc:
            print(f"SNS publish failed for {video_id}: {exc}")

    return {"statusCode": 200}
