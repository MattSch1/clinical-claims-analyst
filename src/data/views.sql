-- ============================================================================
-- views.sql  —  De-identified views over the Synthea base tables.
--
-- The agent's read-only role is granted SELECT on these v_* views ONLY, never on
-- the base tables. Identifier columns (names, SSN, address, lat/lon, full DOB,
-- full ZIP) are dropped. Quasi-identifiers are transformed:
--   * age      -> banded and CAPPED at 90 (Safe Harbor: ages >89 aggregated)
--   * dates    -> per-patient DATE-SHIFTED (preserves within-patient intervals
--                 for readmission / length-of-stay while removing absolute dates;
--                 the MIMIC-style technique) and reduced to YEAR for trends
--   * ZIP      -> 3-digit prefix only
--   * geography-> state only
--
-- DATA IS SYNTHETIC (Synthea). This is architected *as if* it were PHI on purpose.
--
-- Assumptions about the load (see src/data/load.py):
--   * table & column names are lowercased Synthea CSV headers
--   * date/datetime columns castable via ::timestamptz / ::date
--   * code columns (snomed/loinc/rxnorm/cvx) loaded as TEXT
--
-- IMPORTANT: Synthea's schema varies slightly by version. If a column below is
-- missing in your generated CSVs (e.g. `income`, `system`, `category`), drop it
-- from the relevant view. Verify against your actual headers before relying on it.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Per-patient date offset: a consistent random shift (±5 years) per patient.
-- Same offset is applied everywhere that patient's dates appear, so intervals
-- BETWEEN that patient's events are preserved while absolute dates are obscured.
-- ---------------------------------------------------------------------------
SELECT setseed(0.42);  -- reproducible within a session; document this choice

DROP TABLE IF EXISTS patient_date_offset;
CREATE TABLE patient_date_offset AS
SELECT id AS patient,
       (floor(random() * 3650) - 1825)::int AS offset_days   -- ~[-5y, +5y]
FROM patients;
CREATE INDEX ix_pdo_patient ON patient_date_offset (patient);

-- Helper note: shifted ts = base::timestamptz + make_interval(days => offset_days)

-- ---------------------------------------------------------------------------
-- v_patients  (no DOB, no names, no street/lat/lon; age capped+banded)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_patients CASCADE;
CREATE VIEW v_patients AS
SELECT
    p.id,
    LEAST(
        floor(extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)))::int,
        90
    ) AS age,                              -- capped at 90
    CASE
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 18 THEN '0-17'
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 35 THEN '18-34'
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 50 THEN '35-49'
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 65 THEN '50-64'
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 75 THEN '65-74'
        WHEN extract(year from age(coalesce(p.deathdate::date, current_date), p.birthdate::date)) < 90 THEN '75-89'
        ELSE '90+'
    END AS age_band,
    p.gender,
    p.race,
    p.ethnicity,
    p.marital,
    p.state,                                -- state permitted under Safe Harbor
    left(p.zip, 3) AS zip3,                 -- 3-digit ZIP prefix only
    (p.deathdate IS NOT NULL) AS deceased,
    p.healthcare_expenses,
    p.healthcare_coverage
FROM patients p;

-- ---------------------------------------------------------------------------
-- v_encounters  (date-shifted timestamps + year; cost & class retained)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_encounters CASCADE;
CREATE VIEW v_encounters AS
SELECT
    e.id,
    e.patient,
    (e.start::timestamptz + make_interval(days => o.offset_days)) AS start_shifted,
    (e.stop::timestamptz  + make_interval(days => o.offset_days)) AS stop_shifted,
    extract(year from (e.start::timestamptz + make_interval(days => o.offset_days)))::int AS encounter_year,
    round((extract(epoch from (e.stop::timestamptz - e.start::timestamptz)) / 86400.0)::numeric, 2) AS los_days,
    e.encounterclass,
    e.code,
    e.description,
    e.base_encounter_cost,
    e.total_claim_cost,
    e.payer_coverage,
    e.payer
FROM encounters e
JOIN patient_date_offset o ON e.patient = o.patient;

-- ---------------------------------------------------------------------------
-- v_conditions / v_procedures / v_medications / v_observations / v_immunizations
-- Dates reduced to (shifted) YEAR; clinical codes & descriptions retained.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_conditions CASCADE;
CREATE VIEW v_conditions AS
SELECT
    c.patient,
    c.encounter,
    extract(year from (c.start::timestamptz + make_interval(days => o.offset_days)))::int AS onset_year,
    c.code,
    c.description
FROM conditions c
JOIN patient_date_offset o ON c.patient = o.patient;

DROP VIEW IF EXISTS v_procedures CASCADE;
CREATE VIEW v_procedures AS
SELECT
    pr.patient,
    pr.encounter,
    extract(year from (pr.start::timestamptz + make_interval(days => o.offset_days)))::int AS procedure_year,
    pr.code,
    pr.description,
    pr.base_cost
FROM procedures pr
JOIN patient_date_offset o ON pr.patient = o.patient;

DROP VIEW IF EXISTS v_medications CASCADE;
CREATE VIEW v_medications AS
SELECT
    m.patient,
    m.encounter,
    m.payer,
    extract(year from (m.start::timestamptz + make_interval(days => o.offset_days)))::int AS start_year,
    m.code,
    m.description,
    m.dispenses,
    m.totalcost,
    m.payer_coverage
FROM medications m
JOIN patient_date_offset o ON m.patient = o.patient;

DROP VIEW IF EXISTS v_observations CASCADE;
CREATE VIEW v_observations AS
SELECT
    ob.patient,
    ob.encounter,
    extract(year from (ob.date::timestamptz + make_interval(days => o.offset_days)))::int AS obs_year,
    ob.code,
    ob.description,
    ob.value,
    ob.units,
    ob.type
FROM observations ob
JOIN patient_date_offset o ON ob.patient = o.patient;

DROP VIEW IF EXISTS v_immunizations CASCADE;
CREATE VIEW v_immunizations AS
SELECT
    im.patient,
    im.encounter,
    extract(year from (im.date::timestamptz + make_interval(days => o.offset_days)))::int AS imm_year,
    im.code,
    im.description
FROM immunizations im
JOIN patient_date_offset o ON im.patient = o.patient;

-- ---------------------------------------------------------------------------
-- v_payers  (organization-level; no patient PHI)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_payers CASCADE;
CREATE VIEW v_payers AS
SELECT
    pa.id,
    pa.name,
    pa.member_months
FROM payers pa;

-- ============================================================================
-- Least-privilege grant. Run once as a superuser/owner. The agent connects as
-- analyst_ro and can read ONLY the views below — never the base tables, never
-- patient_date_offset (which would let intervals be reversed).
-- ============================================================================
-- CREATE ROLE analyst_ro LOGIN PASSWORD '<set-in-secret>';
-- REVOKE ALL ON ALL TABLES IN SCHEMA public FROM analyst_ro;
-- GRANT USAGE ON SCHEMA public TO analyst_ro;
-- GRANT SELECT ON v_patients, v_encounters, v_conditions, v_procedures,
--                v_medications, v_observations, v_immunizations, v_payers
--          TO analyst_ro;
-- -- also set a per-role statement timeout as defense-in-depth:
-- ALTER ROLE analyst_ro SET statement_timeout = '10s';
