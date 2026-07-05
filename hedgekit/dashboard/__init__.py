"""Process D: the local Dashboard and alerting surface (SPEC S5.1).

Serves the operator dashboard and alerts on 127.0.0.1. Per SPEC S5.1 this
process holds **no exchange** credentials -- only its own dashboard auth
secret -- and reads evaluation and ledger data without any trade authority.
"""
