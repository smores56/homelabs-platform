"""Thin HTTP service modules for the portable data-plane libraries.

The modules are transports only: they parse a JSON body, check one credential,
call a library method, and render the result. Every service is offline against
a local SQLite file or a fixture backend; none opens a socket to a cluster, a
provider, or a backup repository.
"""
