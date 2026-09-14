"""Descriptor-relative workflow template drift evaluator."""

import logging as _logging

# Under a FIPS-only OpenSSL provider, ``import hashlib`` calls ``logging.exception``
# for unavailable constructors (md5, blake2b, blake2s). With no root handlers,
# logging auto-configures a default stderr handler.
# Keep a root handler only while engine imports hashlib; do not alter the embedding
# application's logging behavior after this package import completes.
_root_logger = _logging.getLogger()
_null_handler = _logging.NullHandler()
_root_logger.addHandler(_null_handler)
try:
    from .engine import evaluate
finally:
    _root_logger.removeHandler(_null_handler)
del _root_logger, _null_handler, _logging

__all__ = ["evaluate"]
__version__ = "0.1.0-spike"

