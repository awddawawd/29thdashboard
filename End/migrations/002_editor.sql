-- =============================================================================
-- Migration 002 · Éditeur visuel v2 (Studio)
-- -----------------------------------------------------------------------------
-- À exécuter une fois dans Supabase : SQL Editor → coller ce fichier → Run.
-- Idempotente : peut être rejouée sans effet de bord.
--
-- Facultative : sans elle, l'éditeur fonctionne comme le moteur historique
-- (un élément = une clé XPath), sans relocalisation ni audit. Avec elle :
--
--   source_value  texte d'origine de l'élément dans le gabarit (ancre). Quand le
--                 gabarit évolue (section ajoutée, paragraphes réordonnés), le
--                 contenu du client suit son élément au lieu de s'afficher
--                 ailleurs ; une retouche du gabarit (coquille corrigée) ne
--                 remplace jamais un texte personnalisé. Renseignée
--                 automatiquement au prochain démarrage de chaque vitrine.
--   updated_at    date de la dernière modification faite dans le Studio.
--   updated_by    compte qui l'a faite (« Modifié par … » dans le Studio).
--
-- Les éléments dont la clé commence par « orphan: » ne correspondent plus à la
-- page (élément retiré du gabarit) : ils restent consultables et supprimables
-- depuis le Studio (« Contenus détachés »).
-- =============================================================================

alter table public.tenant_content add column if not exists source_value text;
alter table public.tenant_content add column if not exists updated_at   timestamptz;
alter table public.tenant_content add column if not exists updated_by   text;

-- Aucun index à ajouter : la contrainte d'unicité (tenant_id, file_path, xpath),
-- déjà requise par les écritures des conteneurs, sert aussi les lectures page
-- par page de la réconciliation.

-- Prise en compte immédiate des nouvelles colonnes par l'API PostgREST.
notify pgrst, 'reload schema';
