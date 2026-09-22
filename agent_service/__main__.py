"""Entry point: `python -m agent_service`.

Starts the independent agent process described in docs/AGENT.md.
"""

import sys

import truststore

# The corporate proxy re-signs HTTPS. This must run before any HTTPS client
# is constructed, or Tavily search fails certificate verification.
truststore.inject_into_ssl()

from agent_service.worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
