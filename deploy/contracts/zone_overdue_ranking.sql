-- Integration query contract, NOT a migration: the DBA must supply this view.
-- Required view: BURO_AUTHORIZED_LOAN_SNAPSHOT
-- One row per unique loan/report_date/principal (no duplicate balance snapshots).
-- Columns: principal_id, report_date, zone_code, zone_name, branch_code,
-- branch_name, member_id (VARCHAR2), due_amount, outstanding_principal, due_days.
-- principal_id MUST come from authenticated server context, never an LLM argument.
-- Resolve zone name uniquely to :zone_code before running; ambiguous => clarification.
-- Confirm DUEAMOUNT means overdue amount, currency is consistent and additive,
-- and outstanding is one balance per loan. Fail on null/invalid required values.
WITH member_totals AS (
    SELECT zone_code, zone_name, branch_code, branch_name, member_id,
           SUM(due_amount) AS due_amount,
           SUM(outstanding_principal) AS outstanding_principal,
           MAX(due_days) AS due_days
    FROM BURO_AUTHORIZED_LOAN_SNAPSHOT
    WHERE principal_id = :authenticated_principal_id
      AND report_date = TO_DATE(:report_date, 'FXYYYY-MM-DD')
      AND zone_code = :zone_code
    GROUP BY zone_code, zone_name, branch_code, branch_name, member_id
    HAVING SUM(due_amount) > 0
), ranked AS (
    SELECT member_totals.*,
           ROW_NUMBER() OVER (ORDER BY due_amount DESC,
               NLSSORT(branch_code, 'NLS_SORT=BINARY'),
               NLSSORT(member_id, 'NLS_SORT=BINARY')) AS rank_no,
           COUNT(*) OVER () AS eligible_member_count
    FROM member_totals
)
SELECT * FROM ranked WHERE rank_no <= :top_limit ORDER BY rank_no;
-- Return eligibleMemberCount even for zero rows (0). Serialize amounts as
-- exact decimal strings and identifiers as strings; do not coerce IDs to numbers.
