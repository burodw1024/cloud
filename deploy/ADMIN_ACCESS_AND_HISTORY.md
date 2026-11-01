# Admin access and question history

## Review user attempts

Visit `/agentic/dashboard/` (User attempts in the assistant navigation).
Reviewers need an active account, Staff status and
`agentic_ai.view_agentrunlog`. This permits review of all users' questions and
stored answers/errors, including failures, blocked requests, new unsupported
questions and approval-required attempts. Filters search question/answer/error,
username, status and exact tool name. Pagination reaches older stored attempts.
Only reviewers with `agentic_ai.view_agentapprovalrequest` also see approvals.
The history and logs API responses are marked no-store.

Existing `/admin/agentic_ai/agentrunlog/` remains read-only and now searches
usernames and errors, with a date hierarchy. Execution logs already existed;
there is no migration or retrospective recovery of unrecorded attempts.
Logged responses can be shortened by existing execution code. A submitted valid
question whose execution raises an unexpected exception now also gets a failed
log with the exception type and a generic client response. Ordinary MCP attempts
are logged before discovery/inference/dispatch. HTTP requests rejected before
execution (for example invalid input, CSRF, rate limiting) are not execution logs.
If audit storage itself fails, the failed write cannot be recovered by this view.

## Grant loan access using existing Django Groups

1. Sign in to `/admin/` as a superuser or an authorized user/group administrator.
2. Under Authentication and Authorization → Groups, create `loan_users`.
   Ordinary loan access does not require Django administration permissions.
3. Under Users, assign the intended active users to `loan_users`. They do not
   need Staff status to use the assistant.
4. Open `/agentic/setup/` as a setup administrator. Discover the gateway tools,
   enable the exact loan tool (usually `loan-due-report`), select `loan_users`
   in its role checkboxes, check the project model and save the tools.
5. Test an authorized user and a user outside that group. Removing group
   membership or disabling the tool removes access. Question-guide examples
   use the same authorized discovery.

Setup administrators need Staff status and `agentic_ai.add_agenttool`,
`agentic_ai.change_agenttool`, and `chat.change_systemsetting`.
History-only reviewers need Staff status and `agentic_ai.view_agentrunlog`;
assign that permission to a separate group such as `loan_reviewers` if desired.
Managing user/group membership requires the relevant auth administration
permissions; the tool-setup permissions do not grant user administration.

Existing role behavior: users without any groups resolve to the fallback role
`staff`; superusers resolve to `admin`, and `admin` is privileged. Neither is an
appropriate choice for a restricted ordinary-loan-user group. Use a dedicated
group and do not assume the `staff` role means Django `is_staff` status.
Per-tool group access does not grant branch/customer entitlements: production
NiFi/Oracle must enforce the authenticated account's authorized data scope.
Approval-required tools retain their approval checks; history is not an approval
execution mechanism.

The inspected cloud SQLite snapshot has no Django groups or AgentTool records.
It is not evidence of the live database's configuration. This change creates no
roles, grants no permissions and modifies no live accounts.
