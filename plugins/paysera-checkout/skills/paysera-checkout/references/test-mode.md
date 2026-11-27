# Test Mode

There is no separate sandbox. You test against the live API, `https://api.paysera.com`, with
the project switched to **Test Mode** in the Paysera merchant portal. No real money moves.

1. Create a Checkout project and copy its credentials into the server environment.
2. Enable Test Mode in the project's actions menu.
3. Add your site's URL to the project's websites. Test orders from a URL the project does not
   list are rejected.
4. Run real order flows. Your code does not change: the project's mode decides whether a
   payment is a test.

While Test Mode is on:

- Webhooks are sent exactly as in Live mode and are signed the same way, so test your handler
  end to end.
- Test orders and payments carry an `is_test` flag. Keep test data away from real orders and
  accounting.
- Bank and other non-card methods offer a simulator where you choose success or failure.
- Card payments use test cards. Real cards do not work in Test Mode. The test card numbers and
  the cardholder names that force a decline are in the public guide (link below).
- At most 60 test payments per project per 60 minutes. Over the limit, payment initiation
  fails with `429` and `retry_after`, a Unix time to wait until (not a number of seconds).

Things to test before going live:

- a successful payment marks the order paid only after the webhook, not on the return page;
- a failed or cancelled payment leaves the order unpaid;
- a repeated webhook changes nothing the second time;
- a webhook with a wrong signature is rejected with `401`;
- a refund, including a retry with the same `Idempotency-Key`.

To go live, choose **Disable Test Mode** in the same menu. Nothing in the code changes.

Public guide: https://developers.paysera.com/guides/checkout-modern/getting-started/test-mode
