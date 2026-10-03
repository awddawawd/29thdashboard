-- =============================================================================
-- Migration 001 · Cockpit WaaS (port de l'ancien dashboard)
-- -----------------------------------------------------------------------------
-- À exécuter une fois dans Supabase : SQL Editor → coller ce fichier → Run.
-- Idempotente : peut être rejouée sans effet de bord.
--
-- Tables ajoutées
--   sites                fiche commerciale d'une vitrine (formule, domaine, contrat)
--   invoices             factures & abonnements
--   tickets              conversations support (1 ticket = 1 fil)
--   ticket_messages      messages d'un ticket
--   uptime_daily         disponibilité agrégée par service et par jour (SLA 90 j)
--   host_metrics_hourly  charge de l'hôte agrégée par heure (historique 7 / 30 j)
--
-- Fonctions ajoutées (appelées par l'API avec la clé serveur uniquement)
--   create_waas_user      création d'un compte (mot de passe haché bcrypt / pgcrypto)
--   set_waas_user_password
--
-- Sécurité : RLS activée sans aucune politique → seules les requêtes signées
-- avec la clé secrète (API) y accèdent ; la clé publiable n'y lit rien.
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- 1. Sites (fiche commerciale). L'identifiant est celui du conteneur waas-<id>.
-- -----------------------------------------------------------------------------
create table if not exists public.sites (
    id                 text primary key check (id ~ '^[a-z0-9-]+$'),
    client_id          uuid references public.waas_users (id) on delete set null,
    name               text not null default '',
    plan_id            text not null default 'none',
    plan_name          text not null default 'Sans formule',
    price_monthly      numeric(10, 2) not null default 0 check (price_monthly >= 0),
    domain             text,
    domain_registrar   text,
    domain_expires_at  date,
    ssl_expires_at     date,
    contract_since     date not null default current_date,
    contract_status    text not null default 'active'
                       check (contract_status in ('active', 'paused', 'cancelled')),
    repo_url           text,
    notes              text not null default '',
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);
create index if not exists sites_client_id_idx on public.sites (client_id);

-- -----------------------------------------------------------------------------
-- 2. Factures
-- -----------------------------------------------------------------------------
create table if not exists public.invoices (
    id           uuid primary key default gen_random_uuid(),
    number       text not null unique,
    site_id      text,
    client_id    uuid references public.waas_users (id) on delete set null,
    client_name  text not null default '',
    description  text not null default 'Abonnement vitrine & hébergement',
    amount       numeric(10, 2) not null default 0 check (amount >= 0),
    currency     text not null default 'EUR',
    status       text not null default 'pending'
                 check (status in ('draft', 'pending', 'paid', 'overdue')),
    issued_at    date not null default current_date,
    due_at       date,
    paid_at      timestamptz,
    items        jsonb not null default '[]'::jsonb,
    created_at   timestamptz not null default now()
);
create index if not exists invoices_site_id_idx on public.invoices (site_id);
create index if not exists invoices_status_idx on public.invoices (status);

-- -----------------------------------------------------------------------------
-- 3. Support : tickets et messages
-- -----------------------------------------------------------------------------
create table if not exists public.tickets (
    id          uuid primary key default gen_random_uuid(),
    site_id     text not null,
    subject     text not null check (char_length(subject) between 1 and 200),
    status      text not null default 'open' check (status in ('open', 'waiting', 'closed')),
    priority    text not null default 'normal' check (priority in ('low', 'normal', 'urgent')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists tickets_site_id_idx on public.tickets (site_id);
create index if not exists tickets_status_idx on public.tickets (status, updated_at desc);

create table if not exists public.ticket_messages (
    id           uuid primary key default gen_random_uuid(),
    ticket_id    uuid not null references public.tickets (id) on delete cascade,
    from_role    text not null check (from_role in ('agency', 'client')),
    author_name  text not null default '',
    body         text not null check (char_length(body) between 1 and 5000),
    sent_at      timestamptz not null default now(),
    seen_at      timestamptz
);
create index if not exists ticket_messages_ticket_idx on public.ticket_messages (ticket_id, sent_at);

-- -----------------------------------------------------------------------------
-- 4. Supervision : disponibilité quotidienne et charge horaire de l'hôte
-- -----------------------------------------------------------------------------
create table if not exists public.uptime_daily (
    service_key      text not null,
    day              date not null,
    label            text not null default '',
    kind             text not null default 'http',
    critical         boolean not null default false,
    checks           integer not null default 0,
    up_count         integer not null default 0,
    degraded_count   integer not null default 0,
    down_count       integer not null default 0,
    latency_sum_ms   double precision not null default 0,
    latency_samples  integer not null default 0,
    last_state       text not null default 'up',
    last_code        text not null default '',
    last_detail      text not null default '',
    last_checked_at  timestamptz,
    primary key (service_key, day)
);
create index if not exists uptime_daily_day_idx on public.uptime_daily (day);

create table if not exists public.host_metrics_hourly (
    hour           timestamptz primary key,
    samples        integer not null default 0,
    cpu_sum        double precision not null default 0,
    ram_sum        double precision not null default 0,
    disk_io_sum    double precision not null default 0,
    net_io_sum     double precision not null default 0,
    fleet_mem_sum  double precision not null default 0
);

-- -----------------------------------------------------------------------------
-- 5. Comptes : création et changement de mot de passe (bcrypt, comme l'existant)
-- -----------------------------------------------------------------------------
create or replace function public.create_waas_user(
    p_username  text,
    p_password  text,
    p_role      text default 'client',
    p_tenant_id text default null
)
returns table (id uuid, username text, role text, tenant_id text, created_at timestamptz)
language plpgsql
security definer
set search_path = public, extensions
as $$
begin
    if coalesce(char_length(p_password), 0) < 10 then
        raise exception 'Mot de passe trop court (10 caractères minimum).';
    end if;
    if p_role not in ('client', 'admin') then
        raise exception 'Rôle invalide : %', p_role;
    end if;
    return query
        insert into public.waas_users (username, password_hash, role, tenant_id)
        values (lower(trim(p_username)), crypt(p_password, gen_salt('bf', 10)), p_role, nullif(trim(p_tenant_id), ''))
        returning waas_users.id, waas_users.username, waas_users.role, waas_users.tenant_id, waas_users.created_at;
end;
$$;

create or replace function public.set_waas_user_password(p_user_id uuid, p_password text)
returns boolean
language plpgsql
security definer
set search_path = public, extensions
as $$
begin
    if coalesce(char_length(p_password), 0) < 10 then
        raise exception 'Mot de passe trop court (10 caractères minimum).';
    end if;
    update public.waas_users
       set password_hash = crypt(p_password, gen_salt('bf', 10))
     where waas_users.id = p_user_id;
    return found;
end;
$$;

-- Ces fonctions ne doivent jamais être appelables avec la clé publiable.
revoke all on function public.create_waas_user(text, text, text, text) from public, anon, authenticated;
revoke all on function public.set_waas_user_password(uuid, text) from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 6. Verrouillage : RLS sans politique (accès réservé à la clé secrète de l'API)
-- -----------------------------------------------------------------------------
alter table public.sites               enable row level security;
alter table public.invoices            enable row level security;
alter table public.tickets             enable row level security;
alter table public.ticket_messages     enable row level security;
alter table public.uptime_daily        enable row level security;
alter table public.host_metrics_hourly enable row level security;

-- Recharge le cache de schéma PostgREST (sinon les nouvelles tables restent invisibles).
notify pgrst, 'reload schema';
