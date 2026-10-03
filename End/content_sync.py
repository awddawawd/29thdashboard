"""Réconciliation du contenu éditable d'une page (logique pure, sans base).

À chaque démarrage d'une vitrine — et à chaque publication à chaud — le
conteneur envoie l'inventaire des emplacements éditables de chaque page de son
gabarit (``waas-base-image/waas_content.py``). L'API le confronte aux
enregistrements du locataire et en déduit un plan, par ordre de priorité :

1. **à sa place** : même clé, même texte d'origine (ou enregistrement historique
   sans ancre, que l'on complète : ``backfills``) ;
2. **déplacé** : le gabarit a été réorganisé et le texte d'origine se retrouve,
   à un seul endroit libre, sous une autre clé → la clé suit (``moves``) ;
3. **gabarit retouché** : la clé existe toujours mais son texte d'origine a
   changé et ne se retrouve nulle part (coquille corrigée, accroche réécrite).
   La personnalisation du client est conservée et l'ancre mise à jour ; une
   valeur jamais personnalisée suit le nouveau gabarit (``reanchors``) ;
4. **orphelin** : plus aucun emplacement ne lui correspond. Une valeur
   personnalisée est détachée (conservée pour récupération dans le Studio), une
   valeur par défaut est supprimée (``detach`` / ``delete``) ;
5. **nouvel emplacement** : indexé avec la valeur du gabarit (``inserts``).

Les valeurs à injecter (``placements``) se limitent aux emplacements dont la
valeur diffère du gabarit : une page jamais modifiée est servie à l'identique.

Sans la colonne ``source_value`` (migration ``002_editor.sql`` non appliquée),
seules les étapes 1 et 5 s'appliquent, par clé, comme le moteur historique.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Clé d'un enregistrement détaché (orphelin) : il ne correspond plus à la page.
ORPHAN_PREFIX = "orphan:"
#: Clé transitoire d'un déplacement en deux temps (évite les collisions d'unicité).
MOVING_PREFIX = "moving:"
CONTENT_KINDS = ("text", "image", "attr")
MAX_VALUE_LENGTH = 5000
MAX_URL_LENGTH = 2048
#: Ancres complétées par appel (mise à niveau) : le reste l'est aux démarrages suivants et
#: la requête du conteneur reste bornée dans le temps.
MAX_BACKFILLS_PER_CALL = 300

_BLANK_RUN = re.compile(r"[ \t\r\n]+")
#: Refusés par XML 1.0, donc par lxml dans le conteneur : contrôles, demi-codets isolés,
#: non-caractères U+FFFE / U+FFFF. Une telle valeur ferait échouer la publication.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def norm(value: Optional[str]) -> str:
    """Même normalisation que le conteneur : blancs réduits, insécables conservés."""
    return _BLANK_RUN.sub(" ", value or "").strip(" \t\r\n")


def validate_value(kind: str, value: object) -> tuple[Optional[str], Optional[str]]:
    """Valeur publiable pour ce type d'emplacement : ``(valeur, None)`` ou ``(None, motif)``.

    Mêmes règles que le conteneur (défense en profondeur) : une image n'accepte
    qu'une adresse http(s) ou un chemin, jamais ``javascript:`` ni ``data:``.
    """
    if not isinstance(value, str):
        return None, "Valeur texte attendue."
    if kind == "image":
        url = value.strip()
        if not url:
            return None, "Adresse d'image vide."
        if len(url) > MAX_URL_LENGTH or _CONTROL_CHARS.search(url) or any(c in url for c in '<>"`\\') \
                or any(char.isspace() for char in url):
            return None, "Adresse d'image invalide."
        if _URL_SCHEME.match(url) and not url.lower().startswith(("http://", "https://")):
            return None, "Seules les adresses http(s) ou les chemins du site sont acceptés."
        return url, None
    cleaned = _CONTROL_CHARS.sub("", value)
    if len(cleaned) > MAX_VALUE_LENGTH:
        return None, f"Texte trop long ({len(cleaned)} caractères, {MAX_VALUE_LENGTH} au maximum)."
    return cleaned, None


def is_detached(key: str) -> bool:
    return key.startswith(ORPHAN_PREFIX)


def orphan_key(key: str, stamp: str) -> str:
    return f"{ORPHAN_PREFIX}{stamp}:{key}"


def same_value(kind: str, left: Optional[str], right: Optional[str], exact: bool = False) -> bool:
    """Égalité au sens de l'affichage ; ``exact`` (texte préformaté) : les blancs comptent."""
    if exact:
        return (left or "") == (right or "")
    if kind == "image":
        return (left or "").strip() == (right or "").strip()
    return norm(left) == norm(right)


@dataclass(frozen=True)
class InventorySlot:
    key: str
    kind: str
    source: str
    exact: bool = False


@dataclass(frozen=True)
class StoredRecord:
    key: str
    kind: str
    value: str
    source: Optional[str] = None
    #: Ligne lue : une opération du plan ne touche qu'elle, et seulement si elle porte encore
    #: la clé lue — deux réconciliations concurrentes n'ont aucun effet croisé.
    id: Any = field(default=None, compare=False)

    @property
    def customized(self) -> bool:
        """Sans ancre connue, on suppose une personnalisation (prudence)."""
        return self.source is None or not same_value(self.kind, self.value, self.source)


@dataclass
class Plan:
    inserts: list[InventorySlot] = field(default_factory=list)
    backfills: list[tuple[str, str]] = field(default_factory=list)
    #: (clé, nouvelle ancre, nouvelle valeur ou None pour conserver celle du client)
    reanchors: list[tuple[str, str, Optional[str]]] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)
    detach: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    placements: dict[str, str] = field(default_factory=dict)
    #: Clé lue -> identifiant de la ligne correspondante (garde des écritures)
    ids: dict[str, Any] = field(default_factory=dict)

    @property
    def writes(self) -> int:
        return (len(self.inserts) + len(self.backfills) + len(self.reanchors) + len(self.moves)
                + len(self.detach) + len(self.delete))

    def stats(self) -> dict[str, int]:
        return {
            "inserted": len(self.inserts), "backfilled": len(self.backfills),
            "reanchored": len(self.reanchors), "moved": len(self.moves),
            "detached": len(self.detach), "deleted": len(self.delete),
            "placements": len(self.placements),
        }


def _kind(value: Optional[str]) -> str:
    return value if value in CONTENT_KINDS else "text"


def record_from_row(row: dict) -> StoredRecord:
    """Ligne ``tenant_content`` -> enregistrement (``source_value`` absente = ancre inconnue)."""
    source = row.get("source_value")
    return StoredRecord(
        key=str(row.get("xpath") or ""),
        kind=_kind(row.get("content_type")),
        value="" if row.get("content_value") is None else str(row.get("content_value")),
        source=None if source is None else str(source),
        id=row.get("id"),
    )


def reconcile(slots: Iterable[InventorySlot], records: Iterable[StoredRecord], anchors: bool,
              max_backfills: int = MAX_BACKFILLS_PER_CALL) -> Plan:
    """Plan de réconciliation d'**une** page (voir la docstring du module)."""
    plan = Plan()
    records = list(records)
    plan.ids = {record.key: record.id for record in records if record.id is not None}
    slot_by_key: dict[str, InventorySlot] = {}
    ordered: list[InventorySlot] = []
    for slot in slots:
        if slot.key not in slot_by_key:
            slot_by_key[slot.key] = slot
            ordered.append(slot)

    claimed: dict[str, str] = {}          # clé d'emplacement -> valeur retenue
    pending: list[StoredRecord] = []

    # 1. À sa place (ou historique sans ancre).
    for record in records:
        if not record.key or is_detached(record.key):
            continue
        slot = slot_by_key.get(record.key)
        if slot is None or slot.kind != record.kind:
            pending.append(record)
        elif not anchors or record.source is None:
            claimed[slot.key] = record.value
            if anchors and len(plan.backfills) < max_backfills:
                plan.backfills.append((record.key, slot.source))
        elif same_value(slot.kind, record.source, slot.source):
            claimed[slot.key] = record.value
        else:
            pending.append(record)

    if anchors:
        pending = _relocate(pending, ordered, claimed, plan)
        pending = _keep_retouched(pending, slot_by_key, claimed, plan)
        for record in pending:
            (plan.detach if record.customized else plan.delete).append(record.key)

    # 5. Nouveaux emplacements, puis valeurs à injecter.
    for slot in ordered:
        if slot.key not in claimed:
            plan.inserts.append(slot)
            continue
        value = claimed[slot.key]
        if not same_value(slot.kind, value, slot.source, slot.exact):
            plan.placements[slot.key] = value
    return plan


def _relocate(pending: list[StoredRecord], ordered: list[InventorySlot], claimed: dict[str, str],
              plan: Plan) -> list[StoredRecord]:
    """2. Le texte d'origine se retrouve à un seul endroit libre : la clé le suit."""
    free: dict[tuple[str, str], list[InventorySlot]] = {}
    for slot in ordered:
        if slot.key not in claimed:
            free.setdefault((slot.kind, norm(slot.source)), []).append(slot)
    remaining = []
    for record in pending:
        candidates = [] if record.source is None else [
            slot for slot in free.get((record.kind, norm(record.source)), []) if slot.key not in claimed
        ]
        if len(candidates) == 1 and candidates[0].key != record.key:
            target = candidates[0]
            claimed[target.key] = record.value
            plan.moves.append((record.key, target.key))
        else:
            remaining.append(record)
    return remaining


def _keep_retouched(pending: list[StoredRecord], slot_by_key: dict[str, InventorySlot],
                    claimed: dict[str, str], plan: Plan) -> list[StoredRecord]:
    """3. Même clé, texte du gabarit retouché : la personnalisation du client prime."""
    remaining = []
    for record in pending:
        slot = slot_by_key.get(record.key)
        if slot is None or slot.kind != record.kind or slot.key in claimed or record.source is None:
            remaining.append(record)
        elif record.customized:
            claimed[slot.key] = record.value
            plan.reanchors.append((record.key, slot.source, None))
        else:
            claimed[slot.key] = slot.source
            plan.reanchors.append((record.key, slot.source, slot.source))
    return remaining
