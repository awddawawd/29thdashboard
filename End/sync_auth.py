"""Jetons de synchronisation scopés par locataire.

Le conteneur d'un locataire ne doit **jamais** détenir de secret du plan de
contrôle (clé Supabase ``service_role``, clé de signature JWT…). Il reçoit
uniquement un jeton HMAC dérivé de son propre identifiant :

* la signature ne peut pas être forgée sans le secret serveur ;
* un jeton volé dans le conteneur du locataire A ne donne accès qu'aux données
  de A (le tenant est porté par le jeton, jamais par un paramètre de requête) ;
* aucun accès direct à la base : tout passe par l'API de synchronisation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_DEFAULT_SECRET_KEY = "waas-secret-key-change-in-production"
_MAX_TENANT_ID_LENGTH = 128


def _sync_secret() -> bytes:
    dedicated = os.getenv("WAAS_SYNC_SECRET")
    if dedicated:
        return dedicated.encode("utf-8")
    # Repli : dérivation depuis la clé de signature de l'API (un seul secret à gérer).
    base = os.getenv("WAAS_SECRET_KEY") or _DEFAULT_SECRET_KEY
    return hashlib.sha256(base.encode("utf-8") + b"|waas-sync-v1").digest()


def _signature(tenant_id: str) -> str:
    mac = hmac.new(
        _sync_secret(), f"waas-sync:{tenant_id}".encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def issue_tenant_token(tenant_id: str) -> str:
    """Émet le jeton d'un locataire (appelé par le plan de contrôle uniquement)."""
    clean = (tenant_id or "").strip()
    if not clean or len(clean) > _MAX_TENANT_ID_LENGTH:
        raise ValueError("tenant_id invalide pour l'émission d'un jeton de synchronisation")
    return f"{clean}.{_signature(clean)}"


def verify_tenant_token(token: str | None) -> str | None:
    """Retourne le ``tenant_id`` si le jeton est authentique, sinon ``None``."""
    if not isinstance(token, str) or not token:
        return None
    tenant_id, separator, signature = token.rpartition(".")
    if not separator or not tenant_id or not signature:
        return None
    if len(tenant_id) > _MAX_TENANT_ID_LENGTH or len(signature) > 128:
        return None
    # compare_digest lève TypeError sur une chaîne non ASCII : 401, jamais 500.
    if not signature.isascii():
        return None
    if not hmac.compare_digest(_signature(tenant_id), signature):
        return None
    return tenant_id


def uses_default_secret() -> bool:
    """True si aucun secret dédié n'a été fourni (jeton dérivé du secret JWT)."""
    return not os.getenv("WAAS_SYNC_SECRET") and not os.getenv("WAAS_SECRET_KEY")
