
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.publisher_extension import router as publisher_extension_router
from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

  



class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://postgres:cntx%40123@localhost:5432/consent_management"
    wso2_tls_verify: bool = True
    # WSO2 Publisher REST API v4 metadata synchronization.
    wso2_api_metadata_base: str = "https://localhost:9443/api/am/publisher/v4"
    wso2_token_url: str = "https://localhost:9443/oauth2/token"
    wso2_client_id: str = ""
    wso2_client_secret: str = ""
    wso2_client_scope: str = "apim:api_view apim:api_definition_view"
    consent_internal_key: str = "CHANGE_ME"
    wso2_sync_interval_seconds: int = 60
    default_allowed_purposes: str = "business,student-verification"
    default_max_duration_days: int = 30
    host: str = "127.0.0.1"
    port: int = 8100
    # A comma-separated allow-list for local upstream hosts is recommended.
    # Leave empty only for a strictly local, synthetic-data POC.
    allowed_upstream_hosts: str = "localhost,127.0.0.1"
    allow_openapi_field_fallback: bool = False
    wso2_target_api_id: str = "6a3ee5cd-b233-4935-a70f-7e27d406007c"
    wso2_target_api_name: str = "StudentVerificationAPI"


settings = Settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiOperation(Base):
    __tablename__ = "api_operations"
    __table_args__ = (UniqueConstraint("api_id", "api_version", "operation_id", name="uq_api_version_operation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_id: Mapped[str] = mapped_column(String(255), index=True)
    api_name: Mapped[str] = mapped_column(String(255))
    api_version: Mapped[str] = mapped_column(String(80))
    publisher_subject: Mapped[str] = mapped_column(String(320), index=True)
    operation_id: Mapped[str] = mapped_column(String(255))
    http_method: Mapped[str] = mapped_column(String(10))
    resource_path: Mapped[str] = mapped_column(String(1000))
    upstream_base_url: Mapped[str] = mapped_column(String(2000))
    upstream_path: Mapped[str] = mapped_column(String(1000))
    allowed_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed_purposes_json: Mapped[str] = mapped_column(Text, default='["business"]')
    max_duration_days: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentRequest(Base):
    __tablename__ = "consent_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requester_subject: Mapped[str] = mapped_column(String(320), index=True)
    application_id: Mapped[str] = mapped_column(String(255), index=True)
    api_id: Mapped[str] = mapped_column(String(255), index=True)
    api_version: Mapped[str] = mapped_column(String(80))
    operation_id: Mapped[str] = mapped_column(String(255))
    requested_fields_json: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(255))
    duration_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    decision_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentGrant(Base):
    __tablename__ = "consent_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    requester_subject: Mapped[str] = mapped_column(String(320), index=True)
    application_id: Mapped[str] = mapped_column(String(255), index=True)
    api_id: Mapped[str] = mapped_column(String(255), index=True)
    api_version: Mapped[str] = mapped_column(String(80))
    operation_id: Mapped[str] = mapped_column(String(255))
    approved_fields_json: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "consent_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(320))
    event: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def demo_identity(
    x_demo_application_id: str | None = Header(default=None, alias="X-Demo-Application-ID"),
) -> dict:
    """No authentication for local POC; identity is deliberately synthetic."""
    app_id = (x_demo_application_id or "").strip()
    return {
        "user": "local-demo-user",
        "application_id": app_id or "local-demo-app",
        # In local demo mode the test user can exercise the Publisher UI.
        "roles": {"consent-publisher", "consent-admin", "admin", "Internal/publisher", "Internal/admin"},
    }


class OperationRegister(BaseModel):
    api_id: str = Field(min_length=1, max_length=255)
    api_name: str = Field(min_length=1, max_length=255)
    api_version: str = Field(min_length=1, max_length=80)
    publisher_subject: str = Field(default="local-demo-user", min_length=1, max_length=320)
    operation_id: str = Field(min_length=1, max_length=255)
    http_method: str = Field(pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    resource_path: str = Field(min_length=1, max_length=1000)
    upstream_base_url: HttpUrl
    upstream_path: str = Field(min_length=1, max_length=1000)
    allowed_fields: list[str] = Field(min_length=1)
    allowed_purposes: list[str] = Field(default_factory=lambda: ["business"], min_length=1)
    max_duration_days: int = Field(default=30, ge=1, le=365)
    enabled: bool = False


class ConsentCreate(BaseModel):
    api_id: str
    api_version: str
    operation_id: str
    application_id: str = Field(min_length=1, max_length=255)
    requested_fields: list[str] = Field(min_length=1)
    purpose: str
    duration_days: int = Field(ge=1, le=365)


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    approved_fields: list[str] = Field(default_factory=list)
    note: str = ""


app = FastAPI(
    title="Dynamic WSO2 Consent POC (Local Demo Mode)",
    version="0.3.0",
    description="Authentication intentionally disabled for local synthetic-data testing only.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(publisher_extension_router)
def audit(db: Session, actor: str, event: str, object_id: str | None = None, details: dict | None = None) -> None:
    db.add(AuditEvent(actor=actor, event=event, object_id=object_id, details_json=json.dumps(details or {})))
    db.commit()


def api_to_dict(op: ApiOperation) -> dict:
    return {
        "id": op.id,
        "api_id": op.api_id,
        "api_name": op.api_name,
        "api_version": op.api_version,
        "publisher_subject": op.publisher_subject,
        "operation_id": op.operation_id,
        "http_method": op.http_method,
        "resource_path": op.resource_path,
        "allowed_fields": json.loads(op.allowed_fields_json),
        "allowed_purposes": json.loads(op.allowed_purposes_json),
        "max_duration_days": op.max_duration_days,
        "enabled": op.enabled,
    }


def request_to_dict(row: ConsentRequest) -> dict:
    return {
        "id": row.id,
        "requester_subject": row.requester_subject,
        "application_id": row.application_id,
        "api_id": row.api_id,
        "api_version": row.api_version,
        "operation_id": row.operation_id,
        "requested_fields": json.loads(row.requested_fields_json),
        "purpose": row.purpose,
        "duration_days": row.duration_days,
        "status": row.status,
        "decision_by": row.decision_by,
        "decision_note": row.decision_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


def grant_to_dict(grant: ConsentGrant) -> dict:
    return {
        "id": grant.id,
        "request_id": grant.request_id,
        "requester_subject": grant.requester_subject,
        "application_id": grant.application_id,
        "api_id": grant.api_id,
        "api_version": grant.api_version,
        "operation_id": grant.operation_id,
        "approved_fields": json.loads(grant.approved_fields_json),
        "purpose": grant.purpose,
        "status": grant.status,
        "starts_at": grant.starts_at.isoformat() if grant.starts_at else None,
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
    }


def get_operation(db: Session, api_id: str, api_version: str, operation_id: str) -> ApiOperation:
    op = db.scalar(select(ApiOperation).where(
        ApiOperation.api_id == api_id,
        ApiOperation.api_version == api_version,
        ApiOperation.operation_id == operation_id,
        ApiOperation.enabled.is_(True),
    ))
    if not op:
        raise HTTPException(404, "API operation is not registered or enabled for consent")
    return op


logger = logging.getLogger("consent.wso2_sync")
SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete"}


def require_internal_key(x_consent_internal_key: str | None = Header(default=None, alias="X-Consent-Internal-Key")):
    expected = settings.consent_internal_key.strip()
    if not expected or expected == "CHANGE_ME":
        raise HTTPException(503, "Set CONSENT_INTERNAL_KEY in the service .env before using WSO2 sync")
    if not x_consent_internal_key or not hmac_compare(x_consent_internal_key, expected):
        raise HTTPException(401, "Invalid internal key")
    return True


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def parse_json_document(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def get_production_upstream(api_details: dict) -> str:
    endpoint_config = parse_json_document(api_details.get("endpointConfig"))
    endpoints = endpoint_config.get("production_endpoints") or endpoint_config.get("productionEndpoints") or {}
    if isinstance(endpoints, dict):
        url = endpoints.get("url") or endpoints.get("endpointUrl")
        if url:
            return str(url).rstrip("/")
    if isinstance(endpoints, list):
        for endpoint in endpoints:
            if isinstance(endpoint, dict) and (endpoint.get("url") or endpoint.get("endpointUrl")):
                return str(endpoint.get("url") or endpoint.get("endpointUrl")).rstrip("/")
    # Some APIM responses return endpointConfig as a list of endpoint entries.
    if isinstance(endpoint_config, list):
        for endpoint in endpoint_config:
            if isinstance(endpoint, dict) and str(endpoint.get("endpointType", "")).lower() == "production":
                url = endpoint.get("url") or endpoint.get("endpointUrl")
                if url:
                    return str(url).rstrip("/")
    return ""


def resolve_schema_refs(spec: dict, schema: Any, depth: int = 0) -> Any:
    if depth > 12 or not isinstance(schema, (dict, list)):
        return schema
    if isinstance(schema, list):
        return [resolve_schema_refs(spec, item, depth + 1) for item in schema]
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        node: Any = spec
        try:
            for part in ref[2:].split("/"):
                node = node[part.replace("~1", "/").replace("~0", "~")]
            resolved = resolve_schema_refs(spec, node, depth + 1)
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            if siblings and isinstance(resolved, dict):
                return {**resolved, **siblings}
            return resolved
        except (KeyError, TypeError):
            return schema
    return {k: resolve_schema_refs(spec, v, depth + 1) if isinstance(v, (dict, list)) else v for k, v in schema.items()}


def schema_field_names(spec: dict, schema: Any, depth: int = 0) -> set[str]:
    fields: set[str] = set()
    if depth > 12 or not isinstance(schema, dict):
        return fields
    schema = resolve_schema_refs(spec, schema)
    if not isinstance(schema, dict):
        return fields
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            fields.add(str(name))
            # Include nested keys as possible selectable field names too; filter_json
            # retains matching names at any JSON object depth.
            fields.update(schema_field_names(spec, child, depth + 1))
    for combiner in ("allOf", "oneOf", "anyOf"):
        for part in schema.get(combiner, []) if isinstance(schema.get(combiner, []), list) else []:
            fields.update(schema_field_names(spec, part, depth + 1))
    if isinstance(schema.get("items"), dict):
        fields.update(schema_field_names(spec, schema["items"], depth + 1))
    return fields


def extract_response_fields(spec: dict, resource_path: str, method: str) -> list[str]:
    path_item = (spec.get("paths") or {}).get(resource_path) or {}
    operation = path_item.get(method.lower()) or {}
    responses = operation.get("responses") or {}
    for status in ("200", "201", "202", "default"):
        response = responses.get(status)
        if not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        for mime, item in content.items():
            if "json" not in mime.lower() or not isinstance(item, dict):
                continue
            fields = schema_field_names(spec, item.get("schema", {}))
            if fields:
                return sorted(fields)
        # Swagger 2.0 response schema
        schema = response.get("schema")
        fields = schema_field_names(spec, schema)
        if fields:
            return sorted(fields)
    return []


def make_operation_id(operation: dict, method: str, path: str) -> str:
    operation_id = str(operation.get("operationId") or "").strip()
    if operation_id:
        return operation_id[:255]
    slug = "_".join(part for part in path.strip("/").replace("{", "").replace("}", "").replace("/", "_").split("_") if part)
    return f"{method}_{slug or 'root'}"[:255]


async def get_wso2_access_token(client: httpx.AsyncClient) -> str:
    missing = [name for name, value in {
        "WSO2_TOKEN_URL": settings.wso2_token_url,
        "WSO2_CLIENT_ID": settings.wso2_client_id,
        "WSO2_CLIENT_SECRET": settings.wso2_client_secret,
    }.items() if not value.strip()]
    if missing:
        raise HTTPException(503, "Missing WSO2 settings: " + ", ".join(missing))
    data = {"grant_type": "client_credentials"}
    if settings.wso2_client_scope.strip():
        data["scope"] = settings.wso2_client_scope.strip()
    try:
        response = await client.post(settings.wso2_token_url, data=data, auth=(settings.wso2_client_id, settings.wso2_client_secret))
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not connect to WSO2 token endpoint") from exc
    if response.status_code >= 400:
        logger.error("WSO2 token request failed: status=%s body=%s", response.status_code, response.text[:300])
        raise HTTPException(502, f"WSO2 token endpoint returned HTTP {response.status_code}; check client credentials and scopes")
    token = response.json().get("access_token")
    if not token:
        raise HTTPException(502, "WSO2 token response did not contain access_token")
    return str(token)


async def wso2_get(client: httpx.AsyncClient, token: str, url: str, params: dict | None = None) -> dict:
    response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if response.status_code >= 400:
        raise HTTPException(502, f"WSO2 Publisher API returned HTTP {response.status_code} for {url.rsplit('/', 1)[-1]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "WSO2 Publisher API returned invalid JSON") from exc
    return data if isinstance(data, dict) else {}


async def fetch_wso2_api_list(client: httpx.AsyncClient, token: str) -> list[dict]:
    base = settings.wso2_api_metadata_base.rstrip("/")
    result: list[dict] = []
    offset = 0
    while offset < 10000:
        payload = await wso2_get(client, token, f"{base}/apis", {"limit": 100, "offset": offset})
        batch = payload.get("list") or payload.get("apis") or payload.get("items") or []
        if not isinstance(batch, list) or not batch:
            break
        result.extend(item for item in batch if isinstance(item, dict))
        count = payload.get("count")
        offset += len(batch)
        if len(batch) < 100 or (isinstance(count, int) and offset >= count):
            break
    return result


async def fetch_wso2_api_details(client: httpx.AsyncClient, token: str, api_id: str) -> dict:
    base = settings.wso2_api_metadata_base.rstrip("/")
    return await wso2_get(client, token, f"{base}/apis/{quote(api_id, safe='')}")


async def fetch_wso2_openapi(client: httpx.AsyncClient, token: str, api_id: str) -> dict:
    base = settings.wso2_api_metadata_base.rstrip("/")
    response = await client.get(f"{base}/apis/{quote(api_id, safe='')}/swagger", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if response.status_code >= 400:
        raise HTTPException(502, f"Could not fetch API definition for API {api_id} (HTTP {response.status_code})")
    try:
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("swagger"), str):
            data = json.loads(data["swagger"])
        if isinstance(data, str):
            data = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, f"API {api_id} has an unreadable OpenAPI definition") from exc
    if not isinstance(data, dict):
        raise HTTPException(502, f"API {api_id} OpenAPI definition is not an object")
    return data


def _find_named_property(value: Any, target_names: set[str]) -> Any:
    """Find a WSO2 custom API property across known v4 response shapes."""
    if isinstance(value, dict):
        # WSO2 releases may represent properties as a map or as {name,value} items.
        for key, item in value.items():
            if str(key).lower() in target_names:
                if isinstance(item, dict) and "value" in item:
                    return item.get("value")
                return item
        if str(value.get("name", "")).lower() in target_names and "value" in value:
            return value.get("value")
        for item in value.values():
            found = _find_named_property(item, target_names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_named_property(item, target_names)
            if found is not None:
                return found
    return None


def parse_field_list(value: Any) -> list[str]:
    """Accept CSV, JSON-array strings, or list-shaped Publisher properties."""
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
            values = decoded if isinstance(decoded, list) else raw.split(",")
        except ValueError:
            values = raw.split(",")
    else:
        return []
    return sorted({str(v).strip() for v in values if str(v).strip()})


def get_publisher_shared_fields(details: dict, listed: dict) -> list[str]:
    """Extract Publisher's shared_data_keys consent allow-list.

    Supports maps, nested custom-property maps, and lists of {name, value}
    objects returned by different WSO2 Publisher API response shapes.
    """
    targets = {
        "shared_data_keys",
        "shared-data-keys",
        "shared_data_fields",
        "shared data keys",
        "shared data fields",
    }

    def walk(value: Any) -> list[str]:
        if isinstance(value, dict):
            # Shape: {"shared_data_keys": "field_a,field_b"}
            for key, item in value.items():
                if str(key).strip().lower() in targets:
                    raw = item
                    if isinstance(raw, dict):
                        raw = raw.get("value") or raw.get("propertyValue")
                    fields = parse_field_list(raw)
                    if fields:
                        return fields

            # Shape: {"name": "shared_data_keys", "value": "..."}
            prop_name = str(value.get("name") or value.get("key") or "").strip().lower()
            if prop_name in targets:
                raw = value.get("value") or value.get("propertyValue")
                fields = parse_field_list(raw)
                if fields:
                    return fields

            # Search nested properties/details structures.
            for item in value.values():
                fields = walk(item)
                if fields:
                    return fields

        elif isinstance(value, list):
            for item in value:
                fields = walk(item)
                if fields:
                    return fields

        return []

    # Prefer the detailed API response, then the list response.
    for root in (details, listed):
        fields = walk(root)
        if fields:
            # Preserve order while removing duplicate names.
            return list(dict.fromkeys(fields))

    return []


def sync_one_operation(
    db: Session,
    *,
    api_id: str,
    api_name: str,
    api_version: str,
    publisher_subject: str,
    operation_id: str,
    method: str,
    resource_path: str,
    upstream_base_url: str,
    fields: list[str],
) -> None:
    method = method.upper()

    # First, match the exact operation ID.
    existing = db.scalar(
        select(ApiOperation).where(
            ApiOperation.api_id == api_id,
            ApiOperation.api_version == api_version,
            ApiOperation.operation_id == operation_id,
        )
    )

    # If IDs differ, reuse the matching HTTP method + resource path.
    if existing is None:
        existing = db.scalar(
            select(ApiOperation).where(
                ApiOperation.api_id == api_id,
                ApiOperation.api_version == api_version,
                ApiOperation.http_method == method,
                ApiOperation.resource_path == resource_path,
            )
        )

    purposes = [
        p.strip()
        for p in settings.default_allowed_purposes.split(",")
        if p.strip()
    ] or ["business"]

    if existing:
        # Preserve the existing operation_id to avoid breaking references.
        existing.api_name = api_name
        existing.publisher_subject = publisher_subject
        existing.http_method = method
        existing.resource_path = resource_path
        existing.upstream_base_url = upstream_base_url.rstrip("/") + "/"
        existing.upstream_path = resource_path
        existing.allowed_fields_json = json.dumps(sorted(set(fields)))

        if not existing.allowed_purposes_json or existing.allowed_purposes_json == "[]":
            existing.allowed_purposes_json = json.dumps(purposes)

        existing.max_duration_days = max(
            1,
            min(
                existing.max_duration_days or settings.default_max_duration_days,
                365,
            ),
        )
        existing.enabled = True
        existing.updated_at = utcnow()
    else:
        db.add(
            ApiOperation(
                api_id=api_id,
                api_name=api_name,
                api_version=api_version,
                publisher_subject=publisher_subject,
                operation_id=operation_id,
                http_method=method,
                resource_path=resource_path,
                upstream_base_url=upstream_base_url.rstrip("/") + "/",
                upstream_path=resource_path,
                allowed_fields_json=json.dumps(sorted(set(fields))),
                allowed_purposes_json=json.dumps(purposes),
                max_duration_days=settings.default_max_duration_days,
                enabled=True,
                updated_at=utcnow(),
            )
        )

async def perform_wso2_sync(db: Session) -> dict:
    """Synchronize only the configured WSO2 API, not every API in the list."""
    if not settings.wso2_api_metadata_base.strip():
        raise HTTPException(503, "Set WSO2_API_METADATA_BASE in .env")

    target_id = settings.wso2_target_api_id.strip()
    target_name = settings.wso2_target_api_name.strip()
    if not target_id and not target_name:
        raise HTTPException(503, "Configure WSO2_TARGET_API_ID or WSO2_TARGET_API_NAME in .env")

    synced = 0
    skip_reasons: list[str] = []

    try:
        async with httpx.AsyncClient(
            verify=settings.wso2_tls_verify,
            timeout=30.0,
            follow_redirects=False,
        ) as client:
            token = await get_wso2_access_token(client)

            if target_id:
                candidates = [{"id": target_id}]
            else:
                api_list = await fetch_wso2_api_list(client, token)
                normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
                candidates = [
                    item for item in api_list
                    if normalize(item.get("name", "")) == normalize(target_name)
                ]
                if not candidates:
                    raise HTTPException(
                        404,
                        f"WSO2 API '{target_name}' was not found in the API list visible to this OAuth client.",
                    )

            for listed in candidates:
                api_id = str(listed.get("id") or listed.get("uuid") or "").strip()
                if not api_id:
                    skip_reasons.append("Candidate API has no UUID; skipped.")
                    continue

                details = await fetch_wso2_api_details(client, token, api_id)
                actual_id = str(details.get("id") or details.get("uuid") or api_id).strip()
                actual_name = str(details.get("name") or listed.get("name") or "").strip()

                if actual_id != api_id:
                    raise HTTPException(
                        502,
                        f"WSO2 returned unexpected UUID {actual_id}; requested {api_id}.",
                    )
                if target_id and actual_id != target_id:
                    raise HTTPException(
                        409,
                        f"Configured UUID {target_id} does not match WSO2 UUID {actual_id}.",
                    )

                normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
                if target_name and normalize(actual_name) != normalize(target_name):
                    raise HTTPException(
                        409,
                        f"Configured target name '{target_name}' does not match WSO2 API '{actual_name}'. Update WSO2_TARGET_API_NAME.",
                    )

                lifecycle = str(
                    details.get("lifeCycleStatus")
                    or details.get("lifecycleStatus")
                    or listed.get("lifeCycleStatus")
                    or listed.get("lifecycleStatus")
                    or ""
                ).strip().upper()
                if lifecycle != "PUBLISHED":
                    skip_reasons.append(
                        f"{actual_name}: lifecycle is '{lifecycle or 'missing'}', expected 'PUBLISHED'."
                    )
                    continue

                spec = await fetch_wso2_openapi(client, token, api_id)
                version = str(details.get("version") or listed.get("version") or "").strip()
                if not version:
                    raise HTTPException(502, f"WSO2 did not return a version for {api_id}.")

                provider = str(
                    details.get("provider") or listed.get("provider") or "wso2-publisher"
                ).strip()
                upstream = get_production_upstream(details)
                if not upstream:
                    raise HTTPException(
                        502,
                        f"{actual_name}: no production endpoint found in endpointConfig.",
                    )

                fields = get_publisher_shared_fields(details, listed)
                if not fields and settings.allow_openapi_field_fallback:
                    inferred: set[str] = set()
                    for path, path_item in (spec.get("paths") or {}).items():
                        if not isinstance(path_item, dict):
                            continue
                        for method, operation in path_item.items():
                            method = str(method).lower()
                            if method in SUPPORTED_METHODS and isinstance(operation, dict):
                                inferred.update(extract_response_fields(spec, path, method))
                    fields = sorted(inferred)

                if not fields:
                    raise HTTPException(
                        422,
                        f"{actual_name}: shared_data_keys was not found in Publisher metadata. Add and save that API property.",
                    )

                api_synced = 0
                for path, path_item in (spec.get("paths") or {}).items():
                    if not isinstance(path_item, dict):
                        continue
                    for method, operation in path_item.items():
                        method = str(method).lower()
                        if method not in SUPPORTED_METHODS or not isinstance(operation, dict):
                            continue
                        operation_id = make_operation_id(operation, method, path)
                        sync_one_operation(
                            db,
                            api_id=api_id,
                            api_name=actual_name,
                            api_version=version,
                            publisher_subject=provider,
                            operation_id=operation_id,
                            method=method,
                            resource_path=path,
                            upstream_base_url=upstream,
                            fields=fields,
                        )
                        synced += 1
                        api_synced += 1

                if api_synced == 0:
                    skip_reasons.append(
                        f"{actual_name}: no supported HTTP operations found in OpenAPI."
                    )

                logger.info(
                    "WSO2 sync completed: name=%s id=%s operations=%s fields=%s",
                    actual_name, api_id, api_synced, fields,
                )

            db.commit()
            return {
                "success": synced > 0,
                "target_api_id": target_id or None,
                "target_api_name": target_name or None,
                "operations_synced": synced,
                "items_skipped": len(skip_reasons),
                "skip_reasons": skip_reasons,
                "errors": [],
            }

    except HTTPException:
        db.rollback()
        raise
    except httpx.HTTPStatusError as exc:
        db.rollback()
        status = exc.response.status_code
        logger.exception("WSO2 metadata HTTP failure")
        if status == 403:
            detail = (
                "WSO2 returned HTTP 403 while reading target API metadata. "
                "Grant the consent service OAuth client permission to view "
                "the target API and its definition, then obtain a new token."
            )
        else:
            detail = f"WSO2 metadata request failed with HTTP {status}."
        raise HTTPException(502, detail) from exc
    except httpx.HTTPError as exc:
        db.rollback()
        logger.exception("WSO2 metadata connection failed")
        raise HTTPException(
            502, f"WSO2 metadata sync connection failed: {type(exc).__name__}."
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Unexpected WSO2 metadata sync failure")
        raise HTTPException(500, "Unexpected WSO2 metadata sync failure.") from exc


@app.get("/health")
def health():
    return {"status": "ok", "service": "dynamic-wso2-consent-poc", "mode": "local-demo-no-auth"}


@app.get("/api/registry")
def list_registry(_ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    rows = db.scalars(select(ApiOperation).where(ApiOperation.enabled.is_(True)).order_by(ApiOperation.api_name, ApiOperation.api_version)).all()
    return [api_to_dict(row) for row in rows]


@app.post("/api/registry/sync-wso2")
async def sync_wso2_registry(_authorized: bool = Depends(require_internal_key), db: Session = Depends(get_db)):
    """Import published API operations and response fields from WSO2 Publisher v4."""
    return await perform_wso2_sync(db)


@app.post("/api/registry/publisher-sync")
async def publisher_sync(
    _ident: dict = Depends(demo_identity),
    db: Session = Depends(get_db),
):
    """Publisher UI sync endpoint for the local demo.

    This deliberately reuses the configured WSO2 metadata sync routine. It does
    not accept or expose CONSENT_INTERNAL_KEY in browser code. Authentication is
    disabled in this local POC, so do not expose this endpoint to untrusted
    networks. For production, protect it with real Publisher authorization.
    """
    result = await perform_wso2_sync(db)
    rows = db.scalars(
        select(ApiOperation)
        .where(
            ApiOperation.api_id == settings.wso2_target_api_id,
            ApiOperation.enabled.is_(True),
        )
        .order_by(ApiOperation.api_version, ApiOperation.resource_path)
    ).all()
    return {
        **result,
        "registry_operations": [api_to_dict(row) for row in rows],
    }


@app.post("/api/registry/operations", status_code=201)
def register_operation(payload: OperationRegister, _ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    """Open registration endpoint for local POC only."""
    base_url = str(payload.upstream_base_url).rstrip("/")
    parsed = urlsplit(base_url)
    allowed_hosts = {h.strip().lower() for h in settings.allowed_upstream_hosts.split(",") if h.strip()}
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in allowed_hosts:
        raise HTTPException(422, "Upstream must use HTTPS or an explicitly allowed local development host")

    existing = db.scalar(select(ApiOperation).where(
        ApiOperation.api_id == payload.api_id,
        ApiOperation.api_version == payload.api_version,
        ApiOperation.operation_id == payload.operation_id,
    ))
    data = payload.model_dump()
    data["upstream_base_url"] = base_url + "/"
    data["allowed_fields_json"] = json.dumps(sorted(set(payload.allowed_fields)))
    data["allowed_purposes_json"] = json.dumps(sorted(set(payload.allowed_purposes)))
    data.pop("allowed_fields")
    data.pop("allowed_purposes")
    data.pop("enabled")
    if existing:
        for key, value in data.items():
            setattr(existing, key, value)
        existing.enabled = payload.enabled
        existing.updated_at = utcnow()
        op = existing
    else:
        op = ApiOperation(**data, enabled=payload.enabled)
        db.add(op)
    db.commit()
    db.refresh(op)
    return api_to_dict(op)


@app.post("/api/registry/operations/{op_id}/enable")
def enable_operation(op_id: int, _ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    op = db.get(ApiOperation, op_id)
    if not op:
        raise HTTPException(404, "Operation not found")
    op.enabled = True
    op.updated_at = utcnow()
    db.commit()
    db.refresh(op)
    return {"enabled": True, "operation": api_to_dict(op)}


@app.post("/api/consent/requests", status_code=201)
def create_consent_request(payload: ConsentCreate, ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    op = get_operation(db, payload.api_id, payload.api_version, payload.operation_id)
    requested = set(payload.requested_fields)
    allowed_fields = set(json.loads(op.allowed_fields_json))
    if not requested:
        raise HTTPException(422, "Select at least one field")
    if not requested.issubset(allowed_fields):
        raise HTTPException(422, "Requested fields exceed the registered API policy")
    if payload.purpose not in set(json.loads(op.allowed_purposes_json)):
        raise HTTPException(422, "Purpose is not permitted for this API operation")
    if payload.duration_days > op.max_duration_days:
        raise HTTPException(422, f"Maximum consent duration is {op.max_duration_days} days")

    # Local demo: application ID is taken from the request body because no
    # authenticated WSO2 application context is available in this mode.
    row = ConsentRequest(
        id=str(uuid.uuid4()),
        requester_subject=ident["user"],
        application_id=payload.application_id.strip(),
        api_id=op.api_id,
        api_version=op.api_version,
        operation_id=op.operation_id,
        requested_fields_json=json.dumps(sorted(requested)),
        purpose=payload.purpose,
        duration_days=payload.duration_days,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit(db, ident["user"], "consent.request.created", row.id, {"application_id": row.application_id, "api_id": op.api_id, "api_version": op.api_version, "operation_id": op.operation_id})
    return request_to_dict(row)


@app.get("/api/consent/requests/mine")
def my_requests(ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    rows = db.scalars(select(ConsentRequest).where(ConsentRequest.requester_subject == ident["user"]).order_by(ConsentRequest.created_at.desc())).all()
    return [request_to_dict(row) for row in rows]


@app.get("/api/consent/requests/pending")
def pending_requests(_ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    rows = db.scalars(select(ConsentRequest).where(ConsentRequest.status == "pending").order_by(ConsentRequest.created_at.asc())).all()
    return [request_to_dict(row) for row in rows]


@app.post("/api/consent/requests/{request_id}/decision")
def decide_request(request_id: str, payload: Decision, ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    row = db.get(ConsentRequest, request_id)
    if not row:
        raise HTTPException(404, "Consent request not found")
    if row.status != "pending":
        raise HTTPException(409, "This request has already been decided")
    op = get_operation(db, row.api_id, row.api_version, row.operation_id)
    now = utcnow()

    if payload.decision == "reject":
        row.status = "rejected"
        row.decision_by = ident["user"]
        row.decision_note = payload.note
        row.decided_at = now
        db.commit()
        db.refresh(row)
        audit(db, ident["user"], "consent.request.rejected", row.id)
        return request_to_dict(row)

    requested = set(json.loads(row.requested_fields_json))
    policy_fields = set(json.loads(op.allowed_fields_json))
    approved = set(payload.approved_fields)
    if not approved:
        raise HTTPException(422, "Select at least one field to approve")
    if not approved.issubset(requested):
        raise HTTPException(422, "Approved fields must be a subset of requested fields")
    if not approved.issubset(policy_fields):
        raise HTTPException(422, "Approved fields violate the registered API policy")

    grant = ConsentGrant(
        id=str(uuid.uuid4()),
        request_id=row.id,
        requester_subject=row.requester_subject,
        application_id=row.application_id,
        api_id=row.api_id,
        api_version=row.api_version,
        operation_id=row.operation_id,
        approved_fields_json=json.dumps(sorted(approved)),
        purpose=row.purpose,
        status="active",
        starts_at=now,
        expires_at=now + timedelta(days=row.duration_days),
    )
    db.add(grant)
    row.status = "approved" if approved == requested else "partially_approved"
    row.decision_by = ident["user"]
    row.decision_note = payload.note
    row.decided_at = now
    db.commit()
    db.refresh(row)
    db.refresh(grant)
    audit(db, ident["user"], "consent.grant.created", grant.id, {"request_id": row.id, "application_id": row.application_id, "approved_fields": sorted(approved)})
    return {"request": request_to_dict(row), "grant": grant_to_dict(grant)}


@app.get("/api/consent/grants/mine")
def my_grants(ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    rows = db.scalars(select(ConsentGrant).where(ConsentGrant.requester_subject == ident["user"]).order_by(ConsentGrant.expires_at.desc())).all()
    return [grant_to_dict(row) for row in rows]


@app.post("/api/consent/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, ident: dict = Depends(demo_identity), db: Session = Depends(get_db)):
    grant = db.get(ConsentGrant, grant_id)
    if not grant:
        raise HTTPException(404, "Grant not found")
    if grant.status != "revoked":
        grant.status = "revoked"
        grant.revoked_at = utcnow()
        db.commit()
        audit(db, ident["user"], "consent.grant.revoked", grant.id)
    return {"status": "revoked", "grant_id": grant.id}


def filter_json(value: Any, approved_fields: set[str]) -> Any:
    """Recursively retain approved JSON object keys."""
    if isinstance(value, dict):
        return {key: filter_json(item, approved_fields) for key, item in value.items() if key in approved_fields}
    if isinstance(value, list):
        return [filter_json(item, approved_fields) for item in value]
    return value


@app.api_route("/proxy/{api_id}/{api_version}/{operation_id}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_api(
    api_id: str,
    api_version: str,
    operation_id: str,
    request: Request,
    ident: dict = Depends(demo_identity),
    db: Session = Depends(get_db),
):
    method = request.method.upper()
    op = db.scalar(select(ApiOperation).where(
        ApiOperation.api_id == api_id,
        ApiOperation.api_version == api_version,
        ApiOperation.operation_id == operation_id,
        ApiOperation.http_method == method,
        ApiOperation.enabled.is_(True),
    ))
    if not op:
        raise HTTPException(404, "API operation is not registered or enabled")

    purpose = (request.headers.get("X-Consent-Purpose") or "").strip()
    if not purpose:
        raise HTTPException(400, "X-Consent-Purpose header is required")
    if purpose not in set(json.loads(op.allowed_purposes_json)):
        raise HTTPException(403, "Purpose is not permitted by API policy")

    now = utcnow()
    grants = db.scalars(select(ConsentGrant).where(
        ConsentGrant.requester_subject == ident["user"],
        ConsentGrant.application_id == ident["application_id"],
        ConsentGrant.api_id == api_id,
        ConsentGrant.api_version == api_version,
        ConsentGrant.operation_id == operation_id,
        ConsentGrant.purpose == purpose,
        ConsentGrant.status == "active",
        ConsentGrant.starts_at <= now,
        ConsentGrant.expires_at > now,
        ConsentGrant.revoked_at.is_(None),
    )).all()
    if not grants:
        raise HTTPException(403, "No active consent grant matches this application and operation")
    if len(grants) > 1:
        # Merge grants only when purpose/application/operation match; the union
        # reflects all active Publisher approvals for this exact app+operation.
        approved_fields = set().union(*(set(json.loads(g.approved_fields_json)) for g in grants))
    else:
        approved_fields = set(json.loads(grants[0].approved_fields_json))

    upstream_url = op.upstream_base_url.rstrip("/") + "/" + op.upstream_path.lstrip("/")
    body = await request.body()
    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    params = list(request.query_params.multi_items())

    try:
        async with httpx.AsyncClient(verify=settings.wso2_tls_verify, timeout=20.0, follow_redirects=False) as client:
            upstream_response = await client.request(method=method, url=upstream_url, params=params, content=body or None, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Configured upstream API is unavailable") from exc

    response_content_type = upstream_response.headers.get("content-type", "")
    if "application/json" not in response_content_type.lower():
        raise HTTPException(502, "Consent proxy supports JSON responses only")
    try:
        upstream_payload = upstream_response.json()
    except ValueError as exc:
        raise HTTPException(502, "Upstream returned invalid JSON") from exc

    filtered_payload = filter_json(upstream_payload, approved_fields)
    return JSONResponse(content=filtered_payload, status_code=upstream_response.status_code)


@app.get("/api/wso2/metadata-health")
def metadata_health():
    return {
        "configured": bool(settings.wso2_api_metadata_base and settings.wso2_token_url and settings.wso2_client_id and settings.wso2_client_secret),
        "sync_interval_seconds": max(60, settings.wso2_sync_interval_seconds),
        "field_source_priority": (
            ["Publisher API Properties: shared_data_keys", "OpenAPI response schema (opt-in)"]
            if settings.allow_openapi_field_fallback
            else ["Publisher API Properties: shared_data_keys only"]
        ),
        "target_api_id_configured": bool(settings.wso2_target_api_id.strip()),
        "target_api_name": settings.wso2_target_api_name,
        "allow_openapi_field_fallback": settings.allow_openapi_field_fallback,
        "note": "Published API metadata is polled automatically. The WSO2 OAuth client needs Publisher API-view and API-definition-view scopes.",
    }

@app.on_event("startup")
async def start_periodic_wso2_sync():
    async def loop():
        # Run once on startup, then poll for newly published/updated APIs.
        while True:
            if all([settings.wso2_api_metadata_base, settings.wso2_token_url, settings.wso2_client_id, settings.wso2_client_secret]):
                db = SessionLocal()
                try:
                    result = await perform_wso2_sync(db)
                    logger.info("Automatic WSO2 metadata sync: %s", result)
                except Exception:
                    logger.exception("Automatic WSO2 metadata sync failed")
                finally:
                    db.close()
            await asyncio.sleep(max(60, settings.wso2_sync_interval_seconds))
    asyncio.create_task(loop())


# ---------------------------------------------------------------------------
# Gateway response-enforcement contract
# ---------------------------------------------------------------------------
# This endpoint is intended ONLY for a trusted WSO2 Classic Gateway mediator.
# The mediator must send identity values taken from trusted Gateway context,
# never values copied from client-supplied headers or query parameters.
#
# It does not, by itself, modify WSO2 Gateway routing. Until a mediator calls
# this endpoint on the protected API's response path, the original API URL
# will not be consent-filtered by this service.

class GatewayEnforcementInput(BaseModel):
    api_id: str = Field(min_length=1, max_length=255)
    api_version: str = Field(min_length=1, max_length=80)
    operation_id: str = Field(min_length=1, max_length=255)
    application_id: str = Field(min_length=1, max_length=255)
    requester_subject: str = Field(min_length=1, max_length=320)
    purpose: str = Field(min_length=1, max_length=255)
    response_status: int = Field(ge=100, le=599)
    content_type: str = Field(default="application/json", max_length=255)
    response_body: Any


def filter_top_level_json(value: Any, approved_fields: set[str]) -> Any:
    """Keep only explicitly approved top-level keys; preserve nested values."""
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key in approved_fields}
    if isinstance(value, list):
        filtered = []
        for item in value:
            if isinstance(item, dict):
                filtered.append({key: item_value for key, item_value in item.items()
                                 if key in approved_fields})
            else:
                # Do not expose non-object list entries under a field-level policy.
                continue
        return filtered
    raise HTTPException(502, "Consent filtering supports JSON objects or arrays of objects only")


@app.post("/internal/gateway/enforce-response")
def gateway_enforce_response(
    payload: GatewayEnforcementInput,
    _authorized: bool = Depends(require_internal_key),
    db: Session = Depends(get_db),
):
    """Validate the grant and filter a response for a trusted Gateway mediator."""
    if payload.api_id != settings.wso2_target_api_id:
        raise HTTPException(403, "Consent enforcement is not enabled for this API")

    op = db.scalar(select(ApiOperation).where(
        ApiOperation.api_id == payload.api_id,
        ApiOperation.api_version == payload.api_version,
        ApiOperation.operation_id == payload.operation_id,
        ApiOperation.enabled.is_(True),
    ))
    if not op:
        raise HTTPException(403, "API operation is not registered/enabled for consent")

    if "application/json" not in payload.content_type.lower():
        raise HTTPException(502, "Consent enforcement supports application/json responses only")

    allowed_purposes = set(json.loads(op.allowed_purposes_json))
    if payload.purpose not in allowed_purposes:
        raise HTTPException(403, "Purpose is not permitted for this API operation")

    now = utcnow()
    grants = db.scalars(select(ConsentGrant).where(
        ConsentGrant.requester_subject == payload.requester_subject,
        ConsentGrant.application_id == payload.application_id,
        ConsentGrant.api_id == payload.api_id,
        ConsentGrant.api_version == payload.api_version,
        ConsentGrant.operation_id == payload.operation_id,
        ConsentGrant.purpose == payload.purpose,
        ConsentGrant.status == "active",
        ConsentGrant.starts_at <= now,
        ConsentGrant.expires_at > now,
        ConsentGrant.revoked_at.is_(None),
    )).all()

    if not grants:
        raise HTTPException(403, "No active consent grant matches this application and operation")

    # Combine grants only for the same trusted subject/app/API/version/operation/purpose.
    approved_fields = set().union(*(
        set(json.loads(grant.approved_fields_json)) for grant in grants
    ))
    registered_fields = set(json.loads(op.allowed_fields_json))
    approved_fields &= registered_fields
    if not approved_fields:
        raise HTTPException(403, "The active grant contains no currently allowed fields")

    filtered = filter_top_level_json(payload.response_body, approved_fields)
    return {
        "allowed": True,
        "api_id": payload.api_id,
        "api_version": payload.api_version,
        "operation_id": payload.operation_id,
        "application_id": payload.application_id,
        "response_status": payload.response_status,
        "approved_fields": sorted(approved_fields),
        "response_body": filtered,
    }
