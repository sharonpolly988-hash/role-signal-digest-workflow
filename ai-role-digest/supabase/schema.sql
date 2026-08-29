create table if not exists public.seen_posts (
  post_id text primary key,
  url text,
  seen_at timestamptz default now()
);

create table if not exists public.role_feedback (
  id uuid primary key default gen_random_uuid(),
  post_id text not null,
  post_url text,
  title text,
  feedback_type text not null check (feedback_type in ('good', 'not_good')),
  note text,
  feedback_category text check (
    feedback_category in (
      'too_senior',
      'wrong_location',
      'not_hiring_post',
      'expired_post',
      'not_relevant_domain',
      'duplicate'
    )
  ),
  positive_signal_category text check (
    positive_signal_category in (
      'strong_role_match',
      'strong_domain_match',
      'strong_ai_agent_match',
      'strong_workflow_automation_match',
      'strong_gtm_ai_match',
      'strong_location_match',
      'acceptable_seniority',
      'other'
    )
  ),
  extracted_title_keywords jsonb not null default '[]'::jsonb,
  extracted_location_terms jsonb not null default '[]'::jsonb,
  extracted_domain_terms jsonb not null default '[]'::jsonb,
  extracted_seniority_terms jsonb not null default '[]'::jsonb,
  created_at timestamptz default now()
);

create index if not exists role_feedback_post_id_idx
  on public.role_feedback (post_id);

create unique index if not exists role_feedback_one_good_per_post_idx
  on public.role_feedback (post_id)
  where feedback_type = 'good';

create unique index if not exists role_feedback_one_not_good_per_post_idx
  on public.role_feedback (post_id)
  where feedback_type = 'not_good';

alter table public.role_feedback
  add column if not exists feedback_category text,
  add column if not exists positive_signal_category text,
  add column if not exists extracted_title_keywords jsonb not null default '[]'::jsonb,
  add column if not exists extracted_location_terms jsonb not null default '[]'::jsonb,
  add column if not exists extracted_domain_terms jsonb not null default '[]'::jsonb,
  add column if not exists extracted_seniority_terms jsonb not null default '[]'::jsonb;

alter table public.role_feedback
  drop constraint if exists role_feedback_feedback_category_check;

alter table public.role_feedback
  add constraint role_feedback_feedback_category_check
  check (
    feedback_category in (
      'too_senior',
      'wrong_location',
      'not_hiring_post',
      'expired_post',
      'not_relevant_domain',
      'duplicate'
    )
  );

create index if not exists role_feedback_created_at_idx
  on public.role_feedback (created_at desc);

create index if not exists role_feedback_feedback_category_idx
  on public.role_feedback (feedback_category);

create index if not exists role_feedback_positive_signal_category_idx
  on public.role_feedback (positive_signal_category);

create table if not exists public.feedback_filter_config (
  key text primary key default 'active',
  config jsonb not null default '{}'::jsonb,
  updated_at timestamptz default now()
);

insert into public.feedback_filter_config (key, config)
values (
  'active',
  '{
    "allowed_locations": ["New York", "NYC"],
    "blocked_locations": [],
    "blocked_seniority_keywords": ["Director", "VP", "Vice President", "Chief Accounting Officer"],
    "max_years_experience": 6,
    "require_hiring_signal": true,

    "positive_title_boost_keywords": [
      "Financial Analyst",
      "Senior Financial Analyst",
      "Staff Accountant",
      "Senior Accountant",
      "Financial Reporting Accountant",
      "Corporate Accountant",
      "Accounting Analyst"
    ],

    "positive_domain_boost_keywords": [
      "accounting",
      "financial reporting",
      "financial analysis",
      "GAAP",
      "general ledger",
      "month-end close",
      "variance analysis",
      "FP&A"
    ],

    "positive_workflow_boost_keywords": [
      "month-end close",
      "account reconciliation",
      "financial reporting",
      "budgeting",
      "forecasting",
      "variance analysis"
    ],

    "positive_agent_boost_keywords": [],

    "positive_location_boost_terms": [
      "New York",
      "NYC"
    ],

    "acceptable_seniority_keywords": [
      "Staff Accountant",
      "Senior Accountant",
      "Financial Analyst",
      "Senior Financial Analyst",
      "Associate",
      "II"
    ]
  }'::jsonb
)
on conflict (key)
do update set
  config = excluded.config,
  updated_at = now();

create table if not exists public.apify_query_performance (
  query text primary key,
  posts_returned integer not null default 0,
  unique_posts integer not null default 0,
  valid_hiring_signals integer not null default 0,
  high_fit_signals integer not null default 0,
  duplicate_rate double precision not null default 0,
  last_run_at timestamptz,
  updated_at timestamptz default now()
);

create index if not exists apify_query_performance_last_run_at_idx
  on public.apify_query_performance (last_run_at desc);
