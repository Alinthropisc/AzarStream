"""Repository for admin_users table operations."""

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.admin import AdminUser, AdminRole


class AdminUserRepository:
    """Repository for AdminUser CRUD operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, admin_id: str) -> AdminUser | None:
        """Get admin user by ID."""
        result = await self.session.execute(select(AdminUser).where(AdminUser.id == admin_id))
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> AdminUser | None:
        """Get admin user by username."""
        result = await self.session.execute(select(AdminUser).where(AdminUser.username == username))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> AdminUser | None:
        """Get admin user by email."""
        result = await self.session.execute(select(AdminUser).where(AdminUser.email == email))
        return result.scalar_one_or_none()

    async def get_all(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminUser]:
        """Get all admin users with pagination."""
        result = await self.session.execute(select(AdminUser).order_by(AdminUser.created_at.desc()).limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count(self) -> int:
        """Get total count of admin users."""
        from sqlalchemy import func

        result = await self.session.execute(select(func.count()).select_from(AdminUser))
        return result.scalar() or 0

    async def create(
        self,
        *,
        username: str,
        email: str,
        hashed_password: str,
        role: AdminRole = AdminRole.ADMIN,
        name: str | None = None,
        is_superadmin: bool = False,
    ) -> AdminUser:
        """Create a new admin user."""
        admin = AdminUser(
            username=username,
            email=email,
            hashed_password=hashed_password,
            role=role,
            name=name,
            is_superadmin=is_superadmin,
        )
        self.session.add(admin)
        await self.session.flush()
        await self.session.refresh(admin)
        return admin

    async def update(
        self,
        admin_id: str,
        **kwargs,
    ) -> AdminUser | None:
        """Update admin user fields."""
        admin = await self.get_by_id(admin_id)
        if not admin:
            return None

        for key, value in kwargs.items():
            if hasattr(admin, key):
                setattr(admin, key, value)

        await self.session.flush()
        await self.session.refresh(admin)
        return admin

    async def delete(self, admin_id: str) -> bool:
        """Delete admin user by ID."""
        result = await self.session.execute(delete(AdminUser).where(AdminUser.id == admin_id))
        return result.rowcount > 0

    async def get_superadmins(self) -> list[AdminUser]:
        """Get all superadmin users."""
        result = await self.session.execute(
            select(AdminUser).where(
                AdminUser.is_superadmin.is_(True),
                AdminUser.is_active.is_(True),
            )
        )
        return list(result.scalars().all())

    async def get_active_admins(self) -> list[AdminUser]:
        """Get all active admin users."""
        result = await self.session.execute(select(AdminUser).where(AdminUser.is_active.is_(True)))
        return list(result.scalars().all())
