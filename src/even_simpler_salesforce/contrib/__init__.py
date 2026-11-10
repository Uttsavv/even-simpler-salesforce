"""Framework integrations (§13), each behind its own optional extra.

- :mod:`even_simpler_salesforce.contrib.fastapi` (``[fastapi]``)
- :mod:`even_simpler_salesforce.contrib.flask` (``[flask]``)

This package imports nothing: the framework is imported only when you import its module, so
the core keeps depending on httpx alone.
"""
