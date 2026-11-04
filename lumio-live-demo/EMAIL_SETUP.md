# Email setup (about 3 minutes)

Lumio sends real alert emails through **your own mailbox**. Everything is configured in the dashboard under **Settings → Email delivery**. No files need editing.

## Gmail (recommended for the demo)

Gmail needs an **App Password**. It's a 16-letter code just for Lumio; your normal password won't work.

1. Go to **myaccount.google.com → Security** and make sure **2-Step Verification** is on.
2. Open **myaccount.google.com/apppasswords**, create an app password named "Lumio", and copy the 16 letters.
3. In Lumio, go to **Settings → Email delivery** and fill in:

   | Field | Value |
   |---|---|
   | Provider | Gmail (fills in `smtp.gmail.com`, port 587, STARTTLS) |
   | Sign-in email | your Gmail address |
   | App password | the 16 letters |
   | Demo mode | on |
   | Demo inbox | your Gmail address |
   | Tag by department | on |

   Then switch **email on** (top right of the card) and press **Save email settings**.
4. Press **Send test email**. It should arrive within seconds; check Spam the first time and mark it "Not spam".

**Demo mode** sends every department's email to your one inbox, addressed as `you+fraud@gmail.com`, `you+sre@gmail.com` and so on. Gmail delivers all of these to you, so the audience can watch the routing happen in a single mailbox. For a real team, turn demo mode off and enter each department's address under **Departments**.

## Outlook.com / Microsoft 365

Choose **Outlook.com** or **Microsoft 365** as the provider. Microsoft 365 work accounts need **SMTP AUTH** enabled by the IT admin; some accounts also need an app password.

## Before the demo — checklist

- [ ] The test email arrives in the inbox (not Spam).
- [ ] The inbox is open on the presenting laptop, or on a phone the audience can see.
- [ ] **Dashboard address used by email buttons** is `http://localhost:8000`, so the buttons in the email open the dashboard on this laptop.
- [ ] Optional: in **Alert rules**, turn off "resolved" emails or escalations if you want a quieter inbox.

## If the venue network blocks email

Some guest Wi-Fi networks block outgoing mail (ports 587 and 465). Lumio still composes every email, and the **Email outbox** tab shows each one exactly as it would arrive. Present from there, or use a phone hotspot. Emails that failed can be re-sent from the Outbox later with **Send**.

## Privacy

The app password is stored only in the local `lumio.db` file on this laptop. To remove it, delete `lumio.db` or run `python run_local.py --fresh`.
