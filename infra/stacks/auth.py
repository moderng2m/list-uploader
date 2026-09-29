"""Cognito user pool and sign-in (SPEC §2, OQ-7).

The SPA signs in with the authorization-code flow and PKCE through Cognito's hosted
sign-in page; there is no client secret. Corporate SSO is a SAML identity provider
added when `saml_idp` is configured; dev uses local Cognito users. App admins are
members of the `admin` group, whichever way they sign in.
"""

from __future__ import annotations

from aws_cdk import Aws, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from infra.config import EnvConfig

ADMIN_GROUP = "admin"


class AuthStack(Stack):
    def __init__(
        self, scope: Construct, cid: str, *, cfg: EnvConfig, web_origin: str, **kwargs: object
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"list-uploader-{cfg.name}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12, temp_password_validity=Duration.days(3)
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # The prefix must be unique in the region, so it carries the account ID.
        self.domain = self.user_pool.add_domain(
            "SignIn",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"list-uploader-{cfg.name}-{Aws.ACCOUNT_ID}"
            ),
        )

        providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        idp: cognito.UserPoolIdentityProviderSaml | None = None
        if cfg.saml_idp:
            idp = cognito.UserPoolIdentityProviderSaml(
                self,
                "Sso",
                user_pool=self.user_pool,
                name=cfg.saml_idp.name,
                metadata=cognito.UserPoolIdentityProviderSamlMetadata.url(
                    cfg.saml_idp.metadata_url
                ),
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other(cfg.saml_idp.email_attribute)
                ),
            )
            providers.append(cognito.UserPoolClientIdentityProvider.custom(cfg.saml_idp.name))

        self.client = self.user_pool.add_client(
            "WebClient",
            generate_secret=False,
            prevent_user_existence_errors=True,
            # Hosted sign-in only; no password flows from the browser.
            auth_flows=cognito.AuthFlow(),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[f"{web_origin}/auth/callback"],
                logout_urls=[f"{web_origin}/"],
            ),
            supported_identity_providers=providers,
            id_token_validity=Duration.minutes(60),
            access_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.hours(12),
            enable_token_revocation=True,
        )
        # With no flows listed, Cognito would default to allowing SRP and custom auth
        # too; say explicitly that only refresh works outside the hosted page.
        cfn_client = self.client.node.default_child
        assert isinstance(cfn_client, cognito.CfnUserPoolClient)
        cfn_client.explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]
        if idp is not None:
            self.client.node.add_dependency(idp)
        cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name=ADMIN_GROUP,
            description="Marketing Tech Ops admins (SPEC §2)",
        )
