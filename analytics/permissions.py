from __future__ import annotations

import os

from rest_framework.permissions import BasePermission


def _analytics_admin_emails() -> set[str]:
    raw = os.getenv("ANALYTICS_ADMIN_EMAILS", "")

    return {
        email.strip().lower()
        for email in raw.split(",")
        if email.strip()
    }


class IsAnalyticsAdmin(BasePermission):
    """
    Allows access only to explicitly configured analytics administrators.

    This project uses a custom Customer auth model and does not expose
    Django's is_staff flag, so DRF's built-in IsAdminUser is not suitable.
    """

    message = "Analytics administrator access is required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)

        if not user:
            return False

        if not getattr(user, "is_authenticated", False):
            return False

        # Prefer the authenticated user's email.
        # Fall back to username because the current admin JWT
        # carries the administrator email as the username claim.
        email = str(
            getattr(user, "email", None)
            or getattr(user, "username", None)
            or ""
        ).strip().lower()

        if not email:
            return False

        return email in _analytics_admin_emails()