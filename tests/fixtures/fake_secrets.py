"""Fake secrets for tests/test_redact.py. None of these are real credentials.

Tokens are assembled at import time so no complete token-shaped string sits in the repo:
secret scanners (GitHub push protection, gitleaks) would otherwise flag this file.
"""

from __future__ import annotations

from typing import NamedTuple


class Leak(NamedTuple):
    id: str
    text: str  # a realistic line as it appears in logs, events or args
    secret: str  # must be gone after redaction
    kind: str  # expected placeholder kind: <REDACTED:kind>


def _t(*parts: str) -> str:
    return "".join(parts)


AWS_KEY = _t("AKIA", "Z7QFAKE0EXAMPLE5")
AWS_SECRET = _t("wJalrXUtnFEMI", "/K7MDENG/bPxRfiCYFAKEKEY")
GITHUB = _t("ghp", "_", "a" * 6, "B1c2D3e4F5g6H7i8J9k0L1m2N3o4P5")
GITHUB_PAT = _t("github", "_pat_", "11ABCDEFG0", "x" * 20, "_fake")
SLACK = _t("xox", "b-", "1234567890-0987654321-", "FakeFakeFakeFake")
GOOGLE = _t("AIza", "SyFake", "0" * 29)
STRIPE = _t("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc")
ANTHROPIC = _t("sk", "-ant-", "api03-", "FakeKeyFakeKeyFakeKey-AA")
OPENAI = _t("sk", "-proj-", "FakeProjectKey0123456789abcdef")
OPENAI_LEGACY = _t("sk", "-", "Fake" * 8, "0123456789ABCDEF")
JWT = _t(
    "eyJ",
    "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    ".eyJ",
    "zdWIiOiJwZmEtdGVzdCIsIm5hbWUiOiJmYWtlIn0",
    ".fakesignature0123456789abcdefABCDEF",
)
OPAQUE = _t("pfa", "Opaque", "Token", "9f8e7d6c5b4a")
BASIC = "dXNlcjpwZmEtZmFrZS1wYXNzd29yZA=="  # base64("user:pfa-fake-password")
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7FAKEFAKEFAKE\n" * 3
PEM_LINE = PEM_BODY.splitlines()[0]
PEM = _t("-----BEGIN ", "PRIVATE KEY-----\n", PEM_BODY, "-----END ", "PRIVATE KEY-----")
RSA_PEM = _t("-----BEGIN RSA ", "PRIVATE KEY-----\n", PEM_BODY, "-----END RSA ", "PRIVATE KEY-----")

LEAKS: list[Leak] = [
    # Tokens recognisable by their shape, wherever they appear.
    Leak("aws_access_key", f"using AWS credentials {AWS_KEY} from env", AWS_KEY, "aws_access_key"),
    Leak("github_token", f"git clone https://{GITHUB}@github.com/acme/app", GITHUB, "github_token"),
    Leak("github_pat", f"GITHUB_TOKEN {GITHUB_PAT}", GITHUB_PAT, "github_token"),
    Leak("slack_token", f"posting alert with {SLACK}", SLACK, "slack_token"),
    Leak("google_api_key", f"maps client key {GOOGLE} loaded", GOOGLE, "google_api_key"),
    Leak("stripe_key", f"payments init: {STRIPE}", STRIPE, "api_key"),
    Leak("anthropic_key", f"llm provider key {ANTHROPIC}", ANTHROPIC, "api_key"),
    Leak("openai_key", f"OPENAI {OPENAI}", OPENAI, "api_key"),
    Leak("openai_legacy_key", f"client({OPENAI_LEGACY})", OPENAI_LEGACY, "api_key"),
    Leak("jwt", f"session={JWT}", JWT, "jwt"),
    Leak("jwt_in_json", f'{{"id_token": "{JWT}", "ok": true}}', JWT, "jwt"),
    Leak("private_key", f"loaded tls key:\n{PEM}\nlistening on :8443", PEM_LINE, "private_key"),
    Leak("rsa_private_key", RSA_PEM, PEM_LINE, "private_key"),
    Leak(
        "private_key_cut_by_log_trim",
        f"{PEM_BODY}-----END PRIVATE KEY-----\nserver started",
        PEM_LINE,
        "private_key",
    ),
    # Authorization headers.
    Leak("bearer", f"GET /api 401 Authorization: Bearer {OPAQUE}", OPAQUE, "bearer_token"),
    Leak("bearer_jwt", f"authorization: bearer {JWT}", JWT, "jwt"),
    Leak("basic_auth", f"Authorization: Basic {BASIC}", BASIC, "basic_auth"),
    # Credentials in URLs: the password goes, user and host stay.
    Leak(
        "postgres_url",
        "connecting to postgres://checkout:pfa-fake-db-pass@db.prod:5432/orders",
        "pfa-fake-db-pass",
        "password",
    ),
    Leak("redis_url_no_user", "redis://:pfaFakeRedisPw@redis:6379/0", "pfaFakeRedisPw", "password"),
    Leak("amqp_url", "amqp://guest:pfa-fake-mq@rabbit//", "pfa-fake-mq", "password"),
    # key=value and key: value pairs, in their common spellings.
    Leak(
        "password_eq",
        "db config: user=checkout password=pfa-fake-secret-value",
        "pfa-fake-secret-value",
        "password",
    ),
    Leak("env_dump", "DB_PASSWORD=pfa-fake-secret-value", "pfa-fake-secret-value", "password"),
    Leak("pgpassword", "export PGPASSWORD='pfa fake quoted'", "pfa fake quoted", "password"),
    Leak("yaml_style", "  client_secret: pfaFakeClientSecret", "pfaFakeClientSecret", "secret"),
    Leak(
        "json_style",
        '{"user": "a", "password": "pfa-fake-json-pw"}',
        "pfa-fake-json-pw",
        "password",
    ),
    Leak("api_key_eq", "API_KEY=pfaFakeApiKey123", "pfaFakeApiKey123", "api_key"),
    Leak("x_api_key_header", "x-api-key: pfaFakeHeaderKey", "pfaFakeHeaderKey", "api_key"),
    Leak("access_key_spaces", "aws_secret_access_key = " + AWS_SECRET, AWS_SECRET, "api_key"),
    Leak(
        "query_string", "GET /callback?code=1&token=pfaFakeQueryTok&x=2", "pfaFakeQueryTok", "token"
    ),
    Leak(
        "connection_string",
        "Server=sql;Database=orders;User Id=sa;Password=pfaFakeSqlPw;",
        "pfaFakeSqlPw",
        "password",
    ),
    Leak(
        "cookie", "Set-Cookie: session_id=pfaFakeSession42; Path=/", "pfaFakeSession42", "session"
    ),
    Leak("flag_eq", "starting with --db-password=pfaFakeFlagPw", "pfaFakeFlagPw", "password"),
    Leak("flag_space", "exec app --api-token pfaFakeFlagTok --port 80", "pfaFakeFlagTok", "token"),
]

# Real messages the rules depend on. Redaction must leave them exactly as they are.
BENIGN: list[str] = [
    'MountVolume.SetUp failed for volume "db-password" : secret "db-password" not found',
    'Error: secret "db-creds" not found',
    "Error: couldn't find key password in Secret pfa-lab/app-secrets",
    'Failed to pull image "registry.example.com/team/app:1.0": pull access denied, repository '
    "does not exist or may require authorization: server message: insufficient_scope: "
    "authorization failed",
    "0/1 nodes are available: 1 Insufficient cpu. preemption: 0/1 nodes are available: 1 No "
    "preemption victims found for incoming pod.",
    'Liveness probe failed: Get "http://10.244.0.12:8080/healthz": dial tcp 10.244.0.12:8080: '
    "connect: connection refused",
    "Back-off restarting failed container api in pod checkout-api-7d9f8c6b5-x2k4j_pfa-lab",
    "FATAL: required environment variable DATABASE_URL is not set",
    "KeyError: 'SECRET_KEY'",
    "token validation failed: token expired",
    "using basic authentication against the registry",
    "Bearer realm=registry",
    'password authentication failed for user "checkout"',
    "sk-learn-model-server-7d9f8c6b5-x2k4j",
    "docker.io/library/busybox@sha256:" + "a" * 64,
    "password: <your-password>",
    "PASSWORD=${DB_PASSWORD}",
    "require_auth_token=true",
    "secret/app-secrets",
    "HTTP /healthz port=8080",
]
