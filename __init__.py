"""Directory-plugin entry point for persistent Hermes installations."""

if __package__:
    from .hermes_mobile.plugin import register
else:  # Pytest imports a project-root ``__init__.py`` as a plain module.
    from hermes_mobile.plugin import register

__all__ = ["register"]
