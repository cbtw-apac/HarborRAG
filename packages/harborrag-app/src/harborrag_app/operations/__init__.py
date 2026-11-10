"""Transport-neutral pieces that the HTTP routes and the CLI share.

Nothing here imports a web framework. ``Operations`` is the port both transports call;
``LocalOperations`` runs it in-process. Errors map onto one status and code through
``error_body``; ``Scope`` carries the caller and tenant of an operation.

Start from: Operations, LocalOperations, Scope, error_body.
"""

from .errors import CATALOG as CATALOG
from .errors import ErrorBody as ErrorBody
from .errors import ReferenceNotFoundError as ReferenceNotFoundError
from .errors import TenantForbiddenError as TenantForbiddenError
from .errors import TenantRequiredError as TenantRequiredError
from .errors import error_body as error_body
from .local import LocalOperations as LocalOperations
from .port import Operations as Operations
from .results import Accepted as Accepted
from .results import Saved as Saved
from .results import Tagged as Tagged
from .scope import ConnectionRef as ConnectionRef
from .scope import Scope as Scope
