"""Concrete implementations of the ports defined in :mod:`taxonomaid.ports`.

Adapters may import :mod:`taxonomaid.domain` and :mod:`taxonomaid.ports` but
**must not** import :mod:`taxonomaid.services`. The composition root in
:mod:`taxonomaid.bootstrap` is the only place that imports both ports and
adapters.
"""

from __future__ import annotations
