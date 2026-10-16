# Owner finance guide — J-ONE Hotel & Lodge

This page is for the hotel owner. You do not need to be a programmer.

## What the numbers mean

These labels are **not** the same thing:

| Label | Meaning |
|---|---|
| **Recognized revenue** | Sales the hotel has earned in the period (guest charged / sale posted). A guest may still owe money. |
| **Collections** | Money actually received (cash, card, bank) in the period. |
| **Refunds** | Money actually returned. |
| **Net collections** | Collections minus refunds. |
| **Operating expenses** | Costs recorded in the period. An approved bill is not automatically paid. |
| **Payments made** | Cash leaving the drawer plus net salary payments actually posted. |
| **Operating result** | Recognized revenue minus operating expenses and accrued payroll. This is **not** statutory profit. |

A booking made today may be paid later. A payment today may be for an older stay. The Accounting page always uses the **hotel business date** in Africa/Lagos.

If a figure cannot be loaded, the screen shows an error — it will **not** pretend the amount is ₦0.

## Where to look

1. **Dashboard** — rooms, arrivals, and (for managers) payment totals.
2. **Accounting** (sidebar, Finance group) — the full money picture for Today, Yesterday, week, month, year, or custom dates. Revenue is split by department (rooms, food & beverage, guest services, other). Posted ledger lines are listed underneath.
3. **Reports** — the same ledger measures plus bookings and occupancy.
4. **Payments / Receipts / Payroll / Inventory / Procurement** — the operational records behind the ledger.

Click **Export CSV** on Accounting or Reports to download the same numbers you see on screen.

## Department revenue

Department amounts are **recognized revenue**, not cash in the till. Restaurant, bar, kitchen and POS must not be added together if they are the same sale — the ledger posts each sale once.

## Guest receivables — what guests still owe

**Guest receivables** (the balance sheet line) is what guests have been billed and not
yet had money applied against.

| Adds to the balance (guest charged) | Subtracts from it (money applied) |
|---|---|
| Accommodation nights, POS / room-service sales, laundry charged to the room | Payments collected and applied to the guest's folio, POS sales tendered at the counter |

    Guest receivables = guest charges posted − payments applied to guest folios

The folio statement every guest sees is built from those same ledger lines, so the total
of all folio balances equals this account. Nothing is counted twice.

**Why it can show as a negative (credit) amount.** A guest who pays at check-in before
the nights are posted makes it temporarily negative — the money is applied, the charge
comes later, and the account corrects itself as the nights post. If the booking is then
cancelled, or the guest is a no-show, or they leave early, nothing reverses the applied
payment, so the credit stays: it is money the hotel is holding for a guest, not an asset.
Two approved ways to clear it:

1. refund the guest through the approval workflow (a manager or the accounts manager
   approves), or
2. post a **balanced reclassification journal** on Accounting → *Post journal entry*:
   Debit `ASSET.GUEST_RECEIVABLE`, Credit `LIABILITY.GUEST_DEPOSITS`.

Posted entries are never edited. `python manage.py guest_receivable_check` lists the
folios behind the figure, the reference for each movement, and warns if any of it was
posted in another currency.

## Paying a supplier — the two ways money leaves

| Situation | What to do | What the ledger does |
|---|---|---|
| The money has **already** gone out (bank transfer done, cash paid at the shop, card charged) | Create the expense with the matching already-paid method, then press **Payment receipt** on that row to file the transfer slip, receipt link and bank reference | The expense credits the bank / cash / card account when it is posted. There is no second step and nothing else to post |
| The bill has to be **paid later** | Create the expense with **On account / not paid yet**. It appears under *Supplier accounts and payables* | Posting credits Accounts payable. **Record supplier payment** later posts Dr accounts payable / Cr the account the money came from, and takes the receipt |

A bill that is still unpaid cannot take a receipt on its own: record the payment first
(that is the step that clears what the hotel owes), then attach the slip if it was
missing. Filing a receipt never moves money and never changes a figure.

## When a guest orders food, drinks or laundry

The guest's bill goes onto their **folio** the moment they receive it, so the room
bill and the folio always agree:

| Guest does this | What the folio shows |
|---|---|
| Orders food, drinks or room service to the room | One charge, posted when the kitchen/bar marks the order **delivered** |
| Sends laundry to be washed (in-house guest) | One charge, posted when the laundry is marked **delivered** to the guest |
| Laundry dropped at reception by someone who is not staying here | No folio at all — that customer pays at the desk |

Until delivery there is no charge and no bill, because the guest may still cancel.
Anything delivered but somehow unbilled is **still stated at check-out** under
*Requests not yet paid for*, and the desk can settle it in the same step, so money
can never quietly disappear from a bill. The gate pass stays blocked until every
naira is cleared.

## A drawer that closes short or over

When a cashier records the physical count at the end of a shift and the count does
not match what the system expected, the drawer is held for a manager decision. The
counted cash itself is never changed — a disagreement with the count does not
rewrite it.

* The session appears in **Accounting → Cash management** with the status
  *Pending variance review*.
* A manager (anyone with the refund-and-adjustment approval right) presses
  **Review & approve**, states the decision and the reason, and the drawer closes.
* **Accept the counted cash** posts the difference to *Cash over / short* and closes
  the session. **Do not accept** closes it with the variance on the record.
* Nobody with that right? The row says *Manager review required* — the money waits
  for a manager instead of being silently adjusted.

Every decision is kept with the reason, the manager's name and the time.

## Photos that will not show

Uploaded pictures (staff profile photos, room photos, gallery pictures) are checked
when you save them, and the page tells you the truth:

* **Saved and showing** — the file reached image storage and the browser loaded it.
* **Saved to the record but not showing** — the file could not be served back. The
  message says so, and usually means the storage the system writes pictures to is
  not reachable or is not public. Give that message to whoever manages the server or
  the storage account; nothing else in the record is affected.
* **Not saved** — the file was refused (wrong type, or it is not really an image).
  Nothing was changed.

Website pictures (home page, policy page) are managed in **Gallery** — that is the
panel the public site reads. The Settings page no longer pretends to upload them.

## Government taxes

There is **no government-tax setting on the Settings page**: customer prices already
include everything, and no tax is added to a guest's bill. The hotel's own statutory
tax figure (VAT the hotel must remit on its own sales) is still calculated by the
system and shown on the Accounting statements, and nothing about past figures has
been deleted.

## If something looks wrong

1. Open **Accounting** for the same dates.
2. Open the posted ledger table and note the **reference**.
3. Ask a manager to open **Audit Logs** for that reference.
4. Do not treat an alert as proof of theft. It is a prompt to investigate.

Hardware (fingerprint / camera) is only proof of attendance when the device is connected and verification succeeds. The software will not pretend a match occurred without the device.
