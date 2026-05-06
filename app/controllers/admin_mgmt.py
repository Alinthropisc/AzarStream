"""Web controller for admin user management."""

from urllib.parse import quote

from litestar import Controller, get, post
from litestar.response import Template, Redirect
from litestar.connection import Request

from services.admin import AdminUserService
from schemas.admin import AdminCreateDTO
from models.admin import AdminRole
from app.logging import get_logger

log = get_logger("admin_controller")


class AdminManagementController(Controller):
    """Controller for managing admin users via web UI."""

    path = "/admin/admins"

    @get()
    async def list_admins(
        self,
        request: Request,
    ) -> Template:
        """List all admin users."""
        service = AdminUserService()
        admins = await service.get_all_admins()

        return Template(
            template_name="admin/admins/list.html",
            context={
                "admins": admins,
                "now": lambda: __import__("datetime").datetime.now(),
                "request": request,
            },
        )

    @get("/create")
    async def create_form(
        self,
        request: Request,
    ) -> Template:
        """Show create admin form."""
        return Template(
            template_name="admin/admins/create.html",
            context={
                "now": lambda: __import__("datetime").datetime.now(),
                "request": request,
            },
        )

    @post("/create")
    async def create_admin(
        self,
        request: Request,
    ) -> Redirect:
        """Create a new admin user."""
        service = AdminUserService()

        try:
            form_data = await request.form()
            data = AdminCreateDTO(
                username=form_data.get("username", ""),
                email=form_data.get("email") or None,
                password=form_data.get("password", ""),
                name=form_data.get("name") or None,
                role=AdminRole(form_data.get("role", "admin")),
            )

            await service.create_admin(data)
            return Redirect(path="/admin/admins?message=Admin+user+created+successfully")
        except ValueError as e:
            return Redirect(path=f"/admin/admins/create?error={quote(str(e))}")
        except Exception as e:
            return Redirect(path=f"/admin/admins/create?error={quote(str(e))}")

    @post("/{admin_id:str}/toggle-active")
    async def toggle_active(
        self,
        request: Request,
        admin_id: str,
    ) -> Redirect:
        """Toggle admin active status."""
        service = AdminUserService()

        try:
            await service.toggle_active(admin_id)
            return Redirect(path="/admin/admins?message=Admin+status+updated")
        except ValueError as e:
            return Redirect(path=f"/admin/admins?error={quote(str(e))}")

    @post("/{admin_id:str}/delete")
    async def delete_admin(
        self,
        request: Request,
        admin_id: str,
    ) -> Redirect:
        """Delete an admin user."""
        service = AdminUserService()

        try:
            await service.delete_admin(admin_id)
            return Redirect(path="/admin/admins?message=Admin+user+deleted")
        except ValueError as e:
            return Redirect(path=f"/admin/admins?error={quote(str(e))}")
