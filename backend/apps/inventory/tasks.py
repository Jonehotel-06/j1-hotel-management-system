# apps/inventory/tasks.py
"""Celery tasks for the inventory app.

Automatic recipe-based stock deduction has been REMOVED by hotel policy. The
legacy catch-up sweep that used to resolve POS-to-inventory recipe hand-offs
(``apps.inventory.services.inventory_service.process_pending_pos_consumption``)
no longer deducts any stock, and this scheduled task is kept only as a
documented no-op so a deployment that still enqueues it cannot accidentally
reintroduce automatic deductions. Ingredient consumption is recorded manually
by the Store Keeper through the inventory ledger.

The legacy task name is preserved for Celery routing compatibility; it now
performs no work and returns an empty summary.
"""
import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.inventory.tasks.process_pending_pos_stock_consumption")
def process_pending_pos_stock_consumption():
    """Legacy no-op: automatic recipe deduction is disabled.

    Historically this sweep resolved open POS stock-consumption hand-offs and
    posted their recipe deductions. That pathway is closed. The task returns a
    summary with zero posted movements and never touches a stock balance.
    """
    return {"examined": 0, "posted": 0, "skipped": 0, "recipe_deduction_disabled": True}
