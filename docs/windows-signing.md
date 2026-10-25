# Signing the Windows app

Signing the app removes the blue "Windows protected your PC" screen that shows up when someone runs the installer. We sign with
**Microsoft Artifact Signing** (it used to be called Trusted Signing). It costs $9.99 a month on the Basic plan. The account
has to belong to the project owner, because Microsoft checks the owner's identity and shows that name as the app's publisher.

Why not SignPath's free plan for open-source projects? Their certificate names "SignPath Foundation" as the publisher, the
project has to apply and wait for approval, and every release needs someone to approve it on SignPath's website.

Once the six secrets below are in place, `.github/workflows/desktop.yml` signs the app and its installer on every Windows
build and then checks the signature. Until then, Windows builds simply aren't signed, as before.

## One-time setup (about 30 minutes, plus waiting for Microsoft's identity check)

Microsoft only verifies individual developers who live in the USA or Canada. Anywhere else, you need a registered company.

1. **Azure account.** Sign in at [portal.azure.com](https://portal.azure.com). If you have no subscription, create a
   *Pay-As-You-Go* one, which needs a card.
2. **Signing account.** Search the portal for **Artifact Signing** and choose **Create**. Pick your subscription, make a new
   resource group (for example `wyze-tunnel`), name the account (for example `wyzetunnel`), pick a nearby region such as
   *East US*, and choose the **Basic** plan. When it's ready, open it and copy the **Account URI** from the Overview page,
   for example `https://eus.codesigning.azure.net/`.
3. **Prove who you are.** If **Create** is greyed out on the next page, first give yourself the *Artifact Signing Identity
   Verifier* role on the account under **Access control (IAM)**. Then, in the signing account, open **Identity validations**,
   choose **New identity**, then **Public** and **Individual**, and follow the steps. Microsoft checks a photo ID. This can
   take from a few hours to a few days, and you'll get an email when it's done.
4. **Certificate profile.** When the identity shows *Completed*, open **Certificate profiles**, choose **Create**, then
   **Public Trust**. Name it (for example `wyze-tunnel`) and pick your verified identity.
5. **A login for GitHub.** Search the portal for **App registrations** and choose **New registration**. Name it
   `wyze-tunnel-github` and keep the default settings. On its Overview page, copy the **Application (client) ID** and the
   **Directory (tenant) ID**. Then open **Certificates & secrets**, choose **New client secret** (24 months), and copy its
   **Value** right away, because it's only shown once.
6. **Let that login sign.** Back in the signing account, open **Access control (IAM)**, then **Add role assignment**. Choose
   **Artifact Signing Certificate Profile Signer** (it may still say *Trusted Signing*), then **Select members**, and search
   for `wyze-tunnel-github`.
7. **Put the values in GitHub, and only there.** In the repository, open **Settings**, then **Secrets and variables**, then
   **Actions**. Add six **repository secrets** with exactly these names:

   | Secret | Value |
   |---|---|
   | `AZURE_CLIENT_ID` | Application (client) ID from step 5 |
   | `AZURE_TENANT_ID` | Directory (tenant) ID from step 5 |
   | `AZURE_CLIENT_SECRET` | The client secret **Value** from step 5 |
   | `ARTIFACT_SIGNING_ENDPOINT` | Account URI from step 2 |
   | `ARTIFACT_SIGNING_ACCOUNT` | Account name from step 2 |
   | `ARTIFACT_SIGNING_PROFILE` | Certificate profile name from step 4 |

   Never paste these into a chat, an issue or a commit. Secrets are hidden even from the people who can run the workflows.

8. **Check it.** Open **Actions**, then **Desktop apps**, then **Run workflow**. Leave the version empty. In the Windows
   job, the step *Check the installer is signed* should show `Status : Valid` with your name.

The client secret expires after 24 months. Before then, make a new one (step 5) and replace `AZURE_CLIENT_SECRET`.
