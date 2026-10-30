
from __future__ import annotations

import json, os, uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from fastapi import Query
from sqlalchemy import func, select




# ============================================================
# SETTINGS
# Reads the same DATABASE_URL as your existing consent service.
# Uses separate tables and a separate SQLAlchemy Base.
# ============================================================

class ExtensionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str

    wso2_token_url: str = "https://localhost:9443/oauth2/token"
    wso2_api_metadata_base: str = (
        "https://localhost:9443/api/am/publisher/v4"
    )
    wso2_client_id: str = ""
    wso2_client_secret: str = ""
    wso2_client_scope: str = (
        "apim:api_view apim:api_definition_view"
    )
    wso2_tls_verify: bool = False


extension_settings = ExtensionSettings()

DATABASE_URL = extension_settings.database_url

connect_args = (
    {"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

router = APIRouter(
    prefix="/api/publisher-extension",
    tags=["Publisher API Request Extension"],
)


# ============================================================
# DATABASE MODELS
# These are additional tables. Existing consent models are not
# imported, renamed, or modified by this extension.
# ============================================================

def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PublisherApi(Base):
    __tablename__ = "publisher_api_catalog"

    api_id: Mapped[str] = mapped_column(
        String(255), primary_key=True
    )
    name: Mapped[str] = mapped_column(
        String(255), index=True
    )
    version: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(
        String(255), default=""
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(50), default="PUBLISHED"
    )
    context: Mapped[str] = mapped_column(
        String(1000), default=""
    )
    description: Mapped[str] = mapped_column(
        Text, default=""
    )
    endpoint_config_json: Mapped[str] = mapped_column(
        Text, default="{}"
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class PublisherOperation(Base):
    __tablename__ = "publisher_api_operations"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    api_id: Mapped[str] = mapped_column(
        String(255), index=True
    )
    api_version: Mapped[str] = mapped_column(
        String(80)
    )
    operation_id: Mapped[str] = mapped_column(
        String(255)
    )
    http_method: Mapped[str] = mapped_column(
        String(10)
    )
    resource_path: Mapped[str] = mapped_column(
        String(1000)
    )
    fields_json: Mapped[str] = mapped_column(
        Text, default="[]"
    )


class PublisherAccessRequest(Base):
    __tablename__ = "publisher_access_requests"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True
    )
    requester: Mapped[str] = mapped_column(
        String(320), index=True
    )
    application_name: Mapped[str] = mapped_column(
        String(255)
    )
    api_id: Mapped[str] = mapped_column(
        String(255), index=True
    )
    api_name: Mapped[str] = mapped_column(
        String(255)
    )
    api_version: Mapped[str] = mapped_column(
        String(80)
    )
    operation_id: Mapped[str] = mapped_column(
        String(255)
    )
    http_method: Mapped[str] = mapped_column(
        String(10)
    )
    resource_path: Mapped[str] = mapped_column(
        String(1000)
    )
    requested_fields_json: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    duration_days: Mapped[int] = mapped_column(
        Integer, default=30
    )
    status: Mapped[str] = mapped_column(
        String(30), default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class PublisherDecision(Base):
    __tablename__ = "publisher_request_decisions"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    request_id: Mapped[str] = mapped_column(
        String(36), index=True
    )
    decided_by: Mapped[str] = mapped_column(
        String(320)
    )
    decision: Mapped[str] = mapped_column(
        String(20)
    )
    approved_fields_json: Mapped[str] = mapped_column(
        Text, default="[]"
    )
    note: Mapped[str] = mapped_column(
        Text, default=""
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


class PublisherAudit(Base):
    __tablename__ = "publisher_request_audit"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    actor: Mapped[str] = mapped_column(String(320))
    event: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[str] = mapped_column(
        String(36), index=True
    )
    details_json: Mapped[str] = mapped_column(
        Text, default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )


# Creates only tables declared on this extension's Base.
# It does not drop or alter the existing consent tables.
Base.metadata.create_all(bind=engine)


# ============================================================
# DATABASE HELPERS
# ============================================================

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def audit(
    db: Session,
    actor: str,
    event: str,
    object_id: str,
    details: dict | None = None,
) -> None:
    db.add(
        PublisherAudit(
            actor=actor,
            event=event,
            object_id=object_id,
            details_json=json.dumps(details or {}),
        )
    )
    db.commit()


# ============================================================
# IDENTITY
# DEVELOPMENT PLACEHOLDER ONLY.
# Replace with your verified WSO2/Keycloak identity dependency
# before exposing these routes in a real environment.
# ============================================================

def who(
    x_publisher_user: str | None = Header(
        default=None, alias="X-Publisher-User"
    ),
) -> str:
    return (x_publisher_user or "local-demo-user").strip()


# ============================================================
# REQUEST SCHEMAS
# ============================================================

class RequestIn(BaseModel):
    requester: str = Field(
        default="local-demo-user",
        min_length=1,
        max_length=320,
    )
    application_name: str = Field(
        min_length=1, max_length=255
    )
    api_id: str
    api_version: str
    operation_id: str
    requested_fields: list[str] = Field(default_factory=list)
    purpose: str = Field(min_length=3, max_length=4000)
    duration_days: int = Field(default=30, ge=1, le=365)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    approved_fields: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=4000)


def row_dict(r: PublisherAccessRequest) -> dict:
    return {
        "id": r.id,
        "requester": r.requester,
        "application_name": r.application_name,
        "api_id": r.api_id,
        "api_name": r.api_name,
        "api_version": r.api_version,
        "operation_id": r.operation_id,
        "http_method": r.http_method,
        "resource_path": r.resource_path,
        "requested_fields": json.loads(
            r.requested_fields_json
        ),
        "purpose": r.purpose,
        "duration_days": r.duration_days,
        "status": r.status,
        "created_at": (
            r.created_at.isoformat() if r.created_at else None
        ),
    }


# ============================================================
# OPENAPI FIELD DISCOVERY
# Supports direct properties and simple component $ref schemas.
# Complex nested schemas may need additional resolution logic.
# ============================================================

def openapi_fields(
    spec: dict,
    method: str,
    path: str,
) -> list[str]:
    result: set[str] = set()

    path_item = (spec.get("paths") or {}).get(path) or {}
    operation = path_item.get(method.lower()) or {}
    responses = operation.get("responses") or {}

    for response in responses.values():
        content = (response or {}).get("content") or {}
        media = content.get("application/json") or {}
        schema = media.get("schema") or {}

        for key in schema.get("properties", {}):
            result.add(key)

        ref = schema.get("$ref", "")
        prefix = "#/components/schemas/"

        if ref.startswith(prefix):
            name = ref.rsplit("/", 1)[-1]
            component = (
                spec.get("components", {})
                .get("schemas", {})
                .get(name, {})
            )
            for key in component.get("properties", {}):
                result.add(key)

    return sorted(result)


# ============================================================
# WSO2 METADATA HELPERS
# ============================================================

async def wso2_token(client: httpx.AsyncClient) -> str:
    token_url = extension_settings.wso2_token_url
    client_id = extension_settings.wso2_client_id
    client_secret = extension_settings.wso2_client_secret
    scope = extension_settings.wso2_client_scope

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure WSO2_CLIENT_ID and "
                "WSO2_CLIENT_SECRET in .env"
            ),
        )

    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope

    try:
        response = await client.post(
            token_url,
            data=data,
            auth=(client_id, client_secret),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Cannot connect to WSO2 token endpoint",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                f"WSO2 token request failed "
                f"({response.status_code}); check credentials/scopes"
            ),
        )

    try:
        token = response.json().get("access_token")
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="WSO2 returned an invalid token response",
        ) from exc

    if not token:
        raise HTTPException(
            status_code=502,
            detail="WSO2 token response is missing access_token",
        )

    return token


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    params: dict | None = None,
) -> dict:
    try:
        response = await client.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Cannot connect to WSO2 API metadata endpoint",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                "WSO2 API metadata request failed "
                f"({response.status_code})"
            ),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="WSO2 metadata endpoint returned invalid JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="Unexpected WSO2 metadata response",
        )

    return payload


# ============================================================
# HEALTH
# ============================================================

@router.get("/health")
def extension_health():
    return {
        "module": "publisher-extension",
        "status": "ok",
        "tables": [
            "publisher_api_catalog",
            "publisher_api_operations",
            "publisher_access_requests",
            "publisher_request_decisions",
            "publisher_request_audit",
        ],
    }


# ============================================================
# SYNCHRONIZE PUBLISHED APIS FROM WSO2
# ============================================================








@router.post("/sync-wso2")
async def sync_wso2(
    db: Session = Depends(db_session),
    actor: str = Depends(who),
):
    base = extension_settings.wso2_api_metadata_base.rstrip("/")
    verify = extension_settings.wso2_tls_verify

    # WSO2 returned 4 APIs with limit=10, so use 10 here.
    page_size = 10
    max_pages = 100
    offset = 0

    seen_ids = set()
    synced_ids = set()
    skipped = []
    pages = []
    reported_total = None

    try:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=60,
        ) as client:
            token = await wso2_token(client)

            for _ in range(max_pages):
                payload = await get_json(
                    client,
                    f"{base}/apis",
                    token,
                    {
                        "limit": page_size,
                        "offset": offset,
                    },
                )

                items = payload.get("list")
                if items is None:
                    items = payload.get("apis", [])

                pagination = payload.get("pagination") or {}
                page_total = (
                    pagination.get("total")
                    if isinstance(pagination, dict)
                    else None
                )

                if page_total is not None:
                    reported_total = int(page_total)

                if not isinstance(items, list):
                    raise HTTPException(
                        status_code=502,
                        detail="Unexpected WSO2 API listing response",
                    )

                pages.append({
                    "offset": offset,
                    "limit": page_size,
                    "returned": len(items),
                    "reported_total": reported_total,
                    "names": [
                        item.get("name")
                        for item in items
                        if isinstance(item, dict)
                    ],
                })

                if not items:
                    break

                new_ids = set()

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    api_id = str(
                        item.get("id") or item.get("uuid") or ""
                    ).strip()

                    if not api_id or api_id in seen_ids:
                        continue

                    seen_ids.add(api_id)
                    new_ids.add(api_id)

                    # Use list metadata as a fallback if details fail.
                    details = dict(item)

                    try:
                        detail_payload = await get_json(
                            client,
                            f"{base}/apis/{quote(api_id, safe='')}",
                            token,
                        )
                        details.update(detail_payload)
                    except HTTPException:
                        skipped.append({
                            "api_id": api_id,
                            "name": item.get("name"),
                            "warning": "Details endpoint unavailable; using list metadata",
                        })

                    name = str(
                        details.get("name")
                        or item.get("name")
                        or api_id
                    )
                    version = str(
                        details.get("version")
                        or item.get("version")
                        or ""
                    )
                    lifecycle = str(
                        details.get("lifeCycleStatus")
                        or details.get("lifecycleStatus")
                        or details.get("status")
                        or ""
                    ).strip().upper()

                    if not version:
                        skipped.append({
                            "api_id": api_id,
                            "name": name,
                            "reason": "API version is missing",
                        })
                        continue

                    # Save only verified published APIs.
                    if lifecycle != "PUBLISHED":
                        skipped.append({
                            "api_id": api_id,
                            "name": name,
                            "reason": (
                                f"Lifecycle status is "
                                f"{lifecycle or 'unknown'}"
                            ),
                        })
                        continue

                    try:
                        row = db.get(PublisherApi, api_id)

                        if row is None:
                            row = PublisherApi(
                                api_id=api_id,
                                name=name,
                                version=version,
                            )
                            db.add(row)

                        row.name = name
                        row.version = version
                        row.provider = str(
                            details.get("provider")
                            or item.get("provider")
                            or ""
                        )
                        row.lifecycle_status = lifecycle
                        row.context = str(details.get("context") or "")
                        row.description = str(
                            details.get("description") or ""
                        )
                        row.endpoint_config_json = json.dumps(
                            details.get("endpointConfig") or {}
                        )
                        row.synced_at = now()

                        # Retrieve the API OpenAPI definition.
                        # A failure here must not prevent saving the API.
                        spec = {}

                        try:
                            specdata = await get_json(
                                client,
                                (
                                    f"{base}/apis/"
                                    f"{quote(api_id, safe='')}/swagger"
                                ),
                                token,
                            )

                            raw_spec = specdata.get("swagger")

                            if isinstance(raw_spec, str):
                                spec = json.loads(raw_spec)
                            elif isinstance(raw_spec, dict):
                                spec = raw_spec
                            else:
                                spec = specdata

                        except (
                            HTTPException,
                            ValueError,
                            TypeError,
                        ):
                            spec = {}

                        paths = (
                            spec.get("paths")
                            if isinstance(spec, dict)
                            else None
                        )

                        # Replace operations only if a valid paths map
                        # was retrieved. Otherwise preserve old operations.
                        if isinstance(paths, dict):
                            db.query(PublisherOperation).filter_by(
                                api_id=api_id,
                                api_version=version,
                            ).delete(synchronize_session=False)

                            for path, path_item in paths.items():
                                if not isinstance(path_item, dict):
                                    continue

                                for method, operation in path_item.items():
                                    method = str(method).upper()

                                    if method not in {
                                        "GET", "POST", "PUT", "PATCH",
                                        "DELETE", "OPTIONS", "HEAD",
                                    }:
                                        continue

                                    if not isinstance(operation, dict):
                                        continue

                                    operation_id = str(
                                        operation.get("operationId")
                                        or (
                                            f"{method}_"
                                            f"{path.strip('/').replace('/', '_')}"
                                        )
                                    )[:255]

                                    fields = openapi_fields(
                                        spec,
                                        method,
                                        path,
                                    )

                                    db.add(
                                        PublisherOperation(
                                            api_id=api_id,
                                            api_version=version,
                                            operation_id=operation_id,
                                            http_method=method,
                                            resource_path=path,
                                            fields_json=json.dumps(fields),
                                        )
                                    )

                        db.commit()
                        synced_ids.add(api_id)

                    except Exception as exc:
                        db.rollback()
                        skipped.append({
                            "api_id": api_id,
                            "name": name,
                            "reason": type(exc).__name__,
                        })

                # Advance by the actual number of records returned.
                offset += len(items)

                # If WSO2 reports a total, trust that total rather than
                # stopping just because the page is shorter than 10.
                if (
                    reported_total is not None
                    and offset >= reported_total
                ):
                    break

                # Only use a short page as an end marker when no total
                # was supplied by WSO2.
                if reported_total is None and len(items) < page_size:
                    break

                # Prevent infinite loops if WSO2 repeats the same page.
                if not new_ids:
                    break

            else:
                raise HTTPException(
                    status_code=502,
                    detail="WSO2 pagination exceeded the safety limit",
                )

        audit(
            db,
            actor,
            "wso2_catalog_sync",
            str(uuid.uuid4()),
            {
                "synced_apis": len(synced_ids),
                "unique_api_ids_seen": len(seen_ids),
                "reported_total": reported_total,
                "pages": pages,
                "skipped": skipped,
            },
        )

        return {
            "message": "WSO2 API synchronization completed",
            "synced_apis": len(synced_ids),
            "unique_api_ids_seen": len(seen_ids),
            "wso2_reported_total": reported_total,
            "pages": pages,
            "skipped_count": len(skipped),
            "skipped_apis": skipped,
        }

    except HTTPException:
        db.rollback()
        raise

    except httpx.HTTPError as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail="Could not connect to WSO2 metadata endpoint",
        ) from exc

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"WSO2 synchronization failed: {type(exc).__name__}",
        ) from exc



@router.get("/apis")
def list_apis(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(db_session),
):
    total = db.scalar(
        select(func.count()).select_from(PublisherApi)
    ) or 0

    rows = db.scalars(
        select(PublisherApi)
        .order_by(PublisherApi.name, PublisherApi.api_id)
        .limit(limit)
        .offset(offset)
    ).all()

    items = []

    for row in rows:
        operations = db.scalars(
            select(PublisherOperation)
            .where(
                PublisherOperation.api_id == row.api_id,
                PublisherOperation.api_version == row.version,
            )
            .order_by(
                PublisherOperation.resource_path,
                PublisherOperation.http_method,
            )
        ).all()

        items.append({
            "api_id": row.api_id,
            "name": row.name,
            "version": row.version,
            "provider": row.provider,
            "context": row.context,
            "description": row.description,
            "lifecycle_status": row.lifecycle_status,
            "synced_at": (
                row.synced_at.isoformat()
                if row.synced_at else None
            ),
            "operations": [
                {
                    "operation_id": op.operation_id,
                    "method": op.http_method,
                    "path": op.resource_path,
                    "fields": json.loads(op.fields_json or "[]"),
                }
                for op in operations
            ],
        })

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned_count": len(items),
        "has_more": offset + len(items) < total,
        "api_names": [item["name"] for item in items],
        "items": items,
    }




@router.get('/debug-wso2-pagination')
async def debug_wso2_pagination():
    base = os.getenv(
        'WSO2_API_METADATA_BASE',
        'https://localhost:9443/api/am/publisher/v4',
    ).rstrip('/')

    verify = os.getenv('WSO2_TLS_VERIFY', 'false').lower() in (
        '1', 'true', 'yes',
    )

    async with httpx.AsyncClient(
        verify=verify,
        timeout=30,
    ) as client:
        token = await wso2_token(client)

        payload = await get_json(
            client,
            f'{base}/apis',
            token,
            {'limit': 25, 'offset': 0},
        )

    pagination = payload.get('pagination') or {}
    items = payload.get('list') or payload.get('apis') or []

    return {
        'pagination': pagination,
        'reported_total': (
            pagination.get('total')
            if isinstance(pagination, dict)
            else payload.get('count')
        ),
        'returned_count': len(items),
        'api_names': [
            item.get('name')
            for item in items
            if isinstance(item, dict)
        ],
    }


# ============================================================
# CREATE AN ACCESS REQUEST
# ============================================================


@router.post("/requests", status_code=201)
def create_request(
    data: RequestIn,
    db: Session = Depends(db_session),
    actor: str = Depends(who),
):
    # Verify that the requested API is published and synchronized.
    api = db.get(PublisherApi, data.api_id)

    if (
        not api
        or api.version != data.api_version
        or api.lifecycle_status != "PUBLISHED"
    ):
        raise HTTPException(
            status_code=404,
            detail="Published API/version not found in synchronized catalogue",
        )

    # Verify that the requested endpoint exists.
    operation = db.scalar(
        select(PublisherOperation).where(
            PublisherOperation.api_id == data.api_id,
            PublisherOperation.api_version == data.api_version,
            PublisherOperation.operation_id == data.operation_id,
        )
    )

    if not operation:
        raise HTTPException(
            status_code=404,
            detail="API operation not found",
        )

    # Response fields are optional.
    # An empty list is accepted without field validation.
    requested = sorted({
        field.strip()
        for field in (data.requested_fields or [])
        if isinstance(field, str) and field.strip()
    })

    # Validate fields only when the requester selected some.
    if requested:
        allowed = set(
            json.loads(operation.fields_json or "[]")
        )

        if not set(requested).issubset(allowed):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Requested fields include fields not present "
                    "in the operation schema"
                ),
            )

    # Create a pending request regardless of whether fields were selected.
    request_row = PublisherAccessRequest(
        id=str(uuid.uuid4()),
        requester=data.requester.strip(),
        application_name=data.application_name.strip(),
        api_id=api.api_id,
        api_name=api.name,
        api_version=api.version,
        operation_id=operation.operation_id,
        http_method=operation.http_method,
        resource_path=operation.resource_path,
        requested_fields_json=json.dumps(requested),
        purpose=data.purpose.strip(),
        duration_days=data.duration_days,
        status="pending",
    )

    try:
        db.add(request_row)
        db.commit()
        db.refresh(request_row)

        audit(
            db,
            actor,
            "request_created",
            request_row.id,
            {
                "api_id": request_row.api_id,
                "operation_id": request_row.operation_id,
                "fields": requested,
            },
        )

        return row_dict(request_row)

    except Exception:
        db.rollback()
        raise

# ============================================================
# LIST ACCESS REQUESTS
# ============================================================

@router.get("/requests")
def list_requests(
    requester: str | None = None,
    status: str | None = None,
    db: Session = Depends(db_session),
):
    query = select(PublisherAccessRequest).order_by(
        PublisherAccessRequest.created_at.desc()
    )

    if requester:
        query = query.where(
            PublisherAccessRequest.requester == requester
        )

    if status:
        query = query.where(
            PublisherAccessRequest.status == status
        )

    rows = db.scalars(query).all()
    return {"items": [row_dict(row) for row in rows]}


# ============================================================
# PUBLISHER APPROVAL OR REJECTION
# ============================================================

@router.post("/requests/{request_id}/decision")
def decide_request(
    request_id: str,
    data: DecisionIn,
    db: Session = Depends(db_session),
    actor: str = Depends(who),
):
    request_row = db.get(PublisherAccessRequest, request_id)

    if not request_row:
        raise HTTPException(
            status_code=404,
            detail="Request not found",
        )

    if request_row.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Request has already been decided",
        )

    requested = set(
        json.loads(request_row.requested_fields_json)
    )
    approved = sorted(set(
        field.strip()
        for field in data.approved_fields
        if field.strip()
    ))

    if data.decision == "approve":
        if not approved:
            raise HTTPException(
                status_code=422,
                detail="Select at least one field to approve",
            )

        if not set(approved).issubset(requested):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Approved fields must be a subset "
                    "of the requested fields"
                ),
            )

    request_row.status = (
        "approved" if data.decision == "approve" else "rejected"
    )

    db.add(
        PublisherDecision(
            request_id=request_row.id,
            decided_by=actor,
            decision=data.decision,
            approved_fields_json=json.dumps(
                approved if data.decision == "approve" else []
            ),
            note=data.note,
        )
    )

    db.commit()

    audit(
        db,
        actor,
        "request_" + data.decision,
        request_row.id,
        {
            "approved_fields": approved,
            "note": data.note,
        },
    )

    return row_dict(request_row)


# ============================================================
# AUDIT EVENTS
# ============================================================

@router.get("/audit")
def audit_events(
    limit: int = 100,
    db: Session = Depends(db_session),
):
    limit = max(1, min(limit, 500))

    rows = db.scalars(
        select(PublisherAudit)
        .order_by(PublisherAudit.created_at.desc())
        .limit(limit)
    ).all()

    return {
        "items": [
            {
                "id": row.id,
                "actor": row.actor,
                "event": row.event,
                "object_id": row.object_id,
                "details": json.loads(row.details_json),
                "created_at": (
                    row.created_at.isoformat()
                    if row.created_at else None
                ),
            }
            for row in rows
        ]
    }
