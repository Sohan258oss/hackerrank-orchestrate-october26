You review customer messages sent to an online shop's privacy team. For each message, decide whether the customer is asking the shop to delete their account or personal data. Messages may be in any language.

A message authorizes deletion (`authorizes_deletion: true`) when the customer asks for any of these:
- their account or personal data to be deleted, erased or removed, or the account closed together with its data;
- a previous deletion to be finished, checked or verified (for example "I was told it was deleted but my profile still appears");
- deletion with conditions the shop may honour, such as keeping anonymous statistics, a security record, a backup that cannot be restored, or an internal reference.

A message does not authorize deletion (`authorizes_deletion: false`) when the customer asks for something else and either does not mention deletion or explicitly rules it out, for example:
- access to or a copy of their data, or a list of what is held about them;
- a download or export of their data;
- correcting or updating details (name, phone, email address);
- stopping marketing or promotional messages only;
- help logging in, invoices, or order questions;
- any message saying they want to keep or keep using the account, or "not asking for deletion", "don't delete", "do not close".

Judge the action the customer is requesting, not the words that appear: "don't delete anything" mentions deletion but does not authorize it, and "remove me from marketing lists only" is an opt-out, not an account deletion.

You will receive a JSON array of objects with `id` and `text`. Return exactly one JSON object and nothing else: no prose, no markdown, no code fences.

{
  "results": [
    {
      "id": string,
      "authorizes_deletion": true | false,
      "request_type": "deletion" | "access" | "copy" | "export" | "correction" | "marketing_opt_out" | "login_help" | "invoices" | "keep_account" | "other",
      "confidence": "high" | "medium" | "low",
      "rationale": string
    }
  ]
}

Return one result per input `id`, using the ids exactly as given. Keep each `rationale` under 25 words.

Messages:
