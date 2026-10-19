"""Engine security: API keys, ACL classes, access decisions and the access service.

``decide`` and ``class_readers`` are deliberately not re-exported: each is named like
its own submodule, and binding the function here would shadow the module. Import
them from ``harborrag_engine.security.decide`` and
``harborrag_engine.security.class_readers``.
"""

from .access_service import AccessLimits, ClassAccessService, CorpusPolicy
from .admit import StageDecision, stage_decision
from .api_key_service import (
    MAX_LIFETIME,
    MIN_LIFETIME,
    ApiKeyManagementService,
    ApiKeyPolicyError,
    ApiKeyVerificationService,
    CreatedKey,
    VerifiedKey,
)
from .class_readers import ClassReaders, class_snapshot
from .derived import classes_of, may_serve
from .fingerprint import acl_fingerprint, canonical_acl, class_of
from .resolver import account_index, expand, expand_all

__all__ = [
    "MAX_LIFETIME",
    "MIN_LIFETIME",
    "AccessLimits",
    "ApiKeyManagementService",
    "ApiKeyPolicyError",
    "ApiKeyVerificationService",
    "ClassAccessService",
    "ClassReaders",
    "CorpusPolicy",
    "CreatedKey",
    "StageDecision",
    "VerifiedKey",
    "account_index",
    "acl_fingerprint",
    "canonical_acl",
    "class_of",
    "class_snapshot",
    "classes_of",
    "expand",
    "expand_all",
    "may_serve",
    "stage_decision",
]
