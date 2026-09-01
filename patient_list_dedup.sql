-- Warehouse (Spark/Databricks SQL) port of patient_list_dedup.py.
-- Flags likely-duplicate patients per clinic and recommends an action per row.
-- See README.md ("SQL query variant") for the output-column reference.
--
-- :clinic accepts a clinic id, an exact name, or a partial name.
--   * Selected: filters to the matched clinic(s) and populates the dup columns.
-- Fuzzy name threshold = 0.90. DOB is used to tier match confidence:
--   strong = agrees on >=2 of {name, DOB, MRN}  -> Maintain/Remove
--   weak   = name-only or MRN-only (DOB differs) -> Review, plus a conditional
--            keep/remove verdict ("if confirmed") ranked over the whole cluster:
--            claimed first, then latest data date.

WITH
selected_clinics AS (
  SELECT c.id AS clinicId
  FROM prod.models.clinics c
  WHERE NULLIF(TRIM(:clinic), '') IS NOT NULL
    AND (
         CAST(c.id AS STRING) = TRIM(:clinic)
      OR LOWER(c.name) = LOWER(TRIM(:clinic))
      OR LOWER(c.name) LIKE CONCAT('%', LOWER(TRIM(:clinic)), '%')
    )
),

base AS (
  SELECT
    p.userId,
    p.fullName,
    (p.email IS NOT NULL)            AS claimed,
    p.birthDate,
    p.mrn,
    p.clinicId,
    p.dataSources_providerName       AS connection_provider,
    p.dataSources_state              AS connection_state,
    -- Blank and null-like placeholders ('null', 'NaN', ...) -> NULL so they
    -- never match each other as if they were real data.
    CASE WHEN LOWER(TRIM(p.fullName)) IN ('', 'null', 'nan', 'none', 'n/a', 'na', 'nil')
         THEN NULL ELSE LOWER(TRIM(p.fullName)) END AS norm_name,
    CASE WHEN LOWER(TRIM(p.mrn)) IN ('', 'null', 'nan', 'none', 'n/a', 'na', 'nil')
         THEN NULL ELSE LOWER(TRIM(p.mrn)) END AS norm_mrn,
    -- Normalized DOB for equality tests (blank/null-like -> NULL, never agrees).
    CASE WHEN LOWER(TRIM(CAST(p.birthDate AS STRING))) IN ('', 'null', 'nan', 'none', 'n/a', 'na', 'nil')
         THEN NULL ELSE LOWER(TRIM(CAST(p.birthDate AS STRING))) END AS norm_dob,
    GREATEST(
      TO_DATE(CASE
        WHEN get_json_object(get_json_object(p.summary,'$.cgmStats.dates.lastData'),'$.$date') IS NOT NULL
             AND get_json_object(get_json_object(p.summary,'$.cgmStats.dates.lastData'),'$.$date') NOT LIKE '{"$numberLong":%'
          THEN try_cast(get_json_object(get_json_object(p.summary,'$.cgmStats.dates.lastData'),'$.$date') AS TIMESTAMP)
        WHEN get_json_object(get_json_object(p.summary,'$.cgmStats.dates.lastData'),'$.$date.$numberLong') IS NOT NULL
          THEN from_unixtime(try_cast(get_json_object(get_json_object(p.summary,'$.cgmStats.dates.lastData'),'$.$date.$numberLong') AS BIGINT)/1000)
        ELSE NULL END),
      TO_DATE(CASE
        WHEN get_json_object(get_json_object(p.summary,'$.bgmStats.dates.lastData'),'$.$date') IS NOT NULL
             AND get_json_object(get_json_object(p.summary,'$.bgmStats.dates.lastData'),'$.$date') NOT LIKE '{"$numberLong":%'
          THEN try_cast(get_json_object(get_json_object(p.summary,'$.bgmStats.dates.lastData'),'$.$date') AS TIMESTAMP)
        WHEN get_json_object(get_json_object(p.summary,'$.bgmStats.dates.lastData'),'$.$date.$numberLong') IS NOT NULL
          THEN from_unixtime(try_cast(get_json_object(get_json_object(p.summary,'$.bgmStats.dates.lastData'),'$.$date.$numberLong') AS BIGINT)/1000)
        ELSE NULL END)
    ) AS last_data_date
  FROM prod.default.patient_with_summary AS p
  WHERE p.is_active = 'Y'
    AND NULLIF(TRIM(:clinic), '') IS NOT NULL
    AND p.clinicId IN (SELECT clinicId FROM selected_clinics)
),

-- Raw pairwise agreement flags (name / MRN / DOB) within a clinic.
-- Every flag is guarded on BOTH sides so it is always TRUE/FALSE, never NULL:
-- 'x = NULL' yields NULL, and a NULL flag silently zeroes has_weak_* downstream.
-- Fuzzy similarity = 2*(max_len - levenshtein)/(len_a + len_b), which tracks
-- Python difflib's SequenceMatcher.ratio() at the same 0.90 threshold.
edges AS (
  SELECT * FROM (
    SELECT
      a.userId AS src,
      b.userId AS dst,
      (a.norm_mrn IS NOT NULL AND b.norm_mrn IS NOT NULL
         AND a.norm_mrn = b.norm_mrn)                          AS mrn_match,
      (a.norm_name IS NOT NULL AND b.norm_name IS NOT NULL
         AND a.norm_name = b.norm_name)                        AS exact_name_match,
      COALESCE(
        a.norm_name IS NOT NULL AND b.norm_name IS NOT NULL
          AND a.norm_name <> b.norm_name
          AND (2.0 * (greatest(length(a.norm_name), length(b.norm_name))
                      - levenshtein(a.norm_name, b.norm_name))
               / (length(a.norm_name) + length(b.norm_name))) >= 0.90,
        FALSE)                                                 AS fuzzy_name_match,
      (a.norm_dob IS NOT NULL AND b.norm_dob IS NOT NULL
         AND a.norm_dob = b.norm_dob)                          AS dob_match
    FROM base a
    JOIN base b
      ON a.clinicId = b.clinicId
     AND a.userId <> b.userId
  )
  WHERE mrn_match OR exact_name_match OR fuzzy_name_match
),

-- Classify each edge: name_match, and strong (>=2 of name/DOB/MRN agree).
edges_cls AS (
  SELECT
    src, dst, mrn_match, exact_name_match, fuzzy_name_match, dob_match,
    (exact_name_match OR fuzzy_name_match) AS name_match,
    (CAST(exact_name_match OR fuzzy_name_match AS INT)
       + CAST(mrn_match AS INT)
       + CAST(dob_match AS INT)) >= 2 AS is_strong
  FROM edges
),

edges_strong AS (
  SELECT src, dst FROM edges_cls WHERE is_strong
),

match_summary AS (
  SELECT src AS userId,
         MAX(CAST(mrn_match        AS INT)) AS has_mrn_match,
         MAX(CAST(exact_name_match AS INT)) AS has_exact_name,
         MAX(CAST(fuzzy_name_match AS INT)) AS has_fuzzy_name,
         MAX(CASE WHEN name_match AND NOT is_strong THEN 1 ELSE 0 END) AS has_weak_name,
         MAX(CASE WHEN mrn_match  AND NOT is_strong THEN 1 ELSE 0 END) AS has_weak_mrn
  FROM edges_cls
  GROUP BY src
),

-- Review clusters via ALL edges (min-of-neighbors label propagation).
lp1 AS (
  SELECT b.userId, LEAST(b.userId, COALESCE(MIN(nb.userId), b.userId)) AS label
  FROM base b
  LEFT JOIN edges_cls e ON e.src = b.userId
  LEFT JOIN base      nb ON nb.userId = e.dst
  GROUP BY b.userId
),
lp2 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM lp1 p LEFT JOIN edges_cls e ON e.src = p.userId LEFT JOIN lp1 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
lp3 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM lp2 p LEFT JOIN edges_cls e ON e.src = p.userId LEFT JOIN lp2 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
lp4 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM lp3 p LEFT JOIN edges_cls e ON e.src = p.userId LEFT JOIN lp3 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
lp5 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM lp4 p LEFT JOIN edges_cls e ON e.src = p.userId LEFT JOIN lp4 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
components_all AS ( SELECT userId, label AS grp FROM lp5 ),

-- Merge clusters via STRONG edges only.
sp1 AS (
  SELECT b.userId, LEAST(b.userId, COALESCE(MIN(nb.userId), b.userId)) AS label
  FROM base b
  LEFT JOIN edges_strong e ON e.src = b.userId
  LEFT JOIN base         nb ON nb.userId = e.dst
  GROUP BY b.userId
),
sp2 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM sp1 p LEFT JOIN edges_strong e ON e.src = p.userId LEFT JOIN sp1 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
sp3 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM sp2 p LEFT JOIN edges_strong e ON e.src = p.userId LEFT JOIN sp2 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
sp4 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM sp3 p LEFT JOIN edges_strong e ON e.src = p.userId LEFT JOIN sp3 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
sp5 AS (
  SELECT p.userId, LEAST(p.label, COALESCE(MIN(q.label), p.label)) AS label
  FROM sp4 p LEFT JOIN edges_strong e ON e.src = p.userId LEFT JOIN sp4 q ON q.userId = e.dst
  GROUP BY p.userId, p.label
),
components_strong AS ( SELECT userId, label AS grp FROM sp5 ),

enriched AS (
  SELECT
    b.*,
    ca.grp AS grp_all,
    cs.grp AS grp_strong,
    COALESCE(ms.has_mrn_match, 0)  AS has_mrn_match,
    COALESCE(ms.has_exact_name, 0) AS has_exact_name,
    COALESCE(ms.has_fuzzy_name, 0) AS has_fuzzy_name,
    COALESCE(ms.has_weak_name, 0)  AS has_weak_name,
    COALESCE(ms.has_weak_mrn, 0)   AS has_weak_mrn
  FROM base b
  JOIN components_all    ca ON ca.userId = b.userId
  JOIN components_strong cs ON cs.userId = b.userId
  LEFT JOIN match_summary ms ON ms.userId = b.userId
),

ranked AS (
  SELECT *,
    COUNT(*) OVER (PARTITION BY grp_all)    AS all_group_size,
    COUNT(*) OVER (PARTITION BY grp_strong) AS strong_group_size,
    MAX(CASE WHEN claimed OR last_data_date IS NOT NULL THEN 1 ELSE 0 END)
        OVER (PARTITION BY grp_strong) AS strong_has_signal,
    -- Signal + keeper ranking over the WHOLE review cluster, for the
    -- conditional "if confirmed" verdict on weak matches. Same rules:
    -- claimed always wins, then has-data, then latest data date.
    MAX(CASE WHEN claimed OR last_data_date IS NOT NULL THEN 1 ELSE 0 END)
        OVER (PARTITION BY grp_all) AS all_has_signal,
    ROW_NUMBER() OVER (
      PARTITION BY grp_strong
      ORDER BY claimed DESC,
               (last_data_date IS NOT NULL) DESC,
               last_data_date DESC NULLS LAST,
               userId ASC
    ) AS rn_strong,
    ROW_NUMBER() OVER (
      PARTITION BY grp_all
      ORDER BY claimed DESC,
               (last_data_date IS NOT NULL) DESC,
               last_data_date DESC NULLS LAST,
               userId ASC
    ) AS rn_all
  FROM enriched
),

-- Stable cluster numbers: 1..N over review clusters (ordered by cluster
-- label, i.e. lowest member userId), NULL for non-duplicates. Duplicate
-- clusters sort first so their dense ranks start at 1.
numbered AS (
  SELECT *,
    CASE WHEN all_group_size > 1
         THEN DENSE_RANK() OVER (ORDER BY (all_group_size > 1) DESC, grp_all)
    END AS cluster_number
  FROM ranked
)

SELECT
  r.fullName,
  r.claimed,
  r.userId,
  r.birthDate,
  r.mrn,
  r.connection_provider AS `Connection Provider`,
  r.connection_state    AS `Connection State`,
  r.last_data_date      AS lastDataDate,

  -- Duplicate columns are NULL unless a clinic is selected.
  CASE WHEN NULLIF(TRIM(:clinic), '') IS NULL THEN NULL
       ELSE (r.all_group_size > 1) END AS is_duplicate_match,

  CASE WHEN NULLIF(TRIM(:clinic), '') IS NULL THEN NULL
       ELSE r.cluster_number END AS duplicate_cluster,

  CASE WHEN NULLIF(TRIM(:clinic), '') IS NULL THEN NULL
       WHEN r.all_group_size > 1 THEN
         concat_ws('; ',
           CASE WHEN r.has_exact_name = 1 THEN 'Exact name match'
                WHEN r.has_fuzzy_name = 1 THEN 'Fuzzy name match' END,
           CASE WHEN r.has_mrn_match  = 1 THEN 'Shared MRN' END)
  END AS duplicate_reason,

  CASE
    WHEN NULLIF(TRIM(:clinic), '') IS NULL THEN NULL
    WHEN r.all_group_size < 2 THEN NULL                       -- not a duplicate
    WHEN r.strong_group_size > 1 THEN                         -- high-confidence
      CASE WHEN r.strong_has_signal = 0 THEN NULL             -- dup, no signal
           WHEN r.rn_strong = 1 THEN 'Maintain this account'
           ELSE 'Remove this account' END
    ELSE                                                      -- weak-only -> review
      concat_ws('; ',
        CASE WHEN r.has_weak_name = 1 THEN 'Review - possible duplicate (name match, DOB differs)' END,
        CASE WHEN r.has_weak_mrn  = 1 THEN 'Review - possible MRN typo (MRN match, DOB differs)' END,
        CASE WHEN r.all_has_signal = 1 THEN
               CASE WHEN r.rn_all = 1 THEN 'If confirmed duplicate: maintain this account'
                    ELSE 'If confirmed duplicate: remove this account' END
        END)
  END AS recommended_action,

  cl.name AS clinic_name,
  cl.id   AS clinic_id

FROM numbered r
LEFT JOIN prod.models.clinics cl ON cl.id = r.clinicId
