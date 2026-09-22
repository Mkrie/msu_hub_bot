"""Optional routing bridge to Derp's existing paid-image application.

Import this module only in a Derp environment satisfying TeleForge's declared
dependencies. Mount the bridge before the host's native image/tool-approval
routers, keeping both native routers for cancel, style, funding and malformed
callback controls. Native handlers retain acknowledgement, presentation,
authorization, operation coordination and recovery ownership.
"""

from aiogram import F
from derp.approvals import DeferredToolApprovalService
from derp.billing import CLOSED_COMMERCE_POLICY, CommercePolicy
from derp.db import DatabaseManager
from derp.delivery import DeliveryResendCallback, DeliveryService
from derp.features import ImageOperationCoordinator
from derp.filters.meta import MetaCommand, MetaInfo
from derp.handlers.image import handle_edit, handle_imagine, resend_image_delivery
from derp.handlers.tool_approvals import ImageApprovalAction, ImageApprovalCallback, approve_image_tool
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.tools.authorization import ActorRoleResolver
from teleforge import CallbackContext, Feature, MessageContext, callback, command

_IMAGINE = ("imagine", "image", "img", "и")
_EDIT = ("edit", "ed", "e", "е")


class NativeImages(Feature, key="derp.native.images"):
    """Group native routes without replacing the application's paid workflow."""

    def __init__(
        self,
        *,
        image_operations: ImageOperationCoordinator,
        approvals: DeferredToolApprovalService,
        delivery: DeliveryService,
        db: DatabaseManager,
    ) -> None:
        self.image_operations = image_operations
        self.approvals = approvals
        self.delivery = delivery
        self.db = db

    @command(*_IMAGINE, filter=MetaCommand(*_IMAGINE), flags={"chat_action": {"initial_sleep": 2, "action": "upload_photo"}})
    async def imagine(
        self,
        ctx: MessageContext,
        *,
        meta: MetaInfo,
        user_model: UserModel | None = None,
        chat_model: ChatModel | None = None,
    ) -> None:
        await handle_imagine(
            message=ctx.event,
            meta=meta,
            image_operation_coordinator=self.image_operations,
            deferred_tool_approval_service=self.approvals,
            user_model=user_model,
            chat_model=chat_model,
        )

    @command(*_EDIT, filter=MetaCommand(*_EDIT), flags={"chat_action": {"initial_sleep": 2, "action": "upload_photo"}})
    async def edit(
        self,
        ctx: MessageContext,
        *,
        meta: MetaInfo,
        user_model: UserModel | None = None,
        chat_model: ChatModel | None = None,
    ) -> None:
        await handle_edit(
            message=ctx.event,
            meta=meta,
            image_operation_coordinator=self.image_operations,
            deferred_tool_approval_service=self.approvals,
            user_model=user_model,
            chat_model=chat_model,
        )

    @callback(ImageApprovalCallback.filter(F.action == ImageApprovalAction.RUN), ack="manual")
    async def approve(
        self,
        ctx: CallbackContext,
        callback_data: ImageApprovalCallback,
        *,
        user_model: UserModel | None = None,
        chat_model: ChatModel | None = None,
        actor_role_resolver: ActorRoleResolver | None = None,
        commerce_policy: CommercePolicy = CLOSED_COMMERCE_POLICY,
    ) -> None:
        await approve_image_tool(
            callback=ctx.query,
            callback_data=callback_data,
            db=self.db,
            image_operation_coordinator=self.image_operations,
            deferred_tool_approval_service=self.approvals,
            user_model=user_model,
            chat_model=chat_model,
            actor_role_resolver=actor_role_resolver,
            commerce_policy=commerce_policy,
        )

    @callback(DeliveryResendCallback, ack="manual")
    async def resend(self, ctx: CallbackContext, callback_data: DeliveryResendCallback) -> None:
        await resend_image_delivery(
            callback=ctx.query,
            callback_data=callback_data,
            delivery_service=self.delivery,
        )
