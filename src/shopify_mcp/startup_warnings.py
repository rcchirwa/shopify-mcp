"""Operator-facing warnings for security opt-outs active in the current config.

Story 10.56 (SEC-22), follow-up to SEC-17 (Story 10.51): ``WEBHOOK_ALLOW_ANY_HOST``
lets an operator explicitly weaken ``register_webhook``'s allowlist enforcement,
but nothing surfaced that the opt-out was active — a deployment could carry it
indefinitely with no visible signal. This module is a covered leaf kept
separate from ``server.py`` (an entry point excluded from the coverage gate)
specifically so the check itself stays unit-testable: ``create_server()`` calls
``startup_warnings(settings)`` and logs each returned string once, at WARNING,
after ``configure_logging`` has attached the stderr handler.

Startup-only by design, not per ``register_webhook`` call: the point is to
catch the *configuration* sitting in a weakened posture, not to repeat a
warning that duplicates the per-call "EXTERNAL DOMAIN" annotation
``tools/webhooks.py``'s ``_check_endpoint`` already puts in the tool's own
preview output.
"""

from shopify_mcp.settings import Settings

_WEBHOOK_ALLOW_ANY_HOST_WARNING = (
    "WEBHOOK_ALLOW_ANY_HOST is enabled: register_webhook will accept ANY https "
    "host as a webhook endpoint, bypassing the WEBHOOK_ALLOWLIST_HOSTS allowlist "
    "entirely. Unset WEBHOOK_ALLOW_ANY_HOST (and configure WEBHOOK_ALLOWLIST_HOSTS) "
    "to restore the default fail-closed posture."
)


def startup_warnings(settings: Settings) -> list[str]:
    """Return the operator-facing warnings implied by a weakened ``settings`` posture.

    Empty when no security opt-out is active. Callers log each returned string
    at WARNING level once, at process startup.
    """
    warnings: list[str] = []
    if settings.webhook_allow_any_host:
        warnings.append(_WEBHOOK_ALLOW_ANY_HOST_WARNING)
    return warnings
