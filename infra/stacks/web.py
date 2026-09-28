"""Static SPA on S3 + CloudFront (SPEC §4.1), deployed first so every other stack
can pin CORS and the sign-in callback to its exact origin.

The SPA itself (plus its runtime `config.json`) is copied in by SiteStack, last,
once the API and user pool exist.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import EnvConfig


def content_security_policy(region: str) -> str:
    """The SPA talks to its API, S3 (presigned upload) and Cognito (sign-in) only."""
    connect = " ".join(
        [
            "'self'",
            f"https://*.execute-api.{region}.amazonaws.com",
            "https://*.s3.amazonaws.com",
            f"https://*.s3.{region}.amazonaws.com",
            f"https://*.auth.{region}.amazoncognito.com",
        ]
    )
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src {connect}",
            "object-src 'none'",
            "base-uri 'self'",
            f"form-action 'self' https://*.auth.{region}.amazoncognito.com",
            "frame-ancestors 'none'",
        ]
    )


class WebStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: EnvConfig, **kwargs: object) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        # No PII here: only the built SPA.
        self.site_bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=content_security_policy(cfg.region), override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(730),
                    include_subdomains=True,
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )
        spa_fallback = [
            cloudfront.ErrorResponse(
                http_status=status,
                response_http_status=200,
                response_page_path="/index.html",
                ttl=Duration.seconds(0),
            )
            for status in (403, 404)
        ]
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
            ),
            error_responses=spa_fallback,
        )
        self.origin = cfg.web_origin or f"https://{self.distribution.distribution_domain_name}"
