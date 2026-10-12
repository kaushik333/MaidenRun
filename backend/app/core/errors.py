from typing import NoReturn

from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.aws.credentials import NoCredentialsError
from app.aws.ec2 import Ec2LaunchError, HealthCheckTimeoutError
from app.core.logging import logger

# AWS error codes that actually mean "this access key/secret is wrong" — not
# every ClientError is a credentials problem (e.g. a bad instance profile
# name isn't), so only these get the "AWS rejected these credentials" framing.
CREDENTIAL_ERROR_CODES = {
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "AuthFailure",
    "SignatureDoesNotMatch",
}
# Valid credentials, but the IAM policy doesn't allow this action — almost
# always a policy set up before the platform needed that permission.
PERMISSION_ERROR_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
OUTDATED_POLICY_HINT = (
    "Your IAM policy doesn't allow this yet — replace the inline policy on your MaidenRun IAM user with the "
    "current infra/iam-policy.json (also shown in the app's AWS one-time setup)."
)


def classify_error(exc: Exception) -> tuple[int, str]:
    """Maps any exception from a provisioning step to (status_code, detail).

    Shared by every provisioning/teardown flow (app deploy, feedback-stack
    deploy, terminate) so 'your credentials are wrong' vs 'AWS rejected this
    specific request' vs 'it launched but never answered' is classified the
    same way everywhere. The status_code half is only meaningful for a
    synchronous error response (e.g. /terminate) — a background deployment's
    failure is reported via its polled JSON body, not an HTTP error status,
    but the same message classification still applies. Anything not
    otherwise recognized still gets classified rather than left to propagate
    unhandled — an unhandled exception's response skips CORSMiddleware
    entirely and the browser reports it as a bare network failure, hiding
    whatever actually went wrong.
    """
    if isinstance(exc, NoCredentialsError):
        return 401, str(exc)
    if isinstance(exc, ClientError):
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"]["Message"]
        if code in CREDENTIAL_ERROR_CODES:
            return 401, f"AWS rejected these credentials: {message}"
        if code in PERMISSION_ERROR_CODES:
            return 403, f"{OUTDATED_POLICY_HINT} AWS said: {message}"
        return 400, f"AWS rejected the request: {message}"
    # Checked before the (broader) Ec2LaunchError below, since it's a subclass:
    # provisioned fine, just never answered — a narrower, 504-shaped problem.
    if isinstance(exc, HealthCheckTimeoutError):
        return 504, str(exc)
    if isinstance(exc, Ec2LaunchError):
        return 502, str(exc)
    return 500, str(exc)


def reraise_as_http_error(exc: ClientError) -> NoReturn:
    """Thin wrapper for single-exception, synchronous call sites (/terminate)."""
    logger.exception("AWS rejected the request")
    status_code, detail = classify_error(exc)
    raise HTTPException(status_code=status_code, detail=detail) from exc
