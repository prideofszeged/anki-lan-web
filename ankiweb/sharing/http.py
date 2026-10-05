from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ankiweb.adapters.anki.collaboration import (
    DestructiveTemplateChangeError, EditConflictError, MirrorPreviewRequired,
    UnresolvedUpdateError,
)
from ankiweb.config import host_allowed
from ankiweb.identity.http import IdentityHttp, IdentityPrincipal
from ankiweb.identity.repository import (
    AuthorizationError, ConflictError, ExpiredTokenError, InvalidTokenError,
    NotFoundError,
)
from ankiweb.security import origin_ok

from .events import ShareSocketRegistry
from .models import DeckShare, ShareDetail, ShareMembership, ShareRole, ShareState
from .repository import ShareNotFoundError
from .service import SharingService


class WorkspaceProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck_id: int = Field(gt=0)


class ReleaseInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    mode: Literal["copy", "follow"]


class JobResponse(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    capability: str
    state: str
    progress: dict[str, Any]
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, job) -> "JobResponse":
        return cls(
            id=job.id, resource_type=job.resource_type, resource_id=job.resource_id,
            capability=job.capability, state=job.state.value,
            progress=dict(job.progress), error_code=job.error_code,
            created_at=job.created_at, started_at=job.started_at,
            finished_at=job.finished_at,
        )


class ShareCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class ShareResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    state: ShareState
    current_release: int | None
    created_at: datetime

    @classmethod
    def of(cls, share: DeckShare) -> "ShareResponse":
        return cls(**share.__dict__) if hasattr(share, "__dict__") else cls(
            id=share.id, owner_user_id=share.owner_user_id, name=share.name,
            state=share.state, current_release=share.current_release,
            created_at=share.created_at,
        )


class MembershipResponse(BaseModel):
    share_id: str
    user_id: str
    role: ShareRole
    joined_at: datetime

    @classmethod
    def of(cls, membership: ShareMembership) -> "MembershipResponse":
        return cls(
            share_id=membership.share_id, user_id=membership.user_id,
            role=membership.role, joined_at=membership.joined_at,
        )


class ShareListResponse(BaseModel):
    shares: list[ShareResponse]


class ShareDetailResponse(BaseModel):
    share: ShareResponse
    membership: MembershipResponse
    members: list[MembershipResponse]

    @classmethod
    def of(cls, detail: ShareDetail) -> "ShareDetailResponse":
        return cls(
            share=ShareResponse.of(detail.share),
            membership=MembershipResponse.of(detail.membership),
            members=[MembershipResponse.of(item) for item in detail.members],
        )


class ShareInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ShareRole
    intended_user_id: str | None = None
    expires_in_seconds: int = Field(default=7 * 86400, ge=60, le=30 * 86400)


class ShareInviteResponse(BaseModel):
    id: str
    role: ShareRole
    expires_at: datetime
    token: str
    accept_url: str


class ShareInviteAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=32, max_length=512)


class WorkspaceNotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    fields: dict[str, str] = Field(min_length=1)


class WorkspaceCommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: Literal["note", "template", "deck"]
    entity_id: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=10_000)


class SubscriptionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_version: int = Field(ge=1)
    mirror_preview_digest: str | None = Field(default=None, min_length=64, max_length=64)
    approve_templates: bool = False


class ConflictResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["mine", "upstream", "manual"]
    manual_value: str | None = Field(default=None, max_length=1_000_000)


class UpdateContinueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manual_values: dict[str, str] = Field(default_factory=dict)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ShareNotFoundError):
        return HTTPException(404, {"code": "share_not_found", "message": "share not found"})
    if isinstance(exc, ExpiredTokenError):
        return HTTPException(410, {"code": "invite_expired", "message": str(exc)})
    if isinstance(exc, InvalidTokenError):
        return HTTPException(400, {"code": "invalid_invite", "message": str(exc)})
    if isinstance(exc, NotFoundError):
        return HTTPException(404, {"code": "not_found", "message": str(exc)})
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, {"code": "forbidden", "message": str(exc)})
    if isinstance(exc, ConflictError):
        return HTTPException(409, {"code": "share_conflict", "message": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(422, {"code": "invalid_input", "message": str(exc)})
    raise exc


async def _call(function, /, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except (AuthorizationError, ConflictError, ExpiredTokenError, InvalidTokenError,
            NotFoundError, ValueError) as exc:
        raise _http_error(exc) from exc


def build_sharing_router(
    service: SharingService, identity_http: IdentityHttp, *, prefix: str = "/api/v1",
    workspace: Any | None = None, updater: Any | None = None,
    job_runner: Any | None = None,
    connections: ShareSocketRegistry | None = None, allowed_hosts: tuple[str, ...] = (),
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["sharing"])
    sockets = connections or ShareSocketRegistry()

    if job_runner is not None:
        @router.post(
            "/shares/{share_id}/workspace/provision",
            response_model=JobResponse, status_code=202,
        )
        async def provision_workspace(
            share_id: str, body: WorkspaceProvisionRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            await identity_http.require_csrf(request, principal)
            key = request.headers.get("idempotency-key", "")
            try:
                job = await job_runner.enqueue_workspace_provision(
                    actor_user_id=principal.user.id, share_id=share_id,
                    deck_id=body.deck_id, idempotency_key=key,
                )
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            except RuntimeError as exc:
                raise HTTPException(503, {
                    "code": "job_runner_unavailable", "message": str(exc),
                }) from exc
            return JobResponse.of(job)

        @router.post(
            "/shares/{share_id}/releases",
            response_model=JobResponse, status_code=202,
        )
        async def publish_release(
            share_id: str, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            await identity_http.require_csrf(request, principal)
            try:
                job = await job_runner.enqueue_release_publish(
                    actor_user_id=principal.user.id, share_id=share_id,
                    idempotency_key=request.headers.get("idempotency-key", ""),
                )
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            except RuntimeError as exc:
                raise HTTPException(503, {
                    "code": "job_runner_unavailable", "message": str(exc),
                }) from exc
            return JobResponse.of(job)

        @router.post(
            "/shares/{share_id}/installs",
            response_model=JobResponse, status_code=202,
        )
        async def install_release(
            share_id: str, body: ReleaseInstallRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            await identity_http.require_csrf(request, principal)
            try:
                job = await job_runner.enqueue_release_install(
                    actor_user_id=principal.user.id, share_id=share_id,
                    version=body.version, mode=body.mode,
                    idempotency_key=request.headers.get("idempotency-key", ""),
                )
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            except RuntimeError as exc:
                raise HTTPException(503, {
                    "code": "job_runner_unavailable", "message": str(exc),
                }) from exc
            return JobResponse.of(job)

        @router.post(
            "/subscriptions/{subscription_id}/updates",
            response_model=JobResponse, status_code=202,
        )
        async def enqueue_subscription_update(
            subscription_id: str, body: SubscriptionUpdateRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            await identity_http.require_csrf(request, principal)
            try:
                job = await job_runner.enqueue_subscription_update(
                    actor_user_id=principal.user.id, subscription_id=subscription_id,
                    target_version=body.target_version,
                    idempotency_key=request.headers.get("idempotency-key", ""),
                    mirror_preview_digest=body.mirror_preview_digest,
                    approve_templates=body.approve_templates,
                )
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            except RuntimeError as exc:
                raise HTTPException(503, {
                    "code": "job_runner_unavailable", "message": str(exc),
                }) from exc
            return JobResponse.of(job)

        @router.get("/subscriptions/{subscription_id}/mirror-preview")
        async def job_mirror_preview(
            subscription_id: str, target_version: int,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            preview = await _call(
                job_runner.updater.preview_mirror,
                actor_user_id=principal.user.id, subscription_id=subscription_id,
                target_version=target_version,
            )
            return {
                "subscription_id": preview.subscription_id,
                "target_version": preview.target_version,
                "tombstones": preview.tombstones, "digest": preview.digest,
            }

        @router.get("/jobs/{job_id}/conflicts")
        async def job_conflicts(
            job_id: str,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            conflicts = await _call(
                service.repository.list_job_conflicts,
                actor_user_id=principal.user.id, job_id=job_id,
            )
            return {"conflicts": [{
                "id": item.id, "entity_type": item.entity_type,
                "source_id": item.source_id, "field": item.field_name,
                "base_hash": item.base_hash, "local_hash": item.local_hash,
                "upstream_hash": item.upstream_hash, "resolution": item.resolution,
            } for item in conflicts]}

        @router.post("/jobs/{job_id}/conflicts/{conflict_id}/resolve")
        async def resolve_job_conflict(
            job_id: str, conflict_id: str, body: ConflictResolutionRequest,
            request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            await identity_http.require_csrf(request, principal)
            conflict = await _call(
                service.repository.resolve_conflict,
                actor_user_id=principal.user.id, job_id=job_id,
                conflict_id=conflict_id, resolution=body.resolution,
                now=service._clock(),
            )
            return {"id": conflict.id, "resolution": conflict.resolution}

        @router.post("/jobs/{job_id}/continue", response_model=JobResponse, status_code=202)
        async def continue_update_job(
            job_id: str, body: UpdateContinueRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            await identity_http.require_csrf(request, principal)
            try:
                job = await job_runner.continue_subscription_update(
                    actor_user_id=principal.user.id, job_id=job_id,
                    manual_values=body.manual_values,
                )
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            return JobResponse.of(job)

        @router.get("/jobs/{job_id}", response_model=JobResponse)
        async def get_job(
            job_id: str,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ) -> JobResponse:
            job = await asyncio.to_thread(
                job_runner.jobs.get_for_actor, job_id,
                actor_user_id=principal.user.id,
            )
            if job is None:
                raise HTTPException(404, {
                    "code": "not_found", "message": "job not found",
                })
            return JobResponse.of(job)

    @router.get("/shares", response_model=ShareListResponse)
    async def list_shares(
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareListResponse:
        shares = await _call(service.list_shares, principal.user.id)
        return ShareListResponse(shares=[ShareResponse.of(item) for item in shares])

    @router.post("/shares", response_model=ShareResponse, status_code=201)
    async def create_share(
        body: ShareCreateRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareResponse:
        await identity_http.require_csrf(request, principal)
        share = await _call(
            service.create_share, actor_user_id=principal.user.id, name=body.name,
        )
        return ShareResponse.of(share)

    @router.get("/shares/{share_id}", response_model=ShareDetailResponse)
    async def get_share(
        share_id: str,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareDetailResponse:
        detail = await _call(
            service.get_share, actor_user_id=principal.user.id, share_id=share_id,
        )
        return ShareDetailResponse.of(detail)

    @router.post(
        "/shares/{share_id}/invitations", response_model=ShareInviteResponse,
        status_code=201,
    )
    async def create_invite(
        share_id: str, body: ShareInviteRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareInviteResponse:
        await identity_http.require_csrf(request, principal)
        grant = await _call(
            service.create_invite, actor_user_id=principal.user.id, share_id=share_id,
            role=body.role, intended_user_id=body.intended_user_id,
            lifetime=timedelta(seconds=body.expires_in_seconds),
        )
        return ShareInviteResponse(
            id=grant.invite.id, role=grant.invite.role,
            expires_at=grant.invite.expires_at, token=grant.token,
            accept_url="/shares/invite#",
        )

    @router.post(
        "/share-invitations/accept", response_model=MembershipResponse,
    )
    async def accept_invite(
        body: ShareInviteAcceptRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> MembershipResponse:
        await identity_http.require_csrf(request, principal)
        membership = await _call(
            service.accept_invite, actor_user_id=principal.user.id,
            token=body.token.get_secret_value(),
        )
        return MembershipResponse.of(membership)

    @router.delete("/shares/{share_id}/invitations/{invite_id}", status_code=204)
    async def revoke_invite(
        share_id: str, invite_id: str, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> Response:
        await identity_http.require_csrf(request, principal)
        await _call(
            service.revoke_invite, actor_user_id=principal.user.id,
            share_id=share_id, invite_id=invite_id,
        )
        return Response(status_code=204)

    @router.delete("/shares/{share_id}/members/{user_id}", status_code=204)
    async def remove_member(
        share_id: str, user_id: str, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> Response:
        await identity_http.require_csrf(request, principal)
        await _call(
            service.remove_member, actor_user_id=principal.user.id,
            share_id=share_id, target_user_id=user_id,
        )
        await sockets.revoke(share_id, user_id)
        return Response(status_code=204)

    @router.websocket("/shares/{share_id}/events")
    async def share_events(websocket: WebSocket, share_id: str):
        host = websocket.headers.get("host", "")
        token = websocket.cookies.get(identity_http.cookie_name)
        session = await asyncio.to_thread(
            identity_http.service.authenticate, token, refresh=False,
        )
        if session is None or not host_allowed(host, allowed_hosts) or not origin_ok(
            "WS", websocket.headers, host, allowed_hosts, has_session=True,
        ):
            await websocket.close(code=1008)
            return
        try:
            await asyncio.to_thread(
                service.repository.require_member,
                actor_user_id=session.user_id, share_id=share_id,
            )
        except AuthorizationError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await sockets.register(share_id, session.user_id, websocket)
        try:
            while True:
                await websocket.receive_text()
                try:
                    await asyncio.to_thread(
                        service.repository.require_member,
                        actor_user_id=session.user_id, share_id=share_id,
                    )
                except AuthorizationError:
                    await websocket.close(code=1008)
                    return
                await websocket.send_json({"type": "heartbeat"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await sockets.unregister(share_id, session.user_id, websocket)

    if workspace is not None:
        @router.patch("/shares/{share_id}/workspace/notes/{guid}")
        async def edit_workspace_note(
            share_id: str, guid: str, body: WorkspaceNotePatch, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            await identity_http.require_csrf(request, principal)
            try:
                note = await asyncio.to_thread(
                    workspace.edit_note, actor_user_id=principal.user.id,
                    share_id=share_id, guid=guid, fields=body.fields,
                    expected_revision=body.expected_revision,
                )
            except EditConflictError as exc:
                raise HTTPException(409, {
                    "code": "edit_conflict", "message": str(exc),
                    "latest": {
                        "guid": exc.latest.guid, "fields": exc.latest.fields,
                        "tags": exc.latest.tags, "revision": exc.latest.revision,
                    },
                }) from exc
            except (AuthorizationError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc
            return {
                "note": {"guid": note.guid, "fields": note.fields, "tags": note.tags},
                "revision": note.revision,
            }

        @router.post("/shares/{share_id}/workspace/comments", status_code=201)
        async def add_workspace_comment(
            share_id: str, body: WorkspaceCommentRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            await identity_http.require_csrf(request, principal)
            comment = await _call(
                service.repository.add_workspace_comment,
                actor_user_id=principal.user.id, share_id=share_id,
                entity_type=body.entity_type, entity_id=body.entity_id,
                body=body.body, now=service._clock(),
            )
            return {
                "id": comment.id, "share_id": comment.share_id,
                "entity_type": comment.entity_type, "entity_id": comment.entity_id,
                "author_user_id": comment.author_user_id, "body": comment.body,
                "created_at": comment.created_at,
            }

    if updater is not None:
        @router.post("/subscriptions/{subscription_id}/updates", status_code=202)
        async def update_subscription(
            subscription_id: str, body: SubscriptionUpdateRequest, request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            await identity_http.require_csrf(request, principal)
            key = request.headers.get("idempotency-key", "")
            try:
                result = await asyncio.to_thread(
                    updater.run, actor_user_id=principal.user.id,
                    subscription_id=subscription_id, target_version=body.target_version,
                    idempotency_key=key,
                    mirror_preview_digest=body.mirror_preview_digest,
                    approve_templates=body.approve_templates,
                )
                return {"job_id": result.job_id, "state": "succeeded"}
            except UnresolvedUpdateError as exc:
                return {"job_id": exc.job_id, "state": "conflicts"}
            except MirrorPreviewRequired as exc:
                raise HTTPException(409, {
                    "code": "mirror_preview_required", "message": str(exc),
                }) from exc
            except DestructiveTemplateChangeError as exc:
                raise HTTPException(409, {
                    "code": "template_approval_required", "message": str(exc),
                }) from exc
            except (AuthorizationError, ConflictError, NotFoundError, ValueError) as exc:
                raise _http_error(exc) from exc

        @router.get("/subscriptions/{subscription_id}/mirror-preview")
        async def mirror_preview(
            subscription_id: str, target_version: int,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            preview = await _call(
                updater.preview_mirror, actor_user_id=principal.user.id,
                subscription_id=subscription_id, target_version=target_version,
            )
            return {
                "subscription_id": preview.subscription_id,
                "target_version": preview.target_version,
                "tombstones": preview.tombstones, "digest": preview.digest,
            }

        @router.get("/jobs/{job_id}/conflicts")
        async def list_conflicts(
            job_id: str,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            conflicts = await _call(
                service.repository.list_job_conflicts,
                actor_user_id=principal.user.id, job_id=job_id,
            )
            return {"conflicts": [{
                "id": item.id, "entity_type": item.entity_type,
                "source_id": item.source_id, "field": item.field_name,
                "base_hash": item.base_hash, "local_hash": item.local_hash,
                "upstream_hash": item.upstream_hash, "resolution": item.resolution,
            } for item in conflicts]}

        @router.post("/jobs/{job_id}/conflicts/{conflict_id}/resolve")
        async def resolve_conflict(
            job_id: str, conflict_id: str, body: ConflictResolutionRequest,
            request: Request,
            principal: IdentityPrincipal = Depends(identity_http.require_principal),
        ):
            await identity_http.require_csrf(request, principal)
            if body.resolution == "manual" and body.manual_value is None:
                raise HTTPException(422, {
                    "code": "manual_value_required", "message": "manual value required",
                })
            conflict = await _call(
                service.repository.resolve_conflict,
                actor_user_id=principal.user.id, job_id=job_id,
                conflict_id=conflict_id, resolution=body.resolution,
                now=service._clock(),
            )
            job = updater.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, {"code": "not_found", "message": "job not found"})
            try:
                result = await asyncio.to_thread(
                    updater.run, actor_user_id=principal.user.id,
                    subscription_id=job.resource_id,
                    target_version=int(job.progress["target_version"]),
                    idempotency_key=job.idempotency_key,
                    mirror_preview_digest=job.progress.get("mirror_preview_digest"),
                    approve_templates=bool(job.progress.get("approve_templates")),
                    manual_values={conflict.id: body.manual_value}
                    if body.resolution == "manual" else {},
                )
                state = "succeeded" if result.applied else "running"
            except UnresolvedUpdateError:
                state = "conflicts"
            return {"id": conflict.id, "resolution": conflict.resolution, "state": state}

    return router
