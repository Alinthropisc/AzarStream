"""Pydantic DTO schemas for admin users."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from models.admin import AdminRole


class AdminUserDTO(BaseModel):
    """DTO for admin user list/detail."""

    id: str
    username: str
    email: str | None
    name: str | None
    role: AdminRole
    is_active: bool
    is_superadmin: bool
    last_login: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def convert_uuid_to_str(cls, v):
        """Convert UUID to string for JSON serialization."""
        if isinstance(v, UUID):
            return str(v)
        if v is None:
            return ""
        return str(v)

    model_config = {"from_attributes": True}


class AdminCreateDTO(BaseModel):
    """DTO for creating a new admin user."""

    username: str = Field(..., min_length=3, max_length=64)
    email: str | None = None
    password: str = Field(..., min_length=8)
    name: str | None = None
    role: AdminRole = AdminRole.ADMIN


class AdminUpdateDTO(BaseModel):
    """DTO for updating an admin user."""

    email: str | None = None
    name: str | None = None
    role: AdminRole | None = None
    is_active: bool | None = None
    password: str | None = Field(None, min_length=8)
