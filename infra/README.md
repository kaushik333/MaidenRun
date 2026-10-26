# One-time AWS setup

This is the full reference version of these steps. The landing page also
has a condensed, visual walkthrough of the same five steps — click **"AWS
one-time setup"** next to the credentials fields — if you'd rather follow
along there.

The platform never provisions anything with a broad or admin-level AWS
credential. Instead, you create a dedicated IAM user in your own account
whose *only* permissions are the ones in [`iam-policy.json`](iam-policy.json)
— EC2, security group, and Systems Manager Parameter Store provisioning,
plus a narrowly-scoped S3 bucket used only to relay the feedback stack's
own code and your uploaded questionnaire onto its instance (see "Deploy
feedback stack"). (Postgres for the feedback stack runs on EC2 too, not
RDS, so there's no separate database permission block.) Do this once, in
the AWS account you want to deploy into.

1. **Create an IAM user** (IAM console → Users → Create user). It doesn't
   need console access — programmatic access only.

2. **Attach [`iam-policy.json`](iam-policy.json)** directly to that user
   (Add permissions → Create inline policy → paste the JSON). This is the
   only thing the user can ever do — there's no broader credential
   upstream of it to worry about.

   **Already did this before?** This policy grows as the platform gains
   features (e.g. S3 permissions were added for "Deploy feedback stack") —
   there's no migration or version check, so an IAM user set up against an
   older version of this file will start failing AWS calls with an
   `is not authorized to perform: ...` error the moment it needs a
   permission that didn't exist yet when you set it up. If you hit that,
   re-open the same inline policy on your IAM user and replace its
   contents with the current `iam-policy.json` (or the in-app "AWS
   one-time setup" guide, which always reads it fresh) — same for
   [`instance-role-policy.json`](instance-role-policy.json) if the error
   instead comes from the instance role.

3. **Create a second IAM role for the app instance itself** — a
   *different* identity from the user above, one that AWS attaches to
   each launched EC2 instance rather than something you hold credentials
   for. This is what lets the deployed agent fetch its own LLM API key at
   boot without the key ever appearing in plaintext in the instance's
   user-data, which anyone who can describe the instance in AWS can read.
   - IAM console → Roles → Create role → Trusted entity type: **AWS
     service** → Use case: **EC2**.
   - Name it exactly `maidenrun-instance-role` (the platform
     references this name directly — see `backend/app/aws/ec2.py`).
   - Attach [`instance-role-policy.json`](instance-role-policy.json) as
     an inline policy — it lets the role read (and only read) secrets
     under this platform's own parameter namespace.
   - Note this role is shared across every deployment (scoped to the
     whole `/maidenrun/*` prefix, not one deployment's exact
     parameter) rather than created fresh per deployment — a deliberate
     simplification so the platform's own IAM user never needs
     `iam:CreateRole`, which would be a much bigger permission to hand
     over than anything else it asks for.

4. **Create an access key** for the user (Security credentials → Create
   access key).

5. **Give MaidenRun the key** — either way, it never leaves your machine.
   - **Recommended: an AWS CLI profile**, the same way every other AWS tool
     reads credentials:
     ```bash
     aws configure --profile maidenrun   # paste the access key from step 4
     ```
     Then in `.env` set `AWS_PROFILE=maidenrun`, and — when running under
     docker compose — `AWS_CONFIG_DIR=~/.aws` so the backend container can
     see it. AWS SSO profiles work too: give the SSO permission set the
     policy from step 2 instead of creating an IAM user, and run
     `aws sso login --profile maidenrun` when the session expires.
   - **Or paste the key into the deploy form.** It's cached locally
     (`~/.maidenrun/credentials.json`, or `data/credentials.json` under
     docker compose — readable only by you) and reused for every later AWS
     call.

   If both exist, the profile wins. The form shows which identity it's
   using above the region selector.
