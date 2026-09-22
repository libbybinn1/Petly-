"""Development entry point for the PetMatch web application.

Usage:
    .venv/Scripts/python.exe run.py
"""

from __future__ import annotations

from app import create_app
from app.config import load_configuration


def main() -> None:
    """Start the Flask development server."""
    configuration = load_configuration()
    application = create_app(configuration)
    application.run(
        host="127.0.0.1",
        port=configuration.flask_port,
        debug=configuration.is_development,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
