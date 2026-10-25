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
6. **External Client App**: Setup → Quick Find "External Client App Manager" → New External Client App.
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
