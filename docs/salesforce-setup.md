# Salesforce setup (Developer Edition)

The agent authenticates with the **OAuth 2.0 Client Credentials flow** on an **External Client App**.
Salesforce restricts creating new Connected Apps as of Spring '26 and is retiring the username-password flow,
and Client Credentials is its recommended flow for server-to-server integrations. Every call runs as one
locked-down integration user. (JWT bearer would be the choice if the app had to act as different users.)

1. **Sign up** at developer.salesforce.com/signup, confirm the email, and set a password. Salesforce may
   delete orgs that are inactive for 45 days.
2. **My Domain**: Setup → Quick Find "My Domain" → copy the host (e.g. `yourname-dev-ed.develop.my.salesforce.com`).
   Tokens must be requested from this host, not login.salesforce.com.
3. **Salesforce CLI** (admin session used only for seeding):
   `npm install -g @salesforce/cli`, then `sf org login web --alias dealdesk-admin --instance-url https://<my-domain>`.
4. **Integration user**: Setup → Users → New User. License **Salesforce Integration**, profile
   **Minimum Access - API Only Integrations**. If that license isn't offered, use the Salesforce license with
   the **Minimum Access - Salesforce** profile.
5. **Permission set** "Deal Desk Agent" (License matching the user's):
   - Object Settings: Accounts, Contacts, Opportunities → **Read** + **View All**. No Edit, no Delete.
   - System Permissions: **Access Activities** and **Edit Tasks**. Without Access Activities, Task and Event
     don't exist at all for this user (`INVALID_TYPE: sObject type 'Task' is not supported`), even with
     Edit Tasks granted. API Enabled comes from the profile.
   - Manage Assignments → add the integration user.
   - Tasks and Events don't appear in permission set Object Settings. Their field-level security is set per
     field in **Object Manager** on the integration user's **profile** (step 6).
6. **Task/Event field-level security** on the "Minimum Access - API Only Integrations" profile:
   Setup → Object Manager → Task (and Event) → Fields & Relationships → field → Set Field-Level Security.
   - Task: **Related To** (`WhatId`), **Due Date** (`ActivityDate`), **Comments** (`Description`) → Visible,
     editable. **Type** → Visible, read-only.
   - Event: **Related To**, **Description**, **Type** → Visible, read-only.
   - Without these, Task queries fail with `No such column 'ActivityDate' on entity 'Task'` and a Task can't
     be linked to an opportunity.
7. **External Client App**: Setup → Quick Find "External Client App Manager" → New External Client App.
   - Distribution State **Local**; Enable OAuth; Callback URL `https://login.salesforce.com/services/oauth2/success`
     (required, unused); scope **Manage user data via APIs (api)**; **Enable Client Credentials Flow**.
   - Policies tab → Edit → OAuth Policies → Enable Client Credentials Flow → **Run As** = the integration user.
   - Settings tab → OAuth Settings → Consumer Key and Secret → put them in `.env` as `SF_CLIENT_ID` / `SF_CLIENT_SECRET`.
   - A new app can take ~10 minutes before it issues tokens.

Then:

```bash
uv run python scripts/seed_salesforce.py
uv run deal-desk sf-check
uv run deal-desk sf-check --write-test-task <an opportunity id>
```

The last command confirms the integration user can create Tasks (the one write the agent is allowed).

## Verified access (2026-10-25)

| As the integration user | Result |
|---|---|
| Read Account, Contact, Opportunity, Task, Event | allowed |
| Create Task on an opportunity | allowed |
| Update Opportunity | refused (`CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY`) |
| Create Event | refused (Event not createable) |
| Delete a Task **it owns** | **allowed** (bundled with Edit Tasks) |

Known limit: Salesforce gives any user with Edit Tasks the right to edit and delete Tasks they own, and there
is no separate permission to remove it. So Task edits and deletes are blocked in code only: `SalesforceClient`
has no update or delete method, and a unit test fails if one is added. A production setup could add a
before-delete Apex trigger on Task that rejects deletes by the integration user.
