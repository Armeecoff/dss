import asyncio
import logging
import os

import database as db
from bot import build_application

logger = logging.getLogger(__name__)

async def setup():
    await db.init_db()
    first_admin = os.getenv("ADMIN_ID", "")
    if first_admin.isdigit():
        await db.add_admin(int(first_admin))
        logger.info(f"Admin {first_admin} registered.")

def main():
    asyncio.get_event_loop().run_until_complete(setup())
    app = build_application()
    logger.info("Bot is starting...")
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "callback_query",
            "pre_checkout_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ]
    )

if __name__ == "__main__":
    main()
