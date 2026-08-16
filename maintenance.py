import logging
from typing import Dict, Any

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.account import ResetAuthorizationRequest
from telethon.tl.functions.contacts import DeleteContactsRequest
from telethon.tl.types import InputUser

from database import save_maintenance_result

logger = logging.getLogger(__name__)


async def terminate_other_sessions(
    account_id: int,
    client: TelegramClient,
) -> Dict[str, Any]:
    terminated = 0
    failed = 0
    current_skipped = 0

    try:
        result = await client(GetAuthorizationsRequest())

        for authorization in result.authorizations:
            if authorization.current:
                current_skipped += 1
                continue

            try:
                await client(
                    ResetAuthorizationRequest(
                        hash=authorization.hash
                    )
                )
                terminated += 1

            except RPCError as exc:
                failed += 1
                logger.warning(
                    "Unable to terminate session: %s",
                    exc,
                )

        details = (
            f"terminated={terminated}; "
            f"failed={failed}; "
            f"current_skipped={current_skipped}"
        )

        await save_maintenance_result(
            account_id,
            "terminate_other_sessions",
            "ok" if failed == 0 else "partial",
            details,
        )

        return {
            "terminated": terminated,
            "failed": failed,
            "current_skipped": current_skipped,
        }

    except Exception as exc:
        logger.exception("Session cleanup failed")

        await save_maintenance_result(
            account_id,
            "terminate_other_sessions",
            "error",
            str(exc),
        )

        raise


async def delete_contacts(
    account_id: int,
    client: TelegramClient,
) -> Dict[str, int]:
    deleted = 0
    failed = 0

    try:
        contacts = []

        async for dialog in client.iter_dialogs():
            entity = dialog.entity

            if not getattr(entity, "bot", False):
                if getattr(entity, "phone", None):
                    contacts.append(
                        InputUser(
                            user_id=entity.id,
                            access_hash=entity.access_hash,
                        )
                    )

        batch_size = 100

        for start in range(0, len(contacts), batch_size):
            batch = contacts[start:start + batch_size]

            try:
                await client(
                    DeleteContactsRequest(
                        id=batch
                    )
                )
                deleted += len(batch)

            except RPCError as exc:
                failed += len(batch)
                logger.warning(
                    "Contact deletion batch failed: %s",
                    exc,
                )

        details = (
            f"deleted={deleted}; "
            f"failed={failed}"
        )

        await save_maintenance_result(
            account_id,
            "delete_contacts",
            "ok" if failed == 0 else "partial",
            details,
        )

        return {
            "deleted": deleted,
            "failed": failed,
        }

    except Exception as exc:
        logger.exception("Contact cleanup failed")

        await save_maintenance_result(
            account_id,
            "delete_contacts",
            "error",
            str(exc),
        )

        raise
