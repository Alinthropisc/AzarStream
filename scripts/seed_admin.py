"""Seed script to create first superadmin from env variables."""

import asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.connection import db
from repositories.admin import AdminUserRepository
from services.admin import AdminUserService
from models.admin import AdminRole
from app.config import settings


async def seed_superadmin():
    """Create superadmin from env if it doesn't exist."""
    # Ensure database is connected
    if not db._engine:
        await db.connect()

    try:
        # Get a session
        async with db.session() as session:
            # Check if table exists
            try:
                await session.execute(text("SELECT 1 FROM admin_users LIMIT 1"))
            except Exception:
                # Table might not exist yet if migrations haven't run
                return

            repo = AdminUserRepository(session)
            service = AdminUserService()

            # Check if env admin already exists
            existing = await repo.get_by_username(settings.admin_username)
            if existing:
                return

            try:
                # Create superadmin
                admin = await repo.create(
                    username=settings.admin_username,
                    email=None,
                    hashed_password=service.hash_password(settings.admin_password),
                    role=AdminRole.ADMIN,
                    name="Environment Superadmin",
                    is_superadmin=True,
                )
                await session.commit()
                print(f"✅ Created superadmin: {settings.admin_username}")
            except IntegrityError:
                # Someone else created it at the same time
                await session.rollback()
            except Exception as e:
                print(f"⚠️  Error creating superadmin: {e}")
                await session.rollback()

    except Exception as e:
        # Silently fail for lifespan, but print for manual run
        if __name__ == "__main__":
            print(f"❌ Failed to seed superadmin: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(seed_superadmin())
