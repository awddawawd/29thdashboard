"""Rendu des pages du dossier Front : inclusions de fragments et paramètres.

Une page = un dossier contenant un ``index.html``. Les éléments communs (tête,
coque, chargeur, scripts) vivent une seule fois dans ``Front/components`` et
sont inclus par directive :

    <!-- @include admin/topbar.html title="Sites & vitrines" -->

Les paramètres d'une directive remplacent les ``{{cle}}`` du fragment inclus
(valeurs échappées HTML ; une clé inconnue devient une chaîne vide). Module sans
dépendance : partagé par l'API (End/main.py) et le serveur de dev (run.py).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Optional

INCLUDE_RE = re.compile(r'<!--\s*@include\s+([^\s>]+)((?:\s+[a-z_]+="[^"]*")*)\s*-->')
PARAM_RE = re.compile(r'([a-z_]+)="([^"]*)"')
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
MAX_DEPTH = 3


def resolve_includes(html_text: str, components_dir: Path, params: Optional[dict[str, str]] = None) -> str:
    """Résout les inclusions (``MAX_DEPTH`` niveaux) confinées à ``components_dir``."""
    root = components_dir.resolve()
    base_params = dict(params or {})

    def substitute(text: str, scope: dict[str, str]) -> str:
        return PLACEHOLDER_RE.sub(lambda m: html.escape(scope.get(m.group(1), ""), quote=True), text)

    def expand(text: str, scope: dict[str, str], depth: int) -> str:
        def replacer(match: re.Match) -> str:
            rel = match.group(1).strip("\"' ")
            target = (root / rel).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return f"<!-- include introuvable : {html.escape(rel)} -->"
            local = {**scope, **dict(PARAM_RE.findall(match.group(2) or ""))}
            fragment = target.read_text(encoding="utf-8")
            if depth < MAX_DEPTH:
                fragment = expand(fragment, local, depth + 1)
            return substitute(fragment, local)

        return INCLUDE_RE.sub(replacer, text)

    return substitute(expand(html_text, base_params, 1), base_params)
