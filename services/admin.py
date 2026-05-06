"""Service for admin user management."""

import bcrypt

from repositories.uow import UnitOfWork
from repositories.admin import AdminUserRepository
from models.admin import AdminRole
from schemas.admin import AdminCreateDTO, AdminUpdateDTO, AdminUserDTO


class AdminUserService:
    """Service for managing admin users."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(),
        ).decode("utf-8")

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash."""
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )

    async def get_all_admins(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminUserDTO]:
        """Get all admin users."""
        async with UnitOfWork() as uow:
            admins = await uow.admins.get_all(limit=limit, offset=offset)
            return [AdminUserDTO.model_validate(a) for a in admins]

    async def get_admin_by_id(self, admin_id: str) -> AdminUserDTO | None:
        """Get admin user by ID."""
        async with UnitOfWork() as uow:
            admin = await uow.admins.get_by_id(admin_id)
            return AdminUserDTO.model_validate(admin) if admin else None

    async def get_admin_by_username(self, username: str) -> AdminUserDTO | None:
        """Get admin user by username."""
        async with UnitOfWork() as uow:
            admin = await uow.admins.get_by_username(username)
            return AdminUserDTO.model_validate(admin) if admin else None

    async def create_admin(
        self,
        data: AdminCreateDTO,
        created_by: str | None = None,
        is_superadmin: bool = False,
    ) -> AdminUserDTO:
        """Create a new admin user."""
        async with UnitOfWork() as uow:
            # Check for duplicate username
            existing = await uow.admins.get_by_username(data.username)
            if existing:
                raise ValueError(f"Username '{data.username}' already exists")

            # Check for duplicate email
            if data.email:
                existing_email = await uow.admins.get_by_email(data.email)
                if existing_email:
                    raise ValueError(f"Email '{data.email}' already exists")

            admin = await uow.admins.create(
                username=data.username,
                email=data.email,
                hashed_password=self.hash_password(data.password),
                role=data.role,
                name=data.name,
                is_superadmin=is_superadmin,
            )
            await uow.commit()

            return AdminUserDTO.model_validate(admin)

    async def update_admin(
        self,
        admin_id: str,
        data: AdminUpdateDTO,
    ) -> AdminUserDTO:
        """Update an admin user."""
        async with UnitOfWork() as uow:
            admin = await uow.admins.get_by_id(admin_id)
            if not admin:
                raise ValueError("Admin user not found")

            update_data = data.model_dump(exclude_unset=True, exclude_none=True)

            # Hash password if provided
            if "password" in update_data:
                update_data["hashed_password"] = self.hash_password(update_data.pop("password"))

            updated = await uow.admins.update(admin_id, **update_data)
            await uow.commit()

            return AdminUserDTO.model_validate(updated)

    async def delete_admin(self, admin_id: str) -> bool:
        """Delete an admin user."""
        async with UnitOfWork() as uow:
            # Prevent deleting superadmins
            admin = await uow.admins.get_by_id(admin_id)
            if admin and admin.is_superadmin:
                raise ValueError("Cannot delete a superadmin account")

            deleted = await uow.admins.delete(admin_id)
            await uow.commit()
            return deleted

    async def toggle_active(self, admin_id: str) -> AdminUserDTO:
        """Toggle admin active status."""
        async with UnitOfWork() as uow:
            admin = await uow.admins.get_by_id(admin_id)
            if not admin:
                raise ValueError("Admin user not found")

            if admin.is_superadmin:
                raise ValueError("Cannot deactivate a superadmin account")

            updated = await uow.admins.update(admin_id, is_active=not admin.is_active)
            await uow.commit()

            return AdminUserDTO.model_validate(updated)

    async def count_admins(self) -> int:
        """Get total count of admin users."""
        async with UnitOfWork() as uow:
            return await uow.admins.count()

    async def authenticate(self, username: str, password: str) -> AdminUserDTO | None:
        """Authenticate admin user by username and password."""
        async with UnitOfWork() as uow:
            admin = await uow.admins.get_by_username(username)

            if not admin or not admin.is_active:
                return None

            if not self.verify_password(password, admin.hashed_password):
                return None

            return AdminUserDTO.model_validate(admin)
