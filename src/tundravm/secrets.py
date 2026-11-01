"""Secrets: values an image needs at run time and never contains.

``Secret("jwt")`` is delivered after attestation (``tundravm secrets deliver INSTANCE
jwt=@jwt.txt``) and read as a systemd credential: ``Service(credentials=[jwt])``.
:class:`Credential` is a credential loaded from a file in the image, and
``Generated("jwt")`` a secret the image makes at boot, for its own services to share.
"""

from __future__ import annotations

from tundravm._image._secrets import Generated, Secret
from tundravm._image._values import Credential

__all__ = ["Credential", "Generated", "Secret"]
