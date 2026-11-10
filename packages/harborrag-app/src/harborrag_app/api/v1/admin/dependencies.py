"""Dependencies shared by the ``/v1/admin`` routes."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Depends, Request

from harborrag_app.api.schemas import Error
from harborrag_app.workflow_control.control_plane.settings_service import SettingsService


def _settings_service(request: Request) -> SettingsService:
    """The settings use cases of the app service the lifespan composed."""
    return cast(SettingsService, request.app.state.app_service.settings_service)


SettingsDependency = Annotated[SettingsService, Depends(_settings_service)]


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for error responses on the shared ``Error`` schema."""
    return {code: {"model": Error} for code in codes}
