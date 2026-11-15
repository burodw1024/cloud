# Savings reports with User Access permissions

Integration settings supports UI/APP access for savings schemas with an exact
string `branch_code` filter, `zone_code`, and required `operation`, `from_date`
and `to_date_exclusive`. Supported operations are period-summary, branch-summary,
zone-summary, zone-branches, branch-product-summary and zone-product-summary.
This contract is separate from the loan report_date contract.

After deployment, Find tools and Check model again, choose User Access
permissions for savings-report, clear any legacy fixed branch list, then save.
Grant the employee this tool and the permitted branches/zones in User access.
Publish the savings business module. Existing group, approval, expiry and paused
module checks remain applicable; explicit user tool selections replace groups.

Each savings call is restricted to one permitted branch. If the user has access
to multiple branches, the question must select one. A policy resolving to one
branch can supply that exact filter automatically. A zone alone cannot authorize
an unrestricted zone report. Cross-branch aggregation is not implemented.
The backend must apply branch_code before grouping, including zone operations.
Ranges must be ISO calendar dates, inclusive start and exclusive end, spanning
1 to 366 days. No loan fields or authorization token are added.

## Backend response contract

The app requires a successful JSON envelope with a data array and explicit scope
evidence, either through MCP structuredContent or a single JSON text result:

```json
{
  "success": true,
  "scope": {
    "branch_code": "0253",
    "zone_code": null,
    "authorized_branch_count": 1,
    "from_date": "2026-10-01",
    "to_date_exclusive": "2026-11-01"
  },
  "data": [{"branch_code": "0253", "deposits": "123.45"}]
}
```

Scope must describe the filter actually applied in SQL, not just echo unchecked
request values. If zone_code was supplied, the response must confirm it too.
Explicit branch identifiers anywhere in the result must be within the grant.
Access is rechecked after the response and before history replay. Missing or
inconsistent scope blocks display. The schema supplied for this change confirms
the input contract only; the live savings response has not yet been inspected.
If the live result differs, inspect a redacted or synthetic response and adapt
the validator to equivalent evidence, or add this evidence to the backend.
Do not remove validation or fabricate scope around an unfiltered report.
