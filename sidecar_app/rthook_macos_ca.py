"""Give frozen macOS TLS clients the CA bundle shipped with the application.

TUF's urllib3 fetcher uses OpenSSL defaults, not requests/certifi defaults.
The build runner's OpenSSL CA paths need not exist on the destination Mac.
Run before application imports; never override an operator's explicit trust file.
"""

import os
import sys

if sys.platform == "darwin" and getattr(sys, "frozen", False):
    if "SSL_CERT_FILE" not in os.environ:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
