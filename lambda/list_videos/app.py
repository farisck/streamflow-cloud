"""
StreamFlow Cloud - list-videos Lambda

Triggered by: API Gateway HTTP API, GET /videos (Cognito JWT authorizer)

Two modes, chosen by query string:
  - Default: public catalog mode. Queries the status-index GSI (status hash
    key, uploadedAt range key) for status="validated" only - powers a
    "browse all videos" screen. Can never expose pending/rejected rows.
  - ?mine=true: private mode. Queries the owner-index GSI (ownerId hash key,
    uploadedAt range key) for the caller's own uploads at ANY status.

Supports cursor-based pagination via "cursor" (returned as "nextCursor") and
an optional "limit" (default DEFAULT_PAGE_SIZE).

Environment variables:
  METADATA_TABLE_NAME - DynamoDB table for video metadata
  DEFAULT_PAGE_SIZE (optional, default 20)
"""

import base64
import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["METADATA_TABLE_NAME"]
DEFAULT_PAGE_SIZE = int(os.environ.get("DEFAULT_PAGE_SIZE", "20"))
OWNER_INDEX = "owner-index"
STATUS_INDEX = "status-index"

table = dynamodb.Table(TABLE_NAME)


def _response(status_code, body_dict):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body_dict, default=_json_default),
    }


def _encode_cursor(last_evaluated_key):
    if not last_evaluated_key:
        return None
    raw = json.dumps(last_evaluated_key).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_cursor(cursor):
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("utf-8"))
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _to_video_summary(item):
    return {
        "videoId": item.get("videoId"),
        "title": item.get("title", item.get("fileName")),
        "status": item.get("status"),
        "ownerId": item.get("ownerId"),
        "uploadedAt": item.get("uploadedAt"),
        "fileSizeBytes": item.get("fileSizeBytes"),
        "contentType": item.get("contentType"),
    }


def handler(event, context):
    try:
        claims = (
            event.get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
            .get("claims", {})
        )
        caller_id = claims.get("sub")
        if not caller_id:
            return _response(401, {"message": "Missing authenticated user (sub claim)."})

        query_params = event.get("queryStringParameters") or {}
        page_size = int(query_params.get("limit", DEFAULT_PAGE_SIZE))
        exclusive_start_key = _decode_cursor(query_params.get("cursor"))
        mine_only = query_params.get("mine", "false").lower() == "true"

        if mine_only:
            query_kwargs = {
                "IndexName": OWNER_INDEX,
                "KeyConditionExpression": Key("ownerId").eq(caller_id),
                "ScanIndexForward": False,
            }
        else:
            query_kwargs = {
                "IndexName": STATUS_INDEX,
                "KeyConditionExpression": Key("status").eq("validated"),
                "ScanIndexForward": False,
            }

        query_kwargs["Limit"] = page_size
        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        result = table.query(**query_kwargs)
        videos = [_to_video_summary(item) for item in result.get("Items", [])]

        return _response(
            200,
            {
                "mode": "mine" if mine_only else "catalog",
                "videos": videos,
                "nextCursor": _encode_cursor(result.get("LastEvaluatedKey")),
            },
        )

    except Exception as exc:  # noqa: BLE001
        print(f"ERROR list_videos: {exc}")
        return _response(500, {"message": "Internal error listing videos."})