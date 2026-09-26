"""
custom_resource_s3_notify

A CloudFormation custom resource. The video bucket lives in the Foundation
stack (01-foundation.yaml) and was created before process_upload existed, so
its NotificationConfiguration can't be set as a plain resource property
without creating a circular dependency between the bucket and this stack's
Lambda function. This custom resource calls
s3:PutBucketNotificationConfiguration directly via boto3 instead.

On Delete, it clears the bucket's notification configuration so the
Foundation stack's bucket doesn't retain a dangling reference to a Lambda
that this stack is tearing down.

No external dependencies (no `cfnresponse` layer needed) - the response to
CloudFormation's pre-signed S3 URL is sent with the standard library only.
"""
import json
import urllib.request

import boto3

s3 = boto3.client("s3")


def send_response(event, context, status, reason=None, physical_resource_id=None):
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Logs: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id or event.get("LogicalResourceId"),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": {},
    }
    encoded = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url=event["ResponseURL"],
        data=encoded,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(encoded))},
    )
    urllib.request.urlopen(request)


def handler(event, context):
    props = event.get("ResourceProperties", {})
    bucket_name = props["BucketName"]
    lambda_arn = props["LambdaArn"]
    upload_prefix = props.get("UploadPrefix", "uploads/")
    physical_id = f"{bucket_name}-notification-config"

    try:
        request_type = event["RequestType"]

        if request_type in ("Create", "Update"):
            s3.put_bucket_notification_configuration(
                Bucket=bucket_name,
                NotificationConfiguration={
                    "LambdaFunctionConfigurations": [
                        {
                            "LambdaFunctionArn": lambda_arn,
                            "Events": ["s3:ObjectCreated:*"],
                            "Filter": {
                                "Key": {
                                    "FilterRules": [
                                        {"Name": "prefix", "Value": upload_prefix}
                                    ]
                                }
                            },
                        }
                    ]
                },
            )
        elif request_type == "Delete":
            try:
                s3.put_bucket_notification_configuration(
                    Bucket=bucket_name, NotificationConfiguration={}
                )
            except Exception as exc:  # noqa: BLE001
                # The bucket may already be gone during a full teardown -
                # don't let that block stack deletion.
                print(f"Non-fatal error clearing notification config: {exc}")

        send_response(event, context, "SUCCESS", physical_resource_id=physical_id)

    except Exception as exc:  # noqa: BLE001
        print(f"Custom resource failed: {exc}")
        send_response(event, context, "FAILED", reason=str(exc), physical_resource_id=physical_id)
