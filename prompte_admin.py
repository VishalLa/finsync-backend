from __future__ import annotations

import sys

from service.client import call_service


def promote(identifier: str) -> bool:
    return call_service("admin.promote_user", identifier=identifier)


if __name__ == "__main__":
	if len(sys.argv) != 2:
		print("Usage: python prompte_admin.py <email-or-user-id>")
		sys.exit(1)

	sys.exit(0 if promote(sys.argv[1]) else 1)
