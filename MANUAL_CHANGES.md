# Manual change list (smallest edits the owner can make by hand)

Applying `PATCH.diff` (or copying the files in this folder) is the recommended route. This sheet exists for the
case where the owner wants to review or hand-apply the **minimum** change for each reported defect, with exact
file names and line numbers.

Two line-number references are given per item:

* **`orig`** = line numbers in the **unmodified repository** you cloned (`main` as audited on 2026-10-29);
* **`new`** = line numbers in the version shipped in this folder (use these if you already copied the file).

---

## 1. Tender amount autofill — the two mandatory edits

### 1.1 Backend: publish the balance (without this the browser has nothing to prefill)

*File:* `backend/apps/pos/serializers.py`
*Class:* `PosOrderListSerializer` — **orig line 325**, **new line 325**

Add five read-only fields (they are inherited by `PosOrderDetailSerializer`, orig line 345):

```python
    net_total = serializers.SerializerMethodField()
    returned_amount = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    outstanding_balance = serializers.SerializerMethodField()
    settlement_action_available = serializers.SerializerMethodField()
```

and add them to `Meta.fields` (**orig line 334–340**, **new line 350–356**), plus the field methods
(**new lines 341–398**):

```python
    @staticmethod
    def _captured(obj):
        annotated = getattr(obj, "captured_amount", None)
        if annotated is not None:
            return Decimal(annotated).quantize(Decimal("0.01"))
        from .services.order_service import tender_captured_amount
        return tender_captured_amount(obj)

    def get_net_total(self, obj):
        return str(Decimal(obj.net_total).quantize(Decimal("0.01")))

    def get_returned_amount(self, obj):
        return str(Decimal(obj.returned_amount or 0).quantize(Decimal("0.01")))

    def get_amount_paid(self, obj):
        return str(self._captured(obj).quantize(Decimal("0.01")))

    def get_outstanding_balance(self, obj):
        from .services.order_service import order_outstanding_balance
        return str(order_outstanding_balance(obj, captured=self._captured(obj)))

    def get_settlement_action_available(self, obj):
        if obj.mode == PosOrder.Mode.ROOM_SERVICE:
            return False
        if obj.status != PosOrder.Status.DELIVERED or not obj.charge_transaction_id:
            return False
        return self.get_outstanding_balance(obj) != "0.00"
```

> Note the relative import: `from .services...`, **not** `from ..services...` (the latter resolves to
> `apps.services` and raises `ModuleNotFoundError` — this was caught during development).

*File:* `backend/apps/pos/views.py` — **orig line 59** `_order_queryset()` and **orig line 187**
`_order_detail_queryset()`: add the `captured_amount` annotation (**new lines 59–103**) so the list endpoint
stays one query. Without it the serializer still works, but issues one extra query per row.

### 1.2 Frontend: prefill the amount box

*File:* `frontend/dashboard/pos.html`
*Function:* `captureTender()` — **orig line 866**, **new line 963**

The one-line minimum (inside the existing `tenderFields` array, **orig line 874**):

```js
{name:"amount",label:"Amount ("+(order.currency||"NGN")+")",type:"number",step:"0.01",required:true,
 value:String(Number(order.outstanding_balance||0).toFixed(2)),        // ← the missing prefill
 help:"Prefilled with the outstanding balance. Lower it to record a partial payment."},
```

and in the `onSubmit` payload (**orig line 881**) add:

```js
currency:balance.currency,
expected_outstanding_balance:balance.outstanding.toFixed(2),
idempotency_key:state.tenderKey,     // set once per opened form, e.g. state.tenderKey=newKey();
```

The shipped version additionally renders a balance panel, refuses room-service/settled/undelivered orders and
reports the resulting balance (**new lines 944–1064**).

---

## 2. Duplicate collection (double click / retry) — mandatory if partial payments are used

*File:* `backend/apps/pos/services/order_service.py`
*Function:* `capture_direct_tender` — **orig line 795**, **new line 832**

Minimum: accept `idempotency_key=""` and, immediately after the captured-tender sum, return the existing
tender when the key is already present:

```python
    idempotency_key = str(idempotency_key or "").strip()[:96]
    if idempotency_key:
        existing = (PosTender.objects.select_for_update()
                    .filter(order=order, metadata__idempotency_key=idempotency_key)
                    .order_by("pk").first())
        if existing is not None:
            existing.idempotent_replay = True
            return existing
```

and store the key on create: `metadata={"idempotency_key": idempotency_key} if idempotency_key else {}`
(**new line 928**).
Because this runs inside the `select_for_update()` order lock, two concurrent identical requests serialise on
the same row.

*File:* `backend/apps/pos/serializers.py` — `PosTenderCaptureSerializer`, **orig line 403**, **new line 480**:
accept the three optional fields (`currency`, `idempotency_key`, `expected_outstanding_balance`).
*File:* `backend/apps/pos/views.py` — `PosTenderCaptureView.post`, **orig line 987**, **new line 1033**: forward
them and return `200` when `tender.idempotent_replay` is set.

---

## 3. “Fully paid order must not accept another payment”

Already enforced server-side; make the message explicit and keep the UI honest.

*File:* `backend/apps/pos/services/order_service.py` — **orig line 807** (just after the captured sum),
**new lines 880–885**:

```python
    outstanding = order_outstanding_balance(order, captured=captured)
    if outstanding <= Decimal("0.00"):
        raise ValidationError(
            f"Order {order.reference} is already fully settled "
            f"({captured} of {order.net_total} {order_currency} collected); there is no balance to collect."
        )
```

*File:* `frontend/dashboard/pos.html` — `actionButtons()`, **orig line 723**, **new line 772**: gate the button
on the server's `settlement_action_available` and print the amount:

```js
var collectable=order.settlement_action_available!=null?!!order.settlement_action_available:Number(outstanding||0)>0;
if(status==="DELIVERED"&&order.mode!=="ROOM_SERVICE"&&collectable&&canTender())buttons+='…>Collect '+(outstanding!=null?JONE.esc(money(outstanding,order.currency)):"")+'</button>';
```

---

## 4. Paid-receipt gate must use the charge now due, not the frozen total

*File:* `frontend/dashboard/pos.html`
*Function:* `printThermalReceipt()` — **orig line 820**, **new lines 878–882**

```js
var receiptBalance=tenderBalance(order);                       // net_total / amount_paid / outstanding
if(capturedAmount+0.0001<receiptBalance.net||receiptBalance.outstanding>0){ … ask Finance to reconcile … }
```

and the printed receipt object (**orig lines 833–841**, **new lines 891–899**): `total:receiptBalance.net`,
`change:Math.max(0,capturedAmount-receiptBalance.net)`, `balance_due:receiptBalance.outstanding`.

---

## 5. Show the authoritative availability (direct sale **and** recipe)

### 5.1 Backend (one place only)

*File:* `backend/apps/inventory/services/inventory_service.py`

* **orig line 1023**, **new line 1038** — add `open_pos_order_demand()` (the single implementation of “stock
  other open orders already promised”);
* **orig line 1023**, **new line 1084** — `pos_stock_summaries(menu_item_ids, *, include_open_order_demand=True)`
  gains `available_quantity`, `available_sufficient_for_one`, `committed_quantity`, `availability_unit` and the
  per-component `quantity_committed` / `quantity_available` (**new lines 1147–1204**). `saleable_quantity`,
  `sufficient_for_one` and `stock_unit` are untouched.

*File:* `backend/apps/pos/services/order_service.py` — **orig lines 272–291**, **new line 283**: the validator
now calls the shared helper instead of keeping its own copy of the rule.

### 5.2 Frontend

*File:* `frontend/dashboard/pos.html`

* **orig line 446** (`renderItemStockHint`), **new line 488** — append the portion/recipe wording, the
  “held by other open orders” note, and the staleness caveat;
* **orig line 420** (`stockOptionSuffix`), **new line 464** — use `availableNow(stock)` for the number;
* **orig line 678** (`addCartLine`), **new line 723** — use `availableNow(stock)` so the pre-add check measures
  the same figure the server will;
* **orig line 706**, **new line 752** — cart line stores `stock_available_now`,
  `stock_committed`, `stock_availability_unit`;
* **orig line 917** (the “+” handler), **new line 976** — tighten `stockCeiling` with
  `il.stock_available_now` when present;
* **orig line 377**, **new line 378** — the cart row prints what is left to order now.

Helpers to copy verbatim: `availableNow`, `availabilityUnit`, `availabilityWord`, `isRecipeProduct`,
`committedNote`, `recipeNote`, `stalenessNote` (**new lines 429–450**).

---

## 6. Name the dish as well as the ingredient in a recipe refusal

*File:* `backend/apps/pos/services/order_service.py`
*Function:* `_validate_order_lines_stock` — **orig lines 337–348** (the multi-component `else` branch),
**new lines 353–373**:

```python
            dish_labels = []
            for menu_item_id_in, _quantity_in, _component_in in lines_in:
                dish_label = names.get(menu_item_id_in) or ""
                if dish_label and dish_label not in dish_labels:
                    dish_labels.append(dish_label)
            dish_note = (" for " + ", ".join(dish_labels)) if dish_labels else ""
```

then include `{dish_note}` after “but this order needs …”.

---

## 7. Test file that must be updated with the receipt change

*File:* `frontend/tests-js/test-pos-receipt-and-transfer-ui.js` — **orig line 12** (the
`capturedAmount+0.0001<Number(order.total_amount||0)` assertion). It pinned the **buggy** contract; it now
asserts the corrected one (**new lines 12–19**). If you apply only the manual edits above, update this
assertion or `node --test tests-js/` will (correctly) fail.
